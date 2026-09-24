import json
import logging
import os
import re
import subprocess
import threading
import urllib.request

from flask import current_app

from portal.graph_store import add_entry, delete_entry, transfer_schedule
from portal.tools.registry import ToolDefinition, ToolParam, register_tool

logger = logging.getLogger(__name__)

# Track which change numbers are currently being generated
_running_jobs = set()
_running_lock = threading.Lock()

# Cap how many interactive runs hit Gerrit at the same time. Under eventlet
# the threading primitives are monkey-patched, so this yields cooperatively.
_run_semaphore = None
_sem_lock = threading.Lock()


def _get_run_semaphore(limit):
    global _run_semaphore
    with _sem_lock:
        if _run_semaphore is None:
            _run_semaphore = threading.BoundedSemaphore(max(1, int(limit)))
    return _run_semaphore


# Case-sensitive ticket form the gc CLI treats as ticket mode
# (mirrors cli.py::cmd_graph's re.fullmatch). The web form uppercases
# before matching so "lu-19921" is accepted and normalized.
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
# Conservative branch name: no leading dash (avoids argv option injection).
_BRANCH_RE = re.compile(r"^[\w][\w./\-]*$")
_MAX_EXTRA_TICKETS = 10


def _parse_identifier(raw):
    """Classify the graph identifier the user submitted.

    Returns (kind, positional, file_id):
      - kind: "ticket", "number", or "url"; None if the input is invalid.
      - positional: the argument passed to `gc graph` (uppercased ticket,
        the number, or the URL verbatim).
      - file_id: the index key / output filename stem (uppercased ticket,
        or the numeric change number — trailing digits for a URL).
    """
    s = (raw or "").strip()
    if not s:
        return None, None, None
    up = s.upper()
    if _TICKET_RE.match(up):
        return "ticket", up, up
    if re.match(r"^\d+$", s):
        return "number", s, s
    if re.match(r"^https?://\S+$", s):
        m = re.search(r"(\d+)\s*$", s)
        if not m:
            return None, None, None
        return "url", s, m.group(1)
    return None, None, None


def _normalize_tickets(raw, cap=_MAX_EXTRA_TICKETS):
    """Uppercase, validate, dedupe and cap a comma-separated ticket list.

    Returns a comma-joined string of valid tickets (possibly empty).
    """
    if not raw:
        return ""
    out, seen = [], set()
    for piece in str(raw).split(","):
        t = piece.strip().upper()
        if not t or t in seen or not _TICKET_RE.match(t):
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= cap:
            break
    return ",".join(out)


def _gerrit_get_change(base_url, change_number):
    """Lightweight Gerrit API call to get project + subject for a change.

    Uses HTTP basic auth via /a/changes/ when GERRIT_USER and GERRIT_PASS
    are present in the environment - required for projects that are not
    readable anonymously. Falls back to the public /changes/ endpoint
    when no credentials are configured, which is all a portal serving
    only public projects needs.
    """
    import base64

    user = os.environ.get("GERRIT_USER", "")
    password = os.environ.get("GERRIT_PASS", "")
    if user and password:
        url = f"{base_url}/a/changes/{change_number}"
    else:
        url = f"{base_url}/changes/{change_number}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    if user and password:
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")

    with urllib.request.urlopen(req, timeout=15) as resp:
        # Gerrit prefixes JSON with )]}' to prevent XSSI
        body = resp.read().decode("utf-8")
        if body.startswith(")]}'"):
            body = body[4:].lstrip()
        return json.loads(body)


def run_gc_graph(params, socketio, room):
    """Run gc graph as a subprocess, streaming output via WebSocket."""
    raw_identifier = params.get("change_number", "").strip()

    # Accept a change number, a Gerrit URL, or a bare JIRA ticket (LU-12345).
    kind, positional, file_id = _parse_identifier(raw_identifier)
    if not kind:
        socketio.emit(
            "output",
            {
                "line": "Error: invalid input. Enter a change number, Gerrit URL, or ticket (e.g. LU-12345).\n"
            },
            room=room,
        )
        socketio.emit("complete", {"ok": False, "error": "Invalid identifier"}, room=room)
        return

    # Check if already running (keyed on the resolved file id: a ticket and a
    # like-numbered change never collide because file ids are regex-disjoint).
    with _running_lock:
        if file_id in _running_jobs:
            socketio.emit(
                "output",
                {"line": f"Graph for {file_id} is already being generated. Please wait.\n"},
                room=room,
            )
            socketio.emit("complete", {"ok": False, "error": "Already running"}, room=room)
            return
        _running_jobs.add(file_id)

    try:
        # Bound total concurrent runs. If no slot is free, tell the user we're
        # queued, then block (cooperatively) until one frees up.
        sem = _get_run_semaphore(current_app.config["INTERACTIVE_RUN_CONCURRENCY"])
        if not sem.acquire(blocking=False):
            socketio.emit(
                "output",
                {"line": "Queued - waiting for a free run slot...\n"},
                room=room,
            )
            sem.acquire()
        try:
            _do_run(params, socketio, room, kind, positional, file_id)
        finally:
            sem.release()
    finally:
        with _running_lock:
            _running_jobs.discard(file_id)


def _do_run(params, socketio, room, kind, positional, file_id):
    """Actual graph generation logic.

    kind is "ticket", "number" or "url". positional is the argument passed
    to `gc graph`; file_id is the index key / output filename stem.
    """
    # If this is an edit that changed the identifier, we'll need to carry
    # over the schedule from the old entry and remove it.
    original_change = params.pop("_original_change_number", None)
    if original_change == file_id:
        original_change = None

    # Internal-project capability is set by the WebSocket handler based on
    # the session's roles. We pop it so it never lands in stored
    # params (which would let a user smuggle the bit back via /metadata).
    internal_access = bool(params.pop("_internal_access", False))

    public_project = current_app.config["PUBLIC_PROJECT"]

    # --branch is privileged: it selects the ticket-mode anchor branch,
    # which can reach branches outside the public project. Drop it
    # entirely for unprivileged sessions so it neither runs nor gets
    # stored and replayed by a scheduled refresh.
    if not internal_access:
        params.pop("branch", None)

    branch = ""
    extra_tickets = ""

    if kind == "ticket":
        # Ticket mode: there is no single change to pre-flight. The project
        # (public vs internal) is classified after generation from the
        # produced graph's node projects.
        ticket = positional  # already uppercased
        subject = ""
        project = None
        branch = (params.get("branch", "") or "").strip()
        if branch and not _BRANCH_RE.match(branch):
            branch = ""
        extra_tickets = _normalize_tickets(params.get("ticket", ""))
        socketio.emit(
            "output",
            {
                "line": f"Resolving ticket {ticket}"
                + (f" on branch {branch}" if branch else "")
                + "...\n"
            },
            room=room,
        )
    else:
        # Numeric / URL: pre-flight the specific change for project + subject.
        socketio.emit("output", {"line": f"Checking change {file_id}...\n"}, room=room)
        try:
            change_info = _gerrit_get_change(current_app.config["GERRIT_URL"], file_id)
            project = change_info.get("project", "")
            subject = change_info.get("subject", "")
        except Exception:
            # For users without internal access, "fetch failed" and "exists
            # but is outside the public project" must produce identical
            # errors, so the response cannot be used to infer that an
            # internal change exists.
            if not internal_access:
                socketio.emit(
                    "output", {"line": f"Error: change {file_id} is not available.\n"}, room=room
                )
                socketio.emit("complete", {"ok": False, "error": "Not available"}, room=room)
            else:
                socketio.emit(
                    "output",
                    {"line": f"Error: could not fetch change {file_id} from Gerrit.\n"},
                    room=room,
                )
                socketio.emit(
                    "complete",
                    {"ok": False, "error": "Could not fetch change from Gerrit"},
                    room=room,
                )
            return

        # Authorization: non-public projects need internal access. Do NOT
        # disclose the actual project name to the user - that would confirm
        # the change exists in an internal project they aren't supposed to
        # know about. Use the same generic "not available" message as the
        # fetch-failure path. Log the real project for admin debugging only.
        if project != public_project and not internal_access:
            logger.info(
                "Access denied: change %s belongs to %s (user lacks internal access)",
                file_id,
                project,
            )
            socketio.emit(
                "output", {"line": f"Error: change {file_id} is not available.\n"}, room=room
            )
            socketio.emit("complete", {"ok": False, "error": "Not available"}, room=room)
            return

        # Pull the ticket out of the subject, e.g. "LU-12345 osd: ..."
        prefix = re.escape(current_app.config["TICKET_PREFIX"])
        ticket_match = re.match(rf"({prefix}-\d+)", subject)
        ticket = ticket_match.group(1) if ticket_match else ""

    # Use custom name if provided; otherwise the commit subject, falling back
    # to the ticket id for ticket-mode graphs (subject is filled in post-run).
    custom_name = params.get("name", "").strip()
    name = custom_name or subject or (ticket if kind == "ticket" else "")

    # Normalize labels: split, lowercase, strip, allow [a-z0-9._-], dedupe
    labels = _normalize_labels(params.get("labels", ""))

    if kind != "ticket":
        socketio.emit("output", {"line": f"Project: {project}\n"}, room=room)
        socketio.emit("output", {"line": f"Subject: {subject}\n"}, room=room)
        if ticket:
            socketio.emit("output", {"line": f"Ticket:  {ticket}\n"}, room=room)
    if extra_tickets:
        socketio.emit("output", {"line": f"Extra tickets: {extra_tickets}\n"}, room=room)
    if labels:
        socketio.emit("output", {"line": f"Labels:  {', '.join(labels)}\n"}, room=room)
    socketio.emit("output", {"line": "\n"}, room=room)

    output_dir = current_app.config["GRAPH_OUTPUT_DIR"]
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{file_id}.html")

    cmd = [
        current_app.config["GC_BIN"],
        "graph",
        positional,
        "--no-open",
        "-o",
        output_path,
    ]

    # Pass custom name to the gc tool so it appears in the graph title.
    # Only pass if explicitly set (otherwise gc uses its default).
    if custom_name:
        cmd.extend(["--name", custom_name])

    if kind == "ticket":
        # Ticket mode never uses --cross-project. A public ticket must not
        # follow into other projects -- that would pull internal nodes into
        # HTML anyone can read -- and a privileged ticket graph is already
        # scoped to its own project by the anchor's branch.
        if branch:
            cmd.extend(["--branch", branch])
        if extra_tickets:
            cmd.extend(["--ticket", extra_tickets])
    else:
        # Cross-project: only for internal anchors, and only when
        # authorized. A public graph must never follow into other
        # projects, even for a privileged user, because the resulting
        # HTML is world-readable and its embedded JSON would carry
        # internal change identifiers.
        if project != public_project and internal_access:
            cmd.append("--cross-project")
            socketio.emit(
                "output",
                {"line": "Cross-project mode enabled (non-public anchor).\n"},
                room=room,
            )

    # Optional flags (shared across modes)
    if params.get("comments"):
        cmd.append("--comments")
    if params.get("skip_topic"):
        cmd.append("--skip-topic")
    if params.get("skip_hashtag"):
        cmd.append("--skip-hashtag")
    if params.get("skip_ci_details"):
        cmd.append("--skip-ci-details")
    if params.get("include_topic"):
        topics = params["include_topic"].strip()
        if re.match(r"^[\w,\-\.]+$", topics):
            cmd.extend(["--include-topic", topics])
    if params.get("include_hashtag"):
        hashtags = params["include_hashtag"].strip()
        if re.match(r"^[\w,\-\.]+$", hashtags):
            cmd.extend(["--include-hashtag", hashtags])

    socketio.emit("output", {"line": f"$ gc graph {positional} ...\n"}, room=room)

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=current_app.config.get("GC_CWD") or None,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        # Capture the CLI's final JSON result line (raw, pre-sanitize) so we
        # can read the resolved ticket anchor for a Gerrit deep-link.
        last_json = None
        for line in process.stdout:
            sanitized = _sanitize_output(line, _deployment_paths())
            socketio.emit("output", {"line": sanitized}, room=room)
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                last_json = stripped

        process.wait()

        if process.returncode == 0:
            anchor_change = None
            if kind == "ticket":
                # Learn the resolved anchor change number from the CLI's JSON.
                if last_json:
                    try:
                        result_obj = json.loads(last_json)
                        a = result_obj.get("anchor")
                        if isinstance(a, int) or (isinstance(a, str) and str(a).isdigit()):
                            anchor_change = str(a)
                    except (ValueError, TypeError):
                        pass
                # Classify by the generated graph's node projects: any node
                # outside the public project makes the whole graph internal
                # and hides it from the public list and direct URLs. This is
                # what keeps the split correct in ticket mode, where there is
                # no single change to pre-flight.
                classified, anchor_subject = classify_graph_project(
                    output_path,
                    public_project,
                    anchor_id=anchor_change,
                )
                project = classified or public_project
                if anchor_subject:
                    subject = anchor_subject
                    if not custom_name:
                        name = anchor_subject

            # Store in index with metadata and params for re-running
            stored_params = {}
            for k, v in params.items():
                if isinstance(v, (str, bool, int, float)):
                    stored_params[k] = v
            stats = extract_stats_from_html(output_path, current_app.config["CI_VOTERS"])
            summary = extract_summary(output_path)
            add_entry(
                output_dir,
                file_id,
                name=name,
                subject=subject,
                ticket=ticket,
                params=stored_params,
                labels=labels,
                stats=stats,
                summary=summary,
                project=project,
                anchor_change_number=anchor_change,
                tz=current_app.config["TIMEZONE"],
            )

            # Edit-with-changed-identifier: carry over the schedule from the
            # old entry, then remove it so the table doesn't show duplicates.
            if original_change:
                transfer_schedule(output_dir, original_change, file_id)
                delete_entry(output_dir, original_change)
                socketio.emit(
                    "output",
                    {"line": f"Replaced previous entry for {original_change}.\n"},
                    room=room,
                )

            graph_url = f"/gerrit_vis/graphs/{file_id}.html"
            socketio.emit(
                "complete",
                {
                    "ok": True,
                    "graph_url": graph_url,
                    "change_number": file_id,
                    "name": name,
                    "subject": subject,
                    "ticket": ticket,
                },
                room=room,
            )
        else:
            socketio.emit(
                "complete",
                {"ok": False, "error": "Graph generation failed. Check the output for details."},
                room=room,
            )
    except Exception:
        socketio.emit("output", {"line": "\nError: an internal error occurred.\n"}, room=room)
        socketio.emit("complete", {"ok": False, "error": "Internal error"}, room=room)
        logger.exception("Error running gc graph tool")


# Paths that should never be exposed to users
_SENSITIVE_PATHS = [
    "/root/",
    "/srv/",
    "/etc/",
    "/home/",
    "/var/",
    "/opt/",
]


def _deployment_paths():
    """The directories this install writes to, wherever they are."""
    cfg = current_app.config
    return (cfg.get("GRAPH_OUTPUT_DIR"), cfg.get("DATA_DIR"), cfg.get("GC_CWD"))


from portal.tools.graph_stats import (  # noqa: E402
    classify_graph_project,
    extract_stats_from_html,
    extract_summary,
)

_LABEL_CHAR_RE = re.compile(r"[^a-z0-9._-]+")


def _normalize_labels(raw):
    """Split comma-separated labels, lowercase, strip, dedupe, sanitize chars.

    Returns a list of normalized label strings.
    """
    if not raw:
        return []
    seen = set()
    out = []
    for piece in raw.split(","):
        p = piece.strip().lower()
        if not p:
            continue
        # Replace forbidden characters with hyphen, collapse multiples
        p = _LABEL_CHAR_RE.sub("-", p).strip("-")
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _sanitize_output(line, extra_paths=()):
    """Strip server filesystem paths and Python tracebacks from output.

    The static list covers the usual system locations. ``extra_paths``
    is for the directories this deployment actually uses -- the data
    dir, the graph dir -- which can be anywhere the operator chose, so
    a fixed list would miss them. The first real install put data under
    /var/lib and the graph path went straight to the browser.
    """
    for path in list(extra_paths) + _SENSITIVE_PATHS:
        if not path:
            continue
        path = path.rstrip("/") + "/"
        if path in line:
            line = re.sub(
                r'(?:File\s+")?' + re.escape(path) + r'[^\s",:]+',
                lambda m: os.path.basename(m.group(0).rstrip('"')),
                line,
            )
    return line


def init_gc_graph_tool():
    register_tool(
        ToolDefinition(
            id="gc-graph",
            name="Gerrit Change Graph",
            description="Interactive DAG visualization of related Gerrit changes. "
            "Enter a change number, URL, or ticket (e.g. LU-12345) to generate an "
            "interactive graph showing the full topology including branches, abandoned "
            "forks, and stale patchsets. "
            "Anyone signed in may graph the public project; other projects "
            "need an account with the 'internal' role.",
            parameters=[
                ToolParam(
                    name="change_number",
                    label="Change / URL / Ticket",
                    type="text",
                    required=True,
                    placeholder="62796 or LU-12345",
                    help_text="Gerrit change number, full URL, or a ticket (e.g. LU-12345). Projects other than the public one need the 'internal' role.",
                ),
                ToolParam(
                    name="name",
                    label="Name (optional)",
                    type="text",
                    placeholder="",
                    help_text="Custom name for this graph. If empty, the commit subject is used.",
                ),
                ToolParam(
                    name="labels",
                    label="Labels (optional)",
                    type="text",
                    placeholder="wbc, pcc, ec",
                    help_text="Comma-separated free-form labels for filtering. Lowercase, [a-z0-9._-] only.",
                ),
                ToolParam(
                    name="ticket",
                    label="Extra tickets (optional)",
                    type="text",
                    placeholder="LU-18222, LU-17916",
                    help_text="Additional tickets (comma-separated) to pull into the graph.",
                ),
                ToolParam(
                    name="branch",
                    label="Ticket branch (optional)",
                    type="text",
                    placeholder="master",
                    internal_only=True,
                    help_text="Only used when the identifier is a ticket. Defaults to master; choosing another branch needs the 'internal' role.",
                ),
                ToolParam(
                    name="comments",
                    label="Fetch comments",
                    type="checkbox",
                    help_text="Fetch detailed inline comments (slower, ~30s for large series)",
                ),
                ToolParam(
                    name="skip_ci_details",
                    label="Skip CI details",
                    type="checkbox",
                    help_text="Skip fetching CI links (faster, fewer API calls)",
                ),
                ToolParam(
                    name="skip_topic",
                    label="Skip topic",
                    type="checkbox",
                    help_text="Do not include series sharing the anchor's topic",
                ),
                ToolParam(
                    name="skip_hashtag",
                    label="Skip hashtag",
                    type="checkbox",
                    help_text="Do not include series sharing the anchor's hashtags",
                ),
                ToolParam(
                    name="include_topic",
                    label="Include topics",
                    type="text",
                    placeholder="topic1,topic2",
                    help_text="Comma-separated additional topics to include",
                ),
                ToolParam(
                    name="include_hashtag",
                    label="Include hashtags",
                    type="text",
                    placeholder="hash1,hash2",
                    help_text="Comma-separated additional hashtags to include",
                ),
            ],
            run_fn=run_gc_graph,
            output_type="html_file",
            public_output=True,
        )
    )
