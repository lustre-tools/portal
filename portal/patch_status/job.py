"""One Patch Status run, and what the web page needs to know about runs.

A run fetches, classifies and renders, then replaces the page body and
its meta data in DIR (normally DATA_DIR/patch_status). While it runs it
holds DIR/run.lock and writes its progress to DIR/status.json, so the page
can show it whoever started it -- the Update button, which starts
``portal-patch-status`` in its own process, or a timer running the same
command. A failed run leaves the last good page in place.

    page.html    the body (render.Page.render), replaced atomically
    meta.json    {version, fetched_at, finished_at, project, branch, head,
                  total, buckets, seconds}
    status.json  {state, phase, done, total, started_at, finished_at, error}
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from typing import Optional

PAGE = "page.html"
META = "meta.json"
STATUS = "status.json"
LOCK = "run.lock"


class Busy(Exception):
    """Another run holds the lock."""


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_meta(directory: str) -> Optional[dict]:
    return _read_json(os.path.join(directory, META))


def read_page(directory: str) -> Optional[str]:
    try:
        with open(os.path.join(directory, PAGE), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def is_running(directory: str) -> bool:
    """Whether a run holds the lock right now. A run that died (killed,
    host restarted) holds nothing, whatever status.json says."""
    path = os.path.join(directory, LOCK)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def read_status(directory: str) -> dict:
    """status.json, corrected by the lock: running only while a run holds it."""
    status = _read_json(os.path.join(directory, STATUS)) or {}
    running = is_running(directory)
    if status.get("state") == "running" and not running:
        status = {**status, "state": "failed",
                  "error": status.get("error") or "the run stopped before it finished"}
    status["running"] = running
    return status


@contextmanager
def _lock(directory: str):
    os.makedirs(directory, exist_ok=True)
    fd = os.open(os.path.join(directory, LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy("a Patch Status run is already in progress") from None
        yield
    finally:
        os.close(fd)


class _Status:
    """status.json for the run in progress, written at most twice a
    second -- the series phase reports every one of ~2400 lookups."""

    def __init__(self, directory: str):
        self.path = os.path.join(directory, STATUS)
        self.data = {
            "state": "running", "phase": "starting", "done": 0, "total": None,
            "started_at": time.time(), "finished_at": None, "error": None,
        }
        self._last = 0.0
        self._write()

    def _write(self) -> None:
        _write_json(self.path, self.data)
        self._last = time.monotonic()

    def progress(self, phase: str, done: int, total: Optional[int]) -> None:
        changed = phase != self.data["phase"]
        self.data.update(phase=phase, done=done, total=total)
        if changed or time.monotonic() - self._last >= 0.5 or (total and done >= total):
            self._write()

    def finish(self, error: Optional[str] = None) -> None:
        self.data.update(
            state="failed" if error else "done", error=error,
            finished_at=time.time(), phase="", done=0, total=None,
        )
        self._write()


def run(
    directory: str,
    *,
    gerrit_url: str,
    jira_url: str,
    project: str,
    branch: str,
    ticket_prefix: str = "LU",
    orgs_file: Optional[str] = None,
    commit_url: str = "",
    trial_picks: bool = True,
    borrow_from: Optional[str] = None,
    limit: Optional[int] = None,
    log=None,
) -> dict:
    """One full run; returns the new meta. Raises Busy if one is running,
    FetchError (after recording it) if the data was incomplete."""
    from portal.patch_status.classify import BUCKET_ORDER, classify
    from portal.patch_status.fetch import FetchError, fetch
    from portal.patch_status.orgs import Orgs
    from portal.patch_status.render import Page

    log = log or (lambda _: None)
    with _lock(directory):
        status = _Status(directory)
        started = time.time()
        try:
            orgs = Orgs.load(orgs_file)
            raw = fetch(
                gerrit_url=gerrit_url, jira_url=jira_url, project=project, branch=branch,
                ticket_prefix=ticket_prefix, limit=limit, progress=status.progress, log=log,
            )
            if trial_picks:
                from portal.patch_status.trial import TrialError, try_picks

                try:
                    tried = try_picks(
                        raw["changes"], directory=directory, gerrit_url=gerrit_url,
                        project=project, branch=branch, borrow_from=borrow_from,
                        progress=status.progress, log=log,
                    )
                    raw["rebase_check"] = f"trial picks onto {branch} ({tried} tried)"
                except TrialError as exc:
                    log(f"trial picks not tried, the votes stand: {exc}")
                    raw["rebase_check"] = "wc-checkpatch votes (the trial picks failed)"
            status.progress("rendering", 0, None)
            changes = classify(raw["changes"])
            body = Page(gerrit_url, jira_url, ticket_prefix, orgs, commit_url).render(raw, changes)
        except FetchError as exc:
            status.finish(error=str(exc)[:300] or "the data was incomplete")
            raise
        except Exception:
            # The page is public: no paths or internals in what it shows.
            # The traceback goes to stderr, the service's log.
            status.finish(error="the run failed; the service log has the details")
            raise
        page_path = os.path.join(directory, PAGE)
        with open(page_path + ".new", "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(page_path + ".new", page_path)
        meta = {
            "version": f"{raw['fetched_at']:.0f}",
            "fetched_at": raw["fetched_at"],
            "finished_at": time.time(),
            "project": raw["project"],
            "branch": raw["branch"],
            "head": raw.get("head"),
            "total": len(changes),
            "buckets": {b: sum(c.get("bucket") == b for c in changes) for b in BUCKET_ORDER},
            "seconds": round(time.time() - started),
            "rebase_check": raw.get("rebase_check") or "wc-checkpatch votes",
        }
        _write_json(os.path.join(directory, META), meta)
        status.finish()
        log(f"{meta['total']} open changes in {meta['seconds']}s")
        return meta


def next_allowed(directory: str, floor_seconds: int, now: Optional[float] = None) -> Optional[float]:
    """When the next run may start (epoch seconds), or None if now. The
    floor counts from the last start, failed runs included: a run is
    thousands of Gerrit requests whatever its outcome."""
    status = _read_json(os.path.join(directory, STATUS)) or {}
    started = status.get("started_at")
    if not isinstance(started, (int, float)):
        return None
    at = started + floor_seconds
    return at if at > (now if now is not None else time.time()) else None


def main(argv=None) -> int:
    """portal-patch-status: one run with the portal's configuration."""
    from portal.config import build_config

    p = argparse.ArgumentParser(prog="portal-patch-status", description=(
        "Fetch the open changes and rebuild the Patch Status page. "
        "Reads Gerrit and Jira without logging in."))
    p.add_argument("--limit", type=int, default=None, help="only the first N changes (testing)")
    p.add_argument("--quiet", action="store_true", help="no progress on stderr")
    args = p.parse_args(argv)

    cfg = build_config(testing=True)
    directory = cfg["PATCH_STATUS_DIR"]

    def log(msg: str) -> None:
        if not args.quiet:
            print(f"[patch-status] {msg}", file=sys.stderr)

    from portal.patch_status.fetch import FetchError

    try:
        run(
            directory,
            gerrit_url=cfg["GERRIT_URL"], jira_url=cfg["JIRA_URL"],
            project=cfg["PATCH_STATUS_PROJECT"], branch=cfg["PATCH_STATUS_BRANCH"],
            ticket_prefix=cfg["TICKET_PREFIX"], orgs_file=cfg["PATCH_STATUS_ORGS"],
            commit_url=cfg["COMMIT_URL"],
            trial_picks=cfg["PATCH_STATUS_TRIAL_PICKS"], borrow_from=cfg["GRAPH_CONFLICTS_REPO"],
            limit=args.limit, log=log,
        )
    except Busy as exc:
        print(f"portal-patch-status: {exc}", file=sys.stderr)
        return 3
    except FetchError as exc:
        print(f"portal-patch-status: fetch failed, the page is unchanged: {exc}", file=sys.stderr)
        return 9
    return 0


if __name__ == "__main__":
    sys.exit(main())
