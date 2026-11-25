"""Patch Status: the page, its status and its Update button.

The page shows the body the last run rendered (portal/patch_status). Update
starts ``portal-patch-status`` as a child process: a run is minutes of
fetching and seconds of pure CPU for classifying and rendering, which must
not stall this eventlet server. The run holds a lock and writes its
progress to a file, so the page shows it whoever started it. Anyone may
press Update, but runs start at most every PATCH_STATUS_MIN_INTERVAL
seconds, and never two at once.
"""

import gzip
import subprocess
import sys
import threading
import time

from flask import Blueprint, current_app, jsonify, make_response, render_template, request
from markupsafe import Markup

from portal.patch_status import job

patch_status_bp = Blueprint("patch_status", __name__)

# The run this process started last: covers the moment between starting it
# and the child taking the lock.
_child = {"proc": None}
_child_lock = threading.Lock()

# Below this a response is not worth compressing.
_GZIP_MIN = 4096


def _dir():
    return current_app.config["PATCH_STATUS_DIR"]


def _child_running():
    proc = _child["proc"]
    return proc is not None and proc.poll() is None


def _utc(epoch):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch)) if epoch else ""


def _gzipped(response):
    """nginx in front does not compress, and the page is ~2 MB of HTML."""
    if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
        return response
    data = response.get_data()
    if len(data) < _GZIP_MIN:
        return response
    response.set_data(gzip.compress(data, 6))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    return response


@patch_status_bp.route("/")
def index():
    meta = job.read_meta(_dir()) or {}
    body = job.read_page(_dir())
    status = job.read_status(_dir())
    html = render_template(
        "patch_status/index.html",
        meta=meta,
        body=Markup(body) if body else None,
        failed=status.get("state") == "failed" and not status.get("running"),
        error=status.get("error"),
        updated_text=_utc(meta.get("fetched_at")),
        branch=meta.get("branch") or current_app.config["PATCH_STATUS_BRANCH"],
        project=meta.get("project") or current_app.config["PATCH_STATUS_PROJECT"],
    )
    response = make_response(html)
    # It carries the viewer's CSRF token for the Update button.
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    return _gzipped(response)


@patch_status_bp.route("/api/status")
def status():
    directory = _dir()
    meta = job.read_meta(directory) or {}
    st = job.read_status(directory)
    running = st["running"] or _child_running()
    response = jsonify({
        "running": running,
        "phase": st.get("phase") if st["running"] else ("starting" if running else ""),
        "done": st.get("done") or 0,
        "total": st.get("total"),
        "version": meta.get("version"),
        "updated_at": meta.get("fetched_at"),
        "next_update_at": None if running else job.next_allowed(
            directory, current_app.config["PATCH_STATUS_MIN_INTERVAL"]),
        "error": st.get("error") if st.get("state") == "failed" and not running else None,
        "now": time.time(),
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@patch_status_bp.route("/api/update", methods=["POST"])
def update():
    directory = _dir()
    with _child_lock:
        if _child_running() or job.is_running(directory):
            return jsonify(started=False, message="An update is already running.")
        at = job.next_allowed(directory, current_app.config["PATCH_STATUS_MIN_INTERVAL"])
        if at:
            return jsonify(
                started=False,
                message=f"Updated recently; the next update can start at {_utc(at)}.",
                next_update_at=at,
            )
        # stdout is quiet; errors and tracebacks go to the service's log.
        _child["proc"] = proc = subprocess.Popen(
            [sys.executable, "-m", "portal.patch_status.job", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
    # Reap it, so it does not linger as a zombie.
    threading.Thread(target=proc.wait, name="patch-status-reaper", daemon=True).start()
    current_app.logger.info("patch status: update started (pid %s)", proc.pid)
    return jsonify(started=True, message="Update started.")
