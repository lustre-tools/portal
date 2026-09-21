"""Configuration, built entirely from the environment.

Nothing here defaults to a deployment-specific path. A fresh checkout
runs against ``./data`` in the working directory, so ``flask run`` works
before anything has been installed or configured.

Every setting is read through :func:`build_config`, which ``create_app``
calls once at startup. Keeping it a function (rather than module-level
constants) means tests just set environment variables and build a new
app -- no module reloading.

Names are prefixed ``PORTAL_`` except the ``GERRIT_*`` credentials,
which deliberately keep the names the ``gerrit-cli`` and
``gerrit-dashboard`` tools already use, so one .env file can serve all
of them.
"""

import os
import secrets
from datetime import time as _time
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


def _env(name, default=None):
    """Read PORTAL_<name>, falling back to <name> for the shared vars."""
    value = os.environ.get(f"PORTAL_{name}")
    if value is None:
        value = os.environ.get(name, default)
    return value


def _env_int(name, default):
    raw = _env(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name, default):
    raw = _env(name)
    if raw is None or raw == "":
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_anchor(raw, fallback=_time(8, 0)):
    """Parse an "HH:MM" refresh anchor; fall back on anything malformed."""
    if not raw:
        return fallback
    try:
        hour, _, minute = raw.partition(":")
        return _time(int(hour), int(minute or 0))
    except (ValueError, TypeError):
        return fallback


def _resolve_timezone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _secret_key(testing):
    """The session signing key.

    Missing it in production is a configuration error, not something to
    paper over: a per-boot random key silently invalidates every session
    on restart, which looks like a bug in the app rather than a missing
    setting. Explicitly opt in to the ephemeral key for local work.
    """
    key = _env("SECRET_KEY")
    if key:
        return key
    if testing or _env("ALLOW_EPHEMERAL_SECRET"):
        return secrets.token_hex(32)
    raise RuntimeError(
        "PORTAL_SECRET_KEY is not set. Generate one with\n"
        "    python -c 'import secrets; print(secrets.token_hex(32))'\n"
        "and put it in your .env, or set PORTAL_ALLOW_EPHEMERAL_SECRET=1 "
        "for local development (sessions will not survive a restart)."
    )


def build_config(testing=False):
    """Return the Flask config mapping for one app instance."""
    load_dotenv()

    data_dir = os.path.abspath(_env("DATA_DIR", "data"))
    graph_dir = _env("GRAPH_DIR") or os.path.join(data_dir, "graphs")
    users_file = _env("USERS_FILE") or os.path.join(data_dir, "users.json")

    # The private area only exists if an operator configures a prefix.
    # Unset means the blueprint is never registered -- the portal has no
    # private area at all, rather than a hidden one.
    private_prefix = (_env("PRIVATE_PREFIX") or "").strip().rstrip("/")
    if private_prefix and not private_prefix.startswith("/"):
        private_prefix = "/" + private_prefix
    private_dir = _env("PRIVATE_DIR") or os.path.join(data_dir, "private")

    return {
        "SECRET_KEY": _secret_key(testing),
        # Session cookie hardening. SECURE is on unless explicitly
        # disabled, which is needed for plain-HTTP local development.
        "SESSION_COOKIE_SECURE": _env("COOKIE_SECURE", "1") not in ("0", "false", "False"),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Strict",
        # Sessions expire. Without this the cookie carries no expiry at
        # all, so a captured one stays valid until the secret key is
        # rotated -- which logs out everybody.
        "PERMANENT_SESSION_LIFETIME": timedelta(
            hours=max(1, _env_int("SESSION_LIFETIME_HOURS", 12))
        ),
        # How many reverse proxies sit in front. 0 means none, and the
        # app will not trust X-Forwarded-* at all; anything above that
        # and client addresses become visible (and forgeable by anyone
        # who can reach the app directly, hence the explicit count).
        "PROXY_HOPS": max(0, _env_int("PROXY_HOPS", 0)),
        # Extra origins allowed to open a WebSocket. None means
        # same-origin only, which is what engineio derives from the
        # request when no list is given -- and is what you want. Setting
        # "*" would let any page on the internet open a socket carrying
        # the visitor's cookies.
        "ALLOWED_ORIGINS": _env_list("ALLOWED_ORIGINS", ()) or None,
        # Branding
        "SITE_NAME": _env("SITE_NAME", "Lustre Tools"),
        # Paths
        "DATA_DIR": data_dir,
        "GRAPH_OUTPUT_DIR": os.path.abspath(graph_dir),
        "USERS_FILE": os.path.abspath(users_file),
        # Upstream services
        "GERRIT_URL": (_env("GERRIT_URL", "https://review.whamcloud.com")).rstrip("/"),
        "JIRA_URL": (_env("JIRA_URL", "https://jira.whamcloud.com")).rstrip("/"),
        # What counts as public. Anything else is "internal" and needs a
        # role to see. Changing this changes the whole visibility model,
        # so it is read in exactly one place.
        "PUBLIC_PROJECT": _env("PUBLIC_PROJECT", "fs/lustre-release"),
        "TICKET_PREFIX": (_env("TICKET_PREFIX", "LU")).upper(),
        "CI_VOTERS": _env_list("CI_VOTERS", ("maloo", "jenkins")),
        # Scheduling
        "TIMEZONE": _resolve_timezone(_env("TIMEZONE", "UTC")),
        "REFRESH_ANCHOR": _parse_anchor(_env("REFRESH_ANCHOR", "08:00")),
        # The optional private area (see the module docstring).
        "PRIVATE_PREFIX": private_prefix,
        "PRIVATE_LABEL": _env("PRIVATE_LABEL", "Private"),
        "PRIVATE_ROLE": _env("PRIVATE_ROLE", "internal"),
        "PRIVATE_DIR": os.path.abspath(private_dir),
        "PRIVATE_THEME": _env("PRIVATE_THEME", "purple"),
        # Optional app-shell for a proxied gerrit-dashboard. Unset means
        # no route and no nav entry; the dashboard is a separate app and
        # a portal without one should not advertise it.
        "DASHBOARD_PREFIX": (_env("DASHBOARD_PREFIX", "") or "").rstrip("/"),
        "DASHBOARD_APP_PATH": _env("DASHBOARD_APP_PATH", "/gerrit_dash_app/"),
        # The external `gc` CLI (gerrit-cli). Resolved on PATH by default
        # so a venv install just works; override to pin a specific one.
        "GC_BIN": _env("GC_BIN", "gc"),
        "GC_CWD": _env("GC_CWD") or None,
        # Concurrency caps, to spread load on the Gerrit server.
        "INTERACTIVE_RUN_CONCURRENCY": max(1, _env_int("INTERACTIVE_RUN_CONCURRENCY", 3)),
        "SCHEDULED_REFRESH_CONCURRENCY": max(1, _env_int("SCHEDULED_REFRESH_CONCURRENCY", 3)),
    }
