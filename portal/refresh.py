"""Regenerate graphs whose scheduled refresh is due.

Run from a timer, e.g. every 30 minutes::

    portal-refresh

This deliberately drives the *same* code path as an interactive run --
``run_gc_graph`` -- rather than rebuilding the ``gc`` command line. The
previous implementation was a shell script wrapping a Python heredoc
that reimplemented the argument builder, the slot arithmetic and the
index write; keeping four copies of that logic in step by hand was a
standing bug. The only thing swapped out is where the output goes: a
log sink instead of a WebSocket.

Scheduled entries run with internal access. They can only have been
scheduled by an admin in the first place, and dropping privileges here
would silently turn an internal graph into a failed refresh.
"""

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from portal.graph_store import (
    _load_index,
    _update,
    get_due_entries,
    next_refresh_time,
    set_derived,
)
from portal.tools.graph_stats import extract_patches, extract_stats_from_html, extract_summary

logger = logging.getLogger("portal.refresh")


class _LogSink:
    """Stands in for the SocketIO server when nobody is watching.

    ``run_gc_graph`` only ever calls ``emit``, so a log sink is enough to
    reuse it verbatim.
    """

    def __init__(self, label):
        self.label = label
        self.ok = None
        self.error = None

    def emit(self, event, payload=None, **_kwargs):
        payload = payload or {}
        if event == "output":
            line = (payload.get("line") or "").rstrip("\n")
            if line:
                logger.info("[%s] %s", self.label, line)
        elif event == "complete":
            self.ok = bool(payload.get("ok"))
            self.error = payload.get("error")


def _refresh_one(app, entry):
    key = entry["change_number"]
    params = dict(entry.get("params") or {})
    params["change_number"] = key
    # Scheduled runs are privileged: see the module docstring.
    params["_internal_access"] = True
    # A rerun must not try to replace some other entry.
    params.pop("_original_change_number", None)

    sink = _LogSink(key)
    with app.app_context():
        from portal.tools.gc_graph import run_gc_graph

        try:
            run_gc_graph(params, sink, room=None)
        except Exception:
            logger.exception("[%s] refresh raised", key)
            return key, False
    if sink.ok:
        logger.info("[%s] refreshed", key)
    else:
        logger.warning("[%s] refresh failed: %s", key, sink.error or "unknown")
    return key, bool(sink.ok)


def _bump_schedules(output_dir, keys, tz, anchor):
    """Move every refreshed entry to its next slot.

    Done for failures too: a broken graph should retry on its normal
    cadence rather than be retried on every single timer tick.
    """
    keys = set(keys)

    def mutate(entries):
        touched = False
        for e in entries:
            if e["change_number"] not in keys:
                continue
            interval = e.get("refresh_interval_hours")
            if interval:
                e["next_refresh"] = next_refresh_time(interval, tz, anchor).isoformat()
                touched = True
        return entries if touched else None

    _update(output_dir, mutate)


def _backfill(output_dir, ci_voters):
    """Fill in stats for graphs generated before the index stored them."""
    entries = _load_index(output_dir)
    with_summary = 0
    for e in entries:
        path = os.path.join(output_dir, e.get("file") or f"{e['change_number']}.html")
        stats = extract_stats_from_html(path, ci_voters)
        summary = extract_summary(path)
        patches = extract_patches(path, ci_voters)
        if summary:
            with_summary += 1
        set_derived(output_dir, e["change_number"], stats=stats, summary=summary, patches=patches)
    logger.info(
        "backfilled %d graph(s); %d carry a summary (the rest predate it "
        "and get one on their next regeneration)",
        len(entries),
        with_summary,
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="portal-refresh",
        description="Regenerate graphs whose scheduled refresh is due",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what is due and exit without regenerating anything",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="re-read stats from the graph files already on disk into the "
        "index, without regenerating anything (after an upgrade)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="refresh every scheduled entry, not only the due ones",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from portal.app import create_app

    app = create_app()
    output_dir = app.config["GRAPH_OUTPUT_DIR"]
    tz = app.config["TIMEZONE"]
    anchor = app.config["REFRESH_ANCHOR"]

    if args.backfill:
        return _backfill(output_dir, app.config["CI_VOTERS"])

    if args.all:
        due = [e for e in _load_index(output_dir) if e.get("refresh_interval_hours")]
    else:
        due = get_due_entries(output_dir, tz=tz)

    if not due:
        logger.info("nothing due")
        return 0

    logger.info("%d graph(s) due: %s", len(due), ", ".join(e["change_number"] for e in due))
    if args.dry_run:
        return 0

    workers = app.config["SCHEDULED_REFRESH_CONCURRENCY"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda e: _refresh_one(app, e), due))

    _bump_schedules(output_dir, [key for key, _ in results], tz, anchor)

    failed = [key for key, ok in results if not ok]
    if failed:
        logger.warning("%d failed: %s", len(failed), ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
