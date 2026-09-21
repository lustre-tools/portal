"""Pytest fixtures.

Every test gets its own temp data directory and account store, and a
freshly built app. Configuration comes from the environment, so there is
no module reloading to work around -- set a variable, build an app.

The three accounts model the three interesting identities:

``alice``     signed in, no roles      -- sees only the public project
``root_user`` admin (implies internal) -- can also schedule refreshes
``insider``   internal only            -- sees internal, cannot schedule
"""

import json
import os
import sys

import pytest
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash

from portal.csrf import UNSAFE_METHODS

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PASSWORDS = {
    "alice": "alicepw",
    "root_user": "adminpw",
    "insider": "insiderpw",
}


@pytest.fixture
def temp_dirs(tmp_path):
    graph_dir = tmp_path / "graphs"
    private_dir = tmp_path / "private"
    graph_dir.mkdir()
    private_dir.mkdir()

    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "version": 2,
                "users": {
                    "alice": {
                        "password_hash": generate_password_hash(PASSWORDS["alice"]),
                        "roles": [],
                    },
                    "root_user": {
                        "password_hash": generate_password_hash(PASSWORDS["root_user"]),
                        "roles": ["admin"],
                    },
                    "insider": {
                        "password_hash": generate_password_hash(PASSWORDS["insider"]),
                        "roles": ["internal"],
                    },
                },
            }
        )
    )

    return {
        "graph_dir": str(graph_dir),
        "private_dir": str(private_dir),
        "users_file": str(users_file),
    }


@pytest.fixture
def app_env(temp_dirs, monkeypatch):
    """Base environment shared by every app fixture."""
    monkeypatch.setenv("PORTAL_SECRET_KEY", "test-secret-key-32bytes-fixed")
    monkeypatch.setenv("PORTAL_GRAPH_DIR", temp_dirs["graph_dir"])
    monkeypatch.setenv("PORTAL_USERS_FILE", temp_dirs["users_file"])
    monkeypatch.setenv("PORTAL_PUBLIC_PROJECT", "fs/lustre-release")
    # The test client speaks plain HTTP, so a Secure cookie would be dropped.
    monkeypatch.setenv("PORTAL_COOKIE_SECURE", "0")
    return temp_dirs


@pytest.fixture
def app(app_env, monkeypatch):
    """An app with the private area enabled, mirroring a full deployment."""
    monkeypatch.setenv("PORTAL_PRIVATE_PREFIX", "/private")
    monkeypatch.setenv("PORTAL_PRIVATE_DIR", app_env["private_dir"])
    monkeypatch.setenv("PORTAL_PRIVATE_LABEL", "Internal Files")

    from portal.app import create_app
    from portal.auth import _cache

    _cache.clear()
    return create_app(testing=True)


@pytest.fixture
def bare_app(app_env):
    """An app with nothing optional configured -- the public default."""
    from portal.app import create_app
    from portal.auth import _cache

    _cache.clear()
    return create_app(testing=True)


class CSRFClient(FlaskClient):
    """A test client that carries a CSRF token on unsafe requests.

    A real browser gets its token from the rendered form. Rather than
    parsing HTML in every test, seed the session with one and send it.
    Pass ``csrf=False`` to post without -- that is how the CSRF tests
    check the rejection path.
    """

    def _token(self):
        with self.session_transaction() as sess:
            token = sess.get("_csrf_token")
            if not token:
                token = "test-csrf-token"
                sess["_csrf_token"] = token
        return token

    def open(self, *args, **kwargs):
        send_csrf = kwargs.pop("csrf", True)
        method = (kwargs.get("method") or "GET").upper()
        if send_csrf and method in UNSAFE_METHODS:
            token = self._token()
            data = kwargs.get("data")
            if isinstance(data, dict):
                data.setdefault("csrf_token", token)
            else:
                kwargs.setdefault("headers", {})
                kwargs["headers"].setdefault("X-CSRF-Token", token)
        return super().open(*args, **kwargs)


@pytest.fixture
def client(app):
    app.test_client_class = CSRFClient
    return app.test_client()


@pytest.fixture
def login(client):
    """Log in as one of the fixture accounts."""

    def _login(username="alice", password=None):
        return client.post(
            "/login",
            data={
                "username": username,
                "password": password or PASSWORDS.get(username, ""),
            },
            follow_redirects=False,
        )

    return _login


@pytest.fixture
def login_admin(login):
    def _login_admin():
        return login("root_user")

    return _login_admin


@pytest.fixture
def login_internal(login):
    def _login_internal():
        return login("insider")

    return _login_internal


@pytest.fixture
def fake_gen(monkeypatch):
    """Run the graph tool without invoking gc or writing a real graph.

    Yields a dict the test can mutate to control what classification
    reports back: ``project`` and ``subject``.
    """
    from portal.tools import gc_graph
    from tests.helpers import FakePopen

    monkeypatch.setattr(gc_graph.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(gc_graph, "extract_stats_from_html", lambda path, voters=None: None)

    state = {"project": "fs/lustre-release", "subject": "LU-19921 subject"}

    def _classify(path, public_project, anchor_id=None):
        return state["project"], state["subject"]

    monkeypatch.setattr(gc_graph, "classify_graph_project", _classify)
    return state


@pytest.fixture
def graph_dir(temp_dirs):
    return temp_dirs["graph_dir"]


@pytest.fixture
def private_dir(temp_dirs):
    return temp_dirs["private_dir"]
