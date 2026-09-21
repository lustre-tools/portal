"""The graph index: a JSON list of generated graphs, on disk.

Written from three places -- the web app, the scheduled refresher, and
any one-off script -- possibly at the same time, so every write takes an
exclusive lock and lands via a temp file and a rename. A reader must
never see a half-written index.

Nothing here reads configuration. The project name that counts as public,
the timezone and the refresh anchor are all passed in by the caller,
which keeps this module importable without an app context.
"""

import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("UTC")
DEFAULT_ANCHOR = time(8, 0)

# A graph is keyed either by a numeric Gerrit change number (e.g. "62796")
# or, in ticket mode, by an uppercase ticket (e.g. "LU-19921"). The two
# forms are regex-disjoint -- a ticket always has a letter prefix -- so a
# ticket graph "LU-19921.html" never collides with change 19921's
# "19921.html".
GRAPH_ID_PATTERN = r"(?:\d+|[A-Z][A-Z0-9]*-\d+)"
GRAPH_ID_RE = re.compile(rf"^{GRAPH_ID_PATTERN}$")
GRAPH_FILE_RE = re.compile(rf"^({GRAPH_ID_PATTERN})\.html$")


def is_valid_graph_id(value):
    """True if value is a valid graph id (numeric change or ticket)."""
    return bool(GRAPH_ID_RE.match(str(value)))


def entry_project(entry, public_project):
    """The project an entry belongs to.

    Entries written before the field existed default to the public
    project -- at that time only public graphs could be created, so the
    assumption is safe.
    """
    return entry.get("project") or public_project


def is_internal_entry(entry, public_project):
    """True if this entry belongs to a project other than the public one."""
    return entry_project(entry, public_project) != public_project


def _index_path(output_dir):
    return os.path.join(output_dir, "index.json")


def _load_index(output_dir):
    path = _index_path(output_dir)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def _write_index(output_dir, entries):
    """Replace the index in one step. Caller must hold the lock."""
    fd, tmp_path = tempfile.mkstemp(dir=output_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp_f:
            json.dump(entries, tmp_f, indent=2)
        os.replace(tmp_path, _index_path(output_dir))
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _update(output_dir, mutate):
    """Run a read-modify-write cycle on the index under an exclusive lock.

    ``mutate`` receives the current entries and returns the list to
    write, or ``None`` to leave the index untouched. Its return value is
    passed back to the caller as ``(result, wrote)``.
    """
    os.makedirs(output_dir, exist_ok=True)
    lock_path = _index_path(output_dir) + ".lock"
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            entries = _load_index(output_dir)
            new_entries = mutate(entries)
            if new_entries is None:
                return False
            _write_index(output_dir, new_entries)
            return True
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def add_entry(
    output_dir,
    change_number,
    name="",
    subject="",
    ticket="",
    params=None,
    stats=None,
    labels=None,
    project=None,
    anchor_change_number=None,
    touch_generated_at=True,
    tz=DEFAULT_TZ,
):
    key = str(change_number)
    entry = {}

    def mutate(entries):
        existing = next((e for e in entries if e["change_number"] == key), None)

        if touch_generated_at or not existing or "generated_at" not in existing:
            generated_at = datetime.now(tz).strftime("%Y-%m-%d %I:%M %p %Z")
        else:
            generated_at = existing["generated_at"]

        entry.update(
            {
                "change_number": key,
                "name": name,
                "subject": subject,
                "ticket": ticket,
                "generated_at": generated_at,
                "file": f"{key}.html",
            }
        )

        # Prefer an explicit project, else keep what was there; a missing
        # value is resolved at read time by entry_project().
        if project:
            entry["project"] = project
        elif existing and "project" in existing:
            entry["project"] = existing["project"]

        # Ticket-mode entries record the resolved anchor change so the UI
        # can deep-link to Gerrit (the key is the ticket, not a change
        # number). Preserve it across metadata-only edits.
        if anchor_change_number:
            entry["anchor_change_number"] = anchor_change_number
        elif existing and "anchor_change_number" in existing:
            entry["anchor_change_number"] = existing["anchor_change_number"]

        if params:
            entry["params"] = params
        if stats:
            entry["stats"] = stats
        elif existing and "stats" in existing:
            entry["stats"] = existing["stats"]
        if labels is not None:
            entry["labels"] = labels
        elif existing and "labels" in existing:
            entry["labels"] = existing["labels"]
        if existing:
            if "refresh_interval_hours" in existing:
                entry["refresh_interval_hours"] = existing["refresh_interval_hours"]
            if "next_refresh" in existing:
                entry["next_refresh"] = existing["next_refresh"]

        return [entry] + [e for e in entries if e["change_number"] != key]

    _update(output_dir, mutate)
    return entry


def delete_entry(output_dir, change_number):
    key = str(change_number)
    _update(output_dir, lambda entries: [e for e in entries if e["change_number"] != key])

    # Outside the lock: losing the index entry is what matters, and a
    # stale HTML file is unreachable without one.
    html_path = os.path.join(output_dir, f"{key}.html")
    if os.path.exists(html_path):
        os.remove(html_path)


def next_refresh_time(interval_hours, tz=DEFAULT_TZ, anchor=DEFAULT_ANCHOR):
    """The next refresh slot for an interval, anchored to a time of day.

    Intervals map to daily slots starting at the anchor (08:00 by
    default)::

        24h  -> 08:00 daily
        12h  -> 08:00 and 20:00
         6h  -> 08:00, 14:00, 20:00, 02:00
        48h  -> 08:00 every other day
       168h  -> 08:00 weekly

    Always returns a slot in the future.
    """
    now = datetime.now(tz)
    base_hour = anchor.hour

    if interval_hours >= 24:
        slot = now.replace(hour=base_hour, minute=anchor.minute, second=0, microsecond=0)
        if slot <= now:
            slot += timedelta(days=1)
        return slot

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slots = []
    h = base_hour
    while h < base_hour + 24:
        slot = today_start.replace(hour=h % 24, minute=anchor.minute)
        if h % 24 < base_hour:
            slot += timedelta(days=1)
        slots.append(slot)
        h += interval_hours

    for slot in sorted(slots):
        if slot > now:
            return slot
    return sorted(slots)[0] + timedelta(days=1)


def update_schedule(
    output_dir, change_number, interval_hours, tz=DEFAULT_TZ, anchor=DEFAULT_ANCHOR
):
    """Set or clear the auto-refresh schedule for one graph.

    ``interval_hours``: a positive int to enable, 0 or None to disable.
    """
    key = str(change_number)

    def mutate(entries):
        for e in entries:
            if e["change_number"] == key:
                if interval_hours and interval_hours > 0:
                    e["refresh_interval_hours"] = interval_hours
                    e["next_refresh"] = next_refresh_time(interval_hours, tz, anchor).isoformat()
                else:
                    e.pop("refresh_interval_hours", None)
                    e.pop("next_refresh", None)
                break
        return entries

    _update(output_dir, mutate)


def transfer_schedule(output_dir, from_change, to_change):
    """Move a schedule between entries, e.g. when a graph is re-keyed.

    No-op if either entry is missing or the source has no schedule.
    """
    if str(from_change) == str(to_change):
        return

    def mutate(entries):
        src = next((e for e in entries if e["change_number"] == str(from_change)), None)
        if not src or "refresh_interval_hours" not in src:
            return None

        dst = next((e for e in entries if e["change_number"] == str(to_change)), None)
        if dst is None:
            return None

        dst["refresh_interval_hours"] = src["refresh_interval_hours"]
        if src.get("next_refresh") is not None:
            dst["next_refresh"] = src["next_refresh"]
        return entries

    _update(output_dir, mutate)


def get_entry(output_dir, change_number):
    for e in _load_index(output_dir):
        if e["change_number"] == str(change_number):
            return e
    return None


def get_due_entries(output_dir, tz=DEFAULT_TZ):
    """Entries whose next_refresh has passed."""
    now = datetime.now(tz)
    due = []
    for e in _load_index(output_dir):
        raw = e.get("next_refresh")
        if not raw:
            continue
        try:
            if datetime.fromisoformat(raw) <= now:
                due.append(e)
        except (ValueError, TypeError):
            pass
    return due


def list_entries(output_dir, query=None, labels=None):
    entries = _load_index(output_dir)

    # Label filter: an entry must carry every requested label.
    if labels:
        wanted = [label.lower() for label in labels]
        entries = [
            e
            for e in entries
            if all(label in [el.lower() for el in e.get("labels", [])] for label in wanted)
        ]

    if not query:
        return entries

    q = query.lower()
    return [
        e
        for e in entries
        if q in e["change_number"].lower()
        or q in e.get("name", "").lower()
        or q in e.get("subject", "").lower()
        or q in e.get("ticket", "").lower()
        or any(q in label.lower() for label in e.get("labels", []))
        or q in json.dumps(e.get("stats", {})).lower()
    ]
