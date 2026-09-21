"""Authentication: login, redirects, roles, sessions."""


def test_main_login_success_redirects_to_index(client, login):
    resp = login("alice", "alicepw")
    assert resp.status_code == 302
    assert "/gerrit_vis/" in resp.headers["Location"]


def test_main_login_wrong_password(client, login):
    resp = login("alice", "wrongpw")
    assert resp.status_code == 200  # form re-rendered with flash
    assert b"Invalid credentials" in resp.data


def test_main_login_unknown_user(client, login):
    resp = login("nobody", "anything")
    assert resp.status_code == 200
    assert b"Invalid credentials" in resp.data


def test_session_stores_only_the_username(client, login_admin):
    """No roles in the cookie: they are resolved per request, so a
    revoked role cannot survive in a signed session."""
    login_admin()
    with client.session_transaction() as sess:
        assert sess.get("auth_user") == "root_user"
        assert "is_admin" not in sess
        assert "roles" not in sess


def test_admin_marker_shown_for_admin(client, login_admin):
    login_admin()
    assert b"(admin)" in client.get("/gerrit_vis/").data


def test_admin_marker_absent_for_plain_user(client, login):
    login("alice", "alicepw")
    body = client.get("/gerrit_vis/").data
    assert b"alice" in body
    assert b"(admin)" not in body


def test_logout_clears_session_and_redirects_to_index(client, login):
    login("alice", "alicepw")
    resp = client.post("/logout")
    assert resp.status_code == 302
    assert "/gerrit_vis/" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert "auth_user" not in sess


def test_login_next_param_local_path_honored(client):
    resp = client.post(
        "/login?next=/gerrit_vis/?q=foo",
        data={"username": "alice", "password": "alicepw"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/gerrit_vis/?q=foo")


def test_login_next_param_external_rejected(client):
    resp = client.post(
        "/login?next=http://evil.com/phish",
        data={"username": "alice", "password": "alicepw"},
    )
    assert resp.status_code == 302
    # Should redirect to default index, NOT to evil.com
    assert "evil.com" not in resp.headers["Location"]


def test_login_next_param_protocol_relative_rejected(client):
    resp = client.post(
        "/login?next=//evil.com/phish",
        data={"username": "alice", "password": "alicepw"},
    )
    assert resp.status_code == 302
    assert "evil.com" not in resp.headers["Location"]


def test_login_backslash_next_rejected(client):
    """Browsers normalise a backslash to a slash, so /\\evil.com would
    become protocol-relative. urlparse alone does not catch it."""
    resp = client.post(
        "/login?next=/\\evil.com/phish",
        data={"username": "alice", "password": "alicepw"},
    )
    assert resp.status_code == 302
    assert "evil.com" not in resp.headers["Location"]


def test_plain_user_sees_no_privileged_chrome(client, login):
    """A signed-in account with no roles gets no admin marker and no
    private-area link, even though the area is configured."""
    login("alice", "alicepw")
    body = client.get("/gerrit_vis/").data
    assert b"alice" in body
    assert b"(admin)" not in body
    assert b"Internal Files" not in body


def test_internal_user_sees_the_private_area_link(client, login_internal):
    body = client.get("/gerrit_vis/").data
    assert b"Internal Files" not in body
    login_internal()
    assert b"Internal Files" in client.get("/gerrit_vis/").data


def test_internal_user_is_not_an_admin(client, login_internal, graph_dir):
    """internal and admin are separate: seeing internal graphs does not
    confer the ability to schedule refreshes."""
    from portal.graph_store import add_entry

    add_entry(graph_dir, "4242")
    login_internal()
    resp = client.post("/gerrit_vis/graphs/schedule/4242", data={"interval": "24"})
    assert resp.status_code == 403


def test_admin_implies_internal(client, login_admin, graph_dir):
    """Granting admin should not require remembering to grant internal."""
    from portal.graph_store import add_entry

    add_entry(graph_dir, "5252", project="internal/example-project")
    with open(f"{graph_dir}/5252.html", "w") as f:
        f.write("<html>x</html>")
    login_admin()
    assert client.get("/gerrit_vis/graphs/5252.html").status_code == 200


# ---------- CSRF ----------


def test_post_without_a_token_is_rejected(client, graph_dir):
    """SameSite=Strict is one browser setting away from being the only
    thing stopping a cross-site POST. The token is the check the app can
    make itself."""
    from portal.graph_store import add_entry

    add_entry(graph_dir, "3131")
    client.post("/login", data={"username": "alice", "password": "alicepw"})
    resp = client.post("/gerrit_vis/graphs/delete/3131", data={}, csrf=False)
    assert resp.status_code == 400
    # And the entry survived.
    from portal.graph_store import get_entry

    assert get_entry(graph_dir, "3131") is not None


def test_post_with_a_wrong_token_is_rejected(client, graph_dir):
    from portal.graph_store import add_entry, get_entry

    add_entry(graph_dir, "3232")
    client.post("/login", data={"username": "alice", "password": "alicepw"})
    resp = client.post(
        "/gerrit_vis/graphs/delete/3232",
        data={"csrf_token": "not-the-right-token"},
        csrf=False,
    )
    assert resp.status_code == 400
    assert get_entry(graph_dir, "3232") is not None


def test_logout_requires_a_post(client, login):
    """A GET logout can be triggered by any page that makes your browser
    fetch a URL -- an <img> tag is enough."""
    login("alice", "alicepw")
    assert client.get("/logout").status_code == 405
    with client.session_transaction() as sess:
        assert sess.get("auth_user") == "alice"


def test_get_requests_never_need_a_token(client):
    assert client.get("/gerrit_vis/").status_code == 200
    assert client.get("/login").status_code == 200


def test_rendered_forms_carry_a_token(client):
    body = client.get("/login").data.decode()
    assert 'name="csrf_token"' in body


def test_session_has_an_expiry(client, login, app):
    """Without PERMANENT_SESSION_LIFETIME the cookie carries no expiry,
    so a captured one is valid until the secret key is rotated."""
    from datetime import timedelta

    assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(hours=12)
    resp = login("alice", "alicepw")
    cookie = resp.headers.get("Set-Cookie", "")
    assert "Expires=" in cookie or "Max-Age=" in cookie


def test_session_cookie_flags(client, login, app, monkeypatch):
    """HttpOnly and SameSite must be set. Secure is off in tests only
    because the test client speaks plain HTTP."""
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Strict"
    resp = login("alice", "alicepw")
    cookie = resp.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
