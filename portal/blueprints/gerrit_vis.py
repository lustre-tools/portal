import time
from datetime import datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from portal.auth import (
    can_view_internal,
    is_admin,
    is_authenticated,
    require_admin,
    require_auth,
)
from portal.graph_store import (
    GRAPH_FILE_RE,
    add_entry,
    delete_entry,
    get_entry,
    is_internal_entry,
    is_valid_graph_id,
    list_entries,
    update_schedule,
)
from portal.stats_view import MISSING, entry_view
from portal.tools.gc_graph import _normalize_labels
from portal.tools.registry import get_tool, list_tools


def _public_project():
    return current_app.config["PUBLIC_PROJECT"]


gerrit_vis_bp = Blueprint("gerrit_vis", __name__, template_folder="../templates/gerrit_vis")


def _can_act_on_entry(entry):
    """Whether the current session may rerun/delete/metadata-edit an entry.

    Public entries: anyone signed in. Internal entries: only accounts
    holding the internal role (admin implies it).
    """
    if not is_authenticated():
        return False
    if is_internal_entry(entry, _public_project()):
        return can_view_internal()
    return True


def _visible_entries(entries):
    """Filter out internal entries the current session cannot see."""
    if can_view_internal():
        return entries
    return [e for e in entries if not is_internal_entry(e, _public_project())]


@gerrit_vis_bp.route("/")
def index():
    """Public: anyone may browse public graphs. Signing in adds
    create/rerun/delete; the internal role adds internal graphs;
    scheduling stays admin-only."""
    query = request.args.get("q", "")
    label_filters = request.args.getlist("label")
    # Normalize: lowercase, dedupe, drop empty
    label_filters = [label.strip().lower() for label in label_filters if label.strip()]
    seen = set()
    label_filters = [label for label in label_filters if not (label in seen or seen.add(label))]

    output_dir = current_app.config["GRAPH_OUTPUT_DIR"]
    entries = list_entries(
        output_dir,
        query if query else None,
        labels=label_filters or None,
    )
    # Filter out internal entries from users who can't see them. Do this AFTER
    # the underlying list_entries so the search/label filter behaves the same;
    # we just drop forbidden rows from the result.
    entries = _visible_entries(entries)

    tools = list_tools()
    authenticated = is_authenticated()
    admin = is_admin()
    internal_access = can_view_internal()

    # Autocomplete labels: only from entries this session can see, or
    # internal-only labels would leak through the suggestions.
    visible_all = _visible_entries(list_entries(output_dir))
    all_labels = sorted({label for e in visible_all for label in e.get("labels", [])})

    # Only send rerun/edit data to signed-in users, and only for entries
    # they can act on. Those entries should already have been filtered
    # out above; this is belt and braces.
    rerun_data = {}
    if authenticated:
        for e in entries:
            if not _can_act_on_entry(e):
                continue
            rerun_data[e["change_number"]] = {
                "params": e.get("params", {}),
                "name": e.get("name", e.get("subject", "")),
                "labels": e.get("labels", []),
            }

    now = time.time()
    views = {e["change_number"]: entry_view(e, now) for e in entries}

    return render_template(
        "gerrit_vis/index.html",
        tools=tools,
        entries=entries,
        views=views,
        missing=MISSING,
        query=query,
        label_filters=label_filters,
        rerun_data=rerun_data,
        authenticated=authenticated,
        is_admin=admin,
        internal_access=internal_access,
        all_labels=all_labels,
        public_project=_public_project(),
        now=datetime.now().astimezone(),
    )


# Keep old routes as redirects
@gerrit_vis_bp.route("/dashboard")
def dashboard():
    return redirect(url_for("gerrit_vis.index"))


@gerrit_vis_bp.route("/graphs/")
def graph_list():
    return redirect(url_for("gerrit_vis.index", q=request.args.get("q", "")))


@gerrit_vis_bp.route("/graphs/<path:filename>")
def serve_graph(filename):
    """Serve a generated graph HTML.

    Public-project graphs are served to anyone; internal ones need the
    internal role. Everything else -- including other files in the graph
    directory, such as index.json -- is a 404, so the endpoint never
    discloses that an internal or auxiliary file exists.
    """
    if ".." in filename:
        abort(404)

    # Strict allowlist: only <change-or-ticket>.html may be served, and only
    # if an index entry exists for it. This means index.json, lock files,
    # temp files, etc. are all 404 - never reachable via this route.
    m = GRAPH_FILE_RE.match(filename)
    if not m:
        abort(404)

    output_dir = current_app.config["GRAPH_OUTPUT_DIR"]
    entry = get_entry(output_dir, m.group(1))
    if not entry:
        abort(404)
    if is_internal_entry(entry, _public_project()) and not can_view_internal():
        abort(404)

    return send_from_directory(output_dir, filename)


@gerrit_vis_bp.route("/graphs/metadata/<change_number>", methods=["POST"])
@require_auth
def update_metadata(change_number):
    """Update only name and labels of an existing graph - no regeneration."""
    if not is_valid_graph_id(change_number):
        return "Bad request", 400

    output_dir = current_app.config["GRAPH_OUTPUT_DIR"]
    existing = get_entry(output_dir, change_number)
    # Treat "no such entry" and "exists but unauthorized" as
    # byte-identical 404s, so this endpoint cannot be used to enumerate
    # internal entries by probing change numbers.
    if not existing or not _can_act_on_entry(existing):
        abort(404)

    new_name = (request.form.get("name", "") or "").strip() or existing.get("subject", "")
    new_labels = _normalize_labels(request.form.get("labels", ""))

    # Update params dict to reflect new name/labels for future re-runs
    stored_params = dict(existing.get("params", {}))
    stored_params["name"] = new_name
    stored_params["labels"] = ", ".join(new_labels)

    add_entry(
        output_dir,
        change_number,
        name=new_name,
        subject=existing.get("subject", ""),
        ticket=existing.get("ticket", ""),
        params=stored_params,
        labels=new_labels,
        touch_generated_at=False,
    )
    flash(f"Updated metadata for change {change_number}.", "success")
    return redirect(url_for("gerrit_vis.index"))


@gerrit_vis_bp.route("/graphs/delete/<change_number>", methods=["POST"])
@require_auth
def delete_graph(change_number):
    if not is_valid_graph_id(change_number):
        return "Bad request", 400
    output_dir = current_app.config["GRAPH_OUTPUT_DIR"]

    existing = get_entry(output_dir, change_number)
    # Identical 404 for "no such entry" and "exists but unauthorized",
    # so probing change numbers cannot enumerate internal entries.
    # Public entries follow the same rule: the reply is unambiguous
    # because the caller sees the same response either way.
    if not existing or not _can_act_on_entry(existing):
        abort(404)

    delete_entry(output_dir, change_number)
    flash(f"Graph for change {change_number} deleted.", "success")

    # Preserve current filters across the redirect, but only if at least one
    # entry still matches them. Otherwise drop the filters so the user isn't
    # left staring at an empty list.
    query = request.form.get("q", "").strip()
    label_filters = [
        label.strip().lower() for label in request.form.getlist("label") if label.strip()
    ]
    seen = set()
    label_filters = [label for label in label_filters if not (label in seen or seen.add(label))]

    redirect_args = {}
    if query or label_filters:
        remaining = list_entries(
            output_dir,
            query if query else None,
            labels=label_filters or None,
        )
        remaining = _visible_entries(remaining)
        if remaining:
            if query:
                redirect_args["q"] = query
            if label_filters:
                redirect_args["label"] = label_filters

    return redirect(url_for("gerrit_vis.index", **redirect_args))


ALLOWED_SCHEDULE_INTERVALS = {0, 6, 12, 24, 48, 168}


@gerrit_vis_bp.route("/graphs/schedule/<change_number>", methods=["POST"])
@require_admin
def schedule_graph(change_number):
    if not is_valid_graph_id(change_number):
        return "Bad request", 400
    output_dir = current_app.config["GRAPH_OUTPUT_DIR"]

    interval = request.form.get("interval", "0")
    try:
        interval_hours = int(interval)
    except ValueError:
        return "Bad request", 400

    # Restrict to the dropdown values so the anchored slot scheduler
    # stays predictable and the UI can always show the stored value.
    if interval_hours not in ALLOWED_SCHEDULE_INTERVALS:
        return "Bad request", 400

    update_schedule(
        output_dir,
        change_number,
        interval_hours,
        tz=current_app.config["TIMEZONE"],
        anchor=current_app.config["REFRESH_ANCHOR"],
    )
    if interval_hours > 0:
        flash(f"Auto-refresh set for change {change_number}: every {interval_hours}h.", "success")
    else:
        flash(f"Auto-refresh disabled for change {change_number}.", "success")
    return redirect(url_for("gerrit_vis.index"))


def register_socketio_handlers(socketio):
    @socketio.on("connect")
    def handle_connect():
        """Refuse the handshake outright for anonymous callers.

        start_tool checks again -- this is not the security boundary --
        but there is no reason to hold an open socket for someone who
        cannot use it.

        Note the limitation: a WebSocket only carries cookies during the
        handshake, so a socket opened while signed in keeps that
        identity until it disconnects. Roles are still resolved live on
        every event, so revoking one takes effect immediately; it is
        only the identity itself that is fixed for the connection's
        lifetime. The client opens a socket per run and closes it on
        completion, which keeps that window short.
        """
        if not is_authenticated():
            return False
        return None

    @socketio.on("start_tool")
    def handle_start_tool(data):
        # Per-connection identifier. Every emit for this run targets it, so
        # output reaches ONLY the originating client. Without it the default
        # emit broadcasts to every connected client, leaking error text and
        # disrupting other users' in-flight runs.
        sid = request.sid

        # Any signed-in account may run a tool.
        if not is_authenticated():
            socketio.emit("complete", {"ok": False, "error": "Unauthorized"}, to=sid)
            return

        run_id = data.get("run_id")
        tool_id = data.get("tool_id")
        params = data.get("params")

        if not run_id or not tool_id or not isinstance(params, dict):
            socketio.emit("complete", {"ok": False, "error": "Invalid request"}, to=sid)
            return

        # Edit case: the user is replacing an entry under a different change
        # number. Only honor the replace intent if the session can act on the
        # original entry. If the entry is missing OR exists but is not
        # actionable by this session (an internal entry, say), we
        # silently drop the _original_change_number and proceed as a normal
        # first-time run. This makes "exists but unauthorized" and "does not
        # exist" indistinguishable, so the WebSocket cannot be used to
        # enumerate internal entries.
        original = data.get("original_change_number")
        if original and is_valid_graph_id(original):
            existing = get_entry(current_app.config["GRAPH_OUTPUT_DIR"], str(original))
            if existing and _can_act_on_entry(existing):
                params["_original_change_number"] = str(original)
            # else: silently drop - do NOT emit a distinct error.

        # Capability flag derived from the session's roles, NOT from the
        # client. The tool reads it to decide whether to allow anchors
        # outside the public project and whether to add --cross-project.
        # Pop any client-supplied value first: the session is the only
        # source of truth.
        params.pop("_internal_access", None)
        params["_internal_access"] = can_view_internal()

        # Stream to this connection only, never to a client-named room.
        # Flask-SocketIO already places every client in a room named after
        # its own sid, so there is nothing to join. The client opens a
        # dedicated socket per run and does not filter on run_id, so the sid
        # is the correct target -- and run_id stays what it always was on the
        # client: a local bookkeeping key, not a channel name the server
        # trusts.
        tool = get_tool(tool_id)
        if not tool:
            socketio.emit("complete", {"ok": False, "error": "Tool not found"}, room=sid)
            return

        # Capture app reference before spawning background task
        app = current_app._get_current_object()

        def run_task():
            with app.app_context():
                tool.run_fn(params, socketio, sid)

        socketio.start_background_task(run_task)
