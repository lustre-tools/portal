"""CSRF protection for state-changing requests.

``SameSite=Strict`` on the session cookie stops the common case, but it
is one browser setting away from being the only thing between a
cross-site POST and a deleted graph, and it does nothing for a browser
that does not honour it. A token adds the check the application itself
can make.

Deliberately small and dependency-free: a random token per session, a
hidden field rendered by ``{{ csrf_field() }}``, and a
``before_request`` hook that rejects any unsafe method without a
matching token.
"""

import hmac
import secrets

from flask import abort, request, session
from markupsafe import Markup

#: Methods that must carry a token. GET/HEAD/OPTIONS are expected to be
#: side-effect free; a view that changes state on GET is the bug.
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_SESSION_KEY = "_csrf_token"
FIELD_NAME = "csrf_token"
HEADER_NAME = "X-CSRF-Token"


def current_token():
    """The session's token, minted on first use."""
    token = session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_SESSION_KEY] = token
    return token


def csrf_field():
    """A hidden input carrying the token, for use in a form."""
    return Markup(f'<input type="hidden" name="{FIELD_NAME}" value="{current_token()}">')


def _submitted_token():
    return request.form.get(FIELD_NAME) or request.headers.get(HEADER_NAME) or ""


def validate():
    """Reject an unsafe request whose token is missing or wrong."""
    expected = session.get(_SESSION_KEY)
    submitted = _submitted_token()
    # compare_digest rather than ==: the comparison itself should not
    # leak how much of the token was correct.
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        abort(400, description="Invalid or missing CSRF token.")


def init_csrf(app):
    @app.before_request
    def _check():
        if request.method not in UNSAFE_METHODS:
            return None
        if getattr(app.view_functions.get(request.endpoint), "_csrf_exempt", False):
            return None
        validate()
        return None

    @app.context_processor
    def _inject():
        return {"csrf_field": csrf_field, "csrf_token": current_token}


def csrf_exempt(view):
    """Mark a view as not requiring a token.

    Nothing uses this today. It exists so that adding, say, a webhook
    receiver later is an explicit decision at the view rather than a
    hole poked in the hook.
    """
    view._csrf_exempt = True
    return view
