"""Application factory."""

import functools
import hashlib
import os

from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix

from portal.config import build_config
from portal.csrf import init_csrf

socketio = SocketIO()


@functools.cache
def _asset_version(static_folder, filename):
    """A short hash of one static file, computed once per process -- the
    files only change with a deploy, and a deploy restarts the service."""
    try:
        with open(os.path.join(static_folder, filename), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:10]
    except OSError:
        return None


def create_app(overrides=None, testing=False):
    app = Flask(__name__)
    app.config.update(build_config(testing=testing))
    if overrides:
        app.config.update(overrides)
    if testing:
        app.config["TESTING"] = True

    # Behind a proxy the peer address is always the proxy's, so without
    # this every client looks like 127.0.0.1 and any per-IP limiting is
    # meaningless. Only trust the headers when an operator has said how
    # many proxies there are -- otherwise a direct caller could forge
    # X-Forwarded-For freely.
    hops = app.config.get("PROXY_HOPS", 0)
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops, x_prefix=hops)

    # Without this, cors_allowed_origins defaults to "*" and any page on
    # the internet could open a socket carrying the visitor's cookies.
    # None (rather than []) means "same origin only": engineio then
    # derives the allowed origin from the request itself.
    socketio.init_app(
        app,
        async_mode="eventlet",
        cors_allowed_origins=app.config.get("ALLOWED_ORIGINS") or None,
        # Use the Flask session from the handshake rather than keeping a
        # separate server-side copy that can drift from it.
        manage_session=False,
    )

    init_csrf(app)

    # Every static URL carries a hash of the file's content, so a deploy
    # that changes the stylesheet or a script changes its URL, and no
    # browser keeps using the copy it cached from the previous version.
    # Without it, a visitor saw the new page with the old stylesheet
    # until they forced a reload.
    @app.url_defaults
    def _versioned_static(endpoint, values):
        if endpoint == "static" and "filename" in values:
            version = _asset_version(app.static_folder, values["filename"])
            if version:
                values.setdefault("v", version)

    from portal.auth import register_auth_routes
    from portal.blueprints.gerrit_vis import (
        gerrit_vis_bp,
        register_socketio_handlers,
    )

    app.register_blueprint(gerrit_vis_bp, url_prefix="/gerrit_vis")
    register_auth_routes(app)
    register_socketio_handlers(socketio)

    # The private area exists only if an operator asked for one.
    prefix = app.config.get("PRIVATE_PREFIX")
    if prefix:
        from portal.blueprints.private_area import make_private_blueprint

        app.register_blueprint(
            make_private_blueprint(app.config["PRIVATE_ROLE"]),
            url_prefix=prefix,
        )

    from portal.tools.gc_graph import init_gc_graph_tool

    init_gc_graph_tool()

    @app.context_processor
    def _chrome():
        """Values every template's header needs."""
        from portal.auth import current_roles, current_user

        return {
            "site_name": app.config["SITE_NAME"],
            "gerrit_url": app.config["GERRIT_URL"],
            "jira_url": app.config["JIRA_URL"],
            "private_prefix": app.config.get("PRIVATE_PREFIX") or "",
            "private_label": app.config.get("PRIVATE_LABEL") or "",
            "private_role": app.config.get("PRIVATE_ROLE") or "",
            "private_theme": app.config.get("PRIVATE_THEME") or "",
            "dashboard_prefix": app.config.get("DASHBOARD_PREFIX") or "",
            "current_user": current_user(),
            "current_roles": current_roles(),
        }

    @app.after_request
    def add_security_headers(response):
        from flask import request as req

        # Generated graph HTML and anything served out of the private
        # area are self-contained documents with inline scripts and CDN
        # dependencies; they need a looser policy than the app's own
        # pages. Everything else gets the strict one.
        private_prefix = app.config.get("PRIVATE_PREFIX")
        is_served_file = req.path.startswith("/gerrit_vis/graphs/") and req.path.endswith(".html")
        if private_prefix and req.path.startswith(private_prefix + "/"):
            is_served_file = True

        if is_served_file and response.content_type and "text/html" in response.content_type:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https: ; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "img-src 'self' data: https: ; "
                "frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.socket.io; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src 'self' wss: ws:; "
                "img-src 'self'; "
                "frame-src 'self'; "
                "frame-ancestors 'self'"
            )

        # Headers an operator would otherwise have to remember to put in
        # their nginx config. Setting them here means a deployment that
        # forgets is still protected.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")

        # Session-dependent responses must never be reused for another
        # identity, by a shared cache or by the browser's back button.
        cached_never = (
            req.path.startswith("/gerrit_vis/graphs")
            or req.path.startswith("/login")
            or req.path == "/gerrit_vis/"
        )
        if private_prefix and req.path.startswith(private_prefix):
            cached_never = True
        if cached_never:
            response.headers["Cache-Control"] = "no-store, private, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    # Without these, an error renders Werkzeug's default page, which
    # announces the framework and -- with debug on -- an interactive
    # console. A caller that asked for JSON gets JSON back.
    def _error(code, message):
        if request.accept_mimetypes.best == "application/json":
            return jsonify(error=message, status=code), code
        return render_template("error.html", code=code, message=message), code

    @app.errorhandler(400)
    def _400(e):
        return _error(400, getattr(e, "description", "Bad request."))

    @app.errorhandler(401)
    def _401(e):
        return _error(401, "You need to sign in to do that.")

    @app.errorhandler(403)
    def _403(e):
        return _error(403, "Your account does not have access to that.")

    @app.errorhandler(404)
    def _404(e):
        return _error(404, "There is nothing at that address.")

    @app.errorhandler(429)
    def _429(e):
        return _error(429, "Too many requests. Wait a moment and try again.")

    @app.errorhandler(500)
    def _500(e):
        # Deliberately says nothing about what went wrong: the detail is
        # in the log, where it does not help an attacker.
        app.logger.exception("Unhandled error")
        return _error(500, "Something went wrong on our side.")

    @app.route("/")
    def index():
        return redirect(url_for("gerrit_vis.index"))

    # Optional app-shell for a proxied gerrit-dashboard: keeps the site
    # header constant and swaps only the body, so moving between the
    # tools does not feel like leaving the site. The dashboard is a
    # separate app (from llm_code_and_review_tools); this portal only
    # frames it, and nginx proxies it at DASHBOARD_APP_PATH.
    dash_prefix = app.config.get("DASHBOARD_PREFIX")
    if dash_prefix:
        app_path = app.config["DASHBOARD_APP_PATH"]

        @app.route(dash_prefix)
        @app.route(dash_prefix + "/")
        @app.route(dash_prefix + "/<path:subpath>")
        def dashboard_shell(subpath=""):
            # Only ever becomes a same-origin iframe src, Jinja-escaped;
            # the embedded app validates its own path segments.
            return render_template(
                "dashboard_shell.html",
                dash_src=app_path + subpath,
                dash_app_path=app_path,
                dash_prefix=dash_prefix,
            )

    return app
