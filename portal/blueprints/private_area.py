"""An optional role-gated file area.

This ships empty and disabled. Setting ``PORTAL_PRIVATE_PREFIX`` mounts
a directory listing at that prefix, visible only to accounts holding
``PORTAL_PRIVATE_ROLE``. Leaving it unset means the blueprint is never
registered at all -- the portal has no private area, rather than a
hidden one.

It is deliberately plain: a listing and a file server. Anything richer
belongs behind ``/_authcheck``, which lets an operator gate a separate
service on the same login without this app knowing about it.
"""

import os

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from portal.auth import _is_safe_redirect, require_role


def make_private_blueprint(role):
    """Build the blueprint for the configured private area.

    ``role`` is bound at construction because the decorator needs it at
    definition time; the app reads it from config and passes it in.
    """
    bp = Blueprint("private", __name__, template_folder="../templates/private")

    @bp.route("/")
    @require_role(role)
    def index():
        content_dir = current_app.config["PRIVATE_DIR"]
        files = []
        if os.path.isdir(content_dir):
            for name in sorted(os.listdir(content_dir)):
                path = os.path.join(content_dir, name)
                if os.path.isfile(path):
                    files.append(
                        {
                            "name": name,
                            "size": os.path.getsize(path),
                            "mtime": os.path.getmtime(path),
                        }
                    )
        return render_template("private/index.html", files=files)

    @bp.route("/<path:filename>")
    @require_role(role)
    def serve_file(filename):
        # send_from_directory refuses to escape the root on its own; the
        # explicit check keeps the intent obvious and returns our own
        # status rather than a 404 that looks like a missing file.
        if ".." in filename or filename.startswith("/"):
            return "Forbidden", 403
        return send_from_directory(current_app.config["PRIVATE_DIR"], filename)

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        """Convenience redirect: the login lives at /login."""
        next_url = request.args.get("next", "") or url_for("private.index")
        if not _is_safe_redirect(next_url):
            next_url = url_for("private.index")
        return redirect(url_for("auth.login", next=next_url))

    @bp.route("/logout")
    def logout():
        return redirect(url_for("auth.logout"))

    return bp
