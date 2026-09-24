"""Session authentication and role checks.

One account table, roles per account (see :mod:`portal.users`). The
session stores only the username; roles are resolved from the store on
each request, so revoking a role or deleting an account takes effect
immediately instead of waiting for the user to log out. The store is
cached in memory and re-read only when the file changes.
"""

import os
from functools import wraps
from urllib.parse import urlparse

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from portal.users import ROLE_ADMIN, ROLE_INTERNAL, load_users, verify

# (mtime, size) -> parsed store. Keyed by path so tests with different
# stores don't see each other's cache.
_cache = {}


def _users(app=None):
    app = app or current_app
    path = app.config["USERS_FILE"]
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None

    cached = _cache.get(path)
    if cached and cached[0] == stamp:
        return cached[1]

    data = load_users(path)
    _cache[path] = (stamp, data)
    return data


def _is_safe_redirect(target):
    """Allow only same-site paths as a ``next`` target.

    Rejects anything with a scheme or host, and anything that is not a
    single leading slash -- ``//evil.com`` is protocol-relative, and
    browsers normalise a backslash to a slash, so ``/\\evil.com`` would
    become one too.
    """
    if not target or not target.startswith("/"):
        return False
    if target.startswith("//") or target.startswith("/\\"):
        return False
    parsed = urlparse(target)
    return parsed.scheme == "" and parsed.netloc == ""


def current_user():
    """The logged-in username, or None.

    An account that has since been deleted counts as logged out.
    """
    username = session.get("auth_user")
    if not username:
        return None
    if username not in _users().get("users", {}):
        return None
    return username


def current_roles():
    username = current_user()
    if not username:
        return set()
    entry = _users()["users"][username]
    roles = set(entry.get("roles") or [])
    # admin implies internal: an operator granting admin should not also
    # have to remember to grant visibility of internal projects.
    if ROLE_ADMIN in roles:
        roles.add(ROLE_INTERNAL)
    return roles


def is_authenticated():
    return current_user() is not None


def has_role(role):
    return role in current_roles()


def is_admin():
    return has_role(ROLE_ADMIN)


def can_view_internal():
    """Who may see and act on graphs outside PORTAL_PUBLIC_PROJECT.

    Everyone else -- signed-in users without the role, and anonymous
    visitors -- must never see internal entries: not in the list, not by
    direct URL, not through autocomplete.
    """
    return has_role(ROLE_INTERNAL)


def _deny(status):
    """Send a browser to the login page, but answer an API caller plainly.

    A signed-in user who lacks a role is not helped by a login form --
    logging in again changes nothing -- so they get the status code.
    """
    if is_authenticated():
        from flask import abort

        abort(status)
    return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            return _deny(401)
        return f(*args, **kwargs)

    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin():
            return _deny(403)
        return f(*args, **kwargs)

    return decorated


def require_role(role):
    """Decorator factory gating a view on one role."""

    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not has_role(role):
                return _deny(403)
            return f(*args, **kwargs)

        return decorated

    return wrapper


def register_auth_routes(app):

    @app.route("/login", methods=["GET", "POST"], endpoint="auth.login")
    def login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")

            if verify(_users(app), username, password):
                # Drop anything the pre-login session carried before
                # granting privileges, so a fixed session id cannot be
                # reused to ride along with the new identity.
                session.clear()
                session["auth_user"] = username
                # permanent is what makes PERMANENT_SESSION_LIFETIME
                # apply; without it the cookie carries no expiry and a
                # captured one is valid until the key is rotated.
                session.permanent = True

                next_url = request.args.get("next", "")
                if not _is_safe_redirect(next_url):
                    next_url = url_for("gerrit_vis.index")
                return redirect(next_url)

            # One message for every failure: never reveal whether the
            # username exists.
            flash("Invalid credentials.", "error")

        return render_template("login.html")

    @app.route("/logout", methods=["GET", "POST"], endpoint="auth.logout")
    def logout():
        # Signing out happens only on POST: a GET logout can be triggered
        # by any page that can make your browser fetch a URL -- an <img>
        # tag is enough. Not dangerous, but not something a third-party
        # page should be able to do to you.
        #
        # GET still has to answer, though. The first version was POST-only
        # and a GET was a bare 405, which is what every bookmark, every
        # tab still showing an older navbar, and the /<private>/logout
        # redirect all hit. A GET now shows a one-button confirmation that
        # POSTs with a CSRF token, so the link works and the protection
        # stays.
        if request.method == "GET":
            if not is_authenticated():
                return redirect(url_for("gerrit_vis.index"))
            return render_template("logout.html")
        session.clear()
        return redirect(url_for("gerrit_vis.index"))

    @app.route("/_authcheck", endpoint="auth.authcheck")
    def authcheck():
        """Bare 200/401 gate for an nginx ``auth_request``.

        Without ``?role=`` it checks only that someone is signed in, and
        ``auth_request /_authcheck;`` is all that needs.

        To gate on a role, the role must reach this view in the query
        string -- and ``auth_request`` does not pass one: writing
        ``auth_request /_authcheck?role=x;`` returns a 500 for every
        visitor. Give the role its own internal location whose
        ``proxy_pass`` carries the query instead; see the README.

        Deliberately undecorated: the decorators redirect, and
        ``auth_request`` treats a 302 as a failure rather than a clean
        deny.
        """
        role = request.args.get("role", "")
        if role:
            return ("", 200) if has_role(role) else ("", 401)
        return ("", 200) if is_authenticated() else ("", 401)
