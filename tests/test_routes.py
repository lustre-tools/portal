"""Tests for HTTP routes: public access, auth-required, admin-only."""

from portal.graph_store import add_entry


def test_root_redirects_to_gerrit_vis(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/gerrit_vis/" in resp.headers["Location"]


def test_index_is_public(client):
    resp = client.get("/gerrit_vis/")
    assert resp.status_code == 200
    assert b"Gerrit Visualizer" in resp.data


def test_public_index_does_not_show_write_buttons(client, graph_dir):
    add_entry(graph_dir, "1234", name="test", labels=["pcc"])
    resp = client.get("/gerrit_vis/")
    body = resp.data
    assert b"accordion-btn" not in body
    assert b"btn-rerun" not in body
    assert b"btn-danger" not in body
    assert b"btn-edit" not in body
    assert b"btn-schedule" not in body
    # A login link is present for anonymous visitors
    assert b"Login" in body


def test_authenticated_index_shows_write_buttons(client, login, graph_dir):
    add_entry(graph_dir, "1234", name="test")
    login("alice", "alicepw")
    resp = client.get("/gerrit_vis/")
    body = resp.data
    assert b"accordion-btn" in body
    assert b"btn-edit" in body
    assert b"btn-rerun" in body
    assert b"btn-danger" in body
    # Schedule is admin-only
    assert b"btn-schedule" not in body


def test_admin_index_shows_schedule_button(client, login_admin, graph_dir):
    add_entry(graph_dir, "1234", name="test")
    login_admin()
    resp = client.get("/gerrit_vis/")
    assert b"btn-schedule" in resp.data


def test_serve_graph_is_public(client, graph_dir):
    import os

    add_entry(graph_dir, "5555")
    with open(os.path.join(graph_dir, "5555.html"), "w") as f:
        f.write("<html>graph</html>")
    resp = client.get("/gerrit_vis/graphs/5555.html")
    assert resp.status_code == 200
    assert b"<html>graph</html>" in resp.data


def test_serve_graph_path_traversal_blocked(client, graph_dir):
    resp = client.get("/gerrit_vis/graphs/../../etc/passwd")
    # 404 or 403 or 308 redirect — all are safe; just verify nothing leaks
    assert resp.status_code in (308, 404, 403)


def test_delete_requires_auth(client, graph_dir):
    add_entry(graph_dir, "1234")
    resp = client.post("/gerrit_vis/graphs/delete/1234", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    # Entry not deleted
    from portal.graph_store import get_entry

    assert get_entry(graph_dir, "1234") is not None


def test_delete_works_for_authenticated(client, login, graph_dir):
    add_entry(graph_dir, "1234")
    login("alice", "alicepw")
    resp = client.post("/gerrit_vis/graphs/delete/1234", follow_redirects=False)
    assert resp.status_code == 302
    from portal.graph_store import get_entry

    assert get_entry(graph_dir, "1234") is None


def test_delete_rejects_non_numeric(client, login, graph_dir):
    login("alice", "alicepw")
    resp = client.post("/gerrit_vis/graphs/delete/abc")
    assert resp.status_code == 400


def test_schedule_requires_admin(client, login, graph_dir):
    add_entry(graph_dir, "1234")
    login("alice", "alicepw")  # not admin
    resp = client.post(
        "/gerrit_vis/graphs/schedule/1234",
        data={"interval": "24"},
        follow_redirects=False,
    )
    # A signed-in user who simply lacks the role gets 403, not a login
    # form: signing in again would change nothing.
    assert resp.status_code == 403


def test_schedule_redirects_anonymous_to_login(client, graph_dir):
    add_entry(graph_dir, "1234")
    resp = client.post(
        "/gerrit_vis/graphs/schedule/1234",
        data={"interval": "24"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_schedule_works_for_admin(client, login_admin, graph_dir):
    add_entry(graph_dir, "1234")
    login_admin()
    resp = client.post(
        "/gerrit_vis/graphs/schedule/1234",
        data={"interval": "24"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    from portal.graph_store import get_entry

    entry = get_entry(graph_dir, "1234")
    assert entry["refresh_interval_hours"] == 24


def test_schedule_rejects_invalid_interval(client, login_admin, graph_dir):
    add_entry(graph_dir, "1234")
    login_admin()
    resp = client.post(
        "/gerrit_vis/graphs/schedule/1234",
        data={"interval": "999"},  # over the 168 max
    )
    assert resp.status_code == 400


def test_metadata_endpoint_requires_auth(client, graph_dir):
    add_entry(graph_dir, "1234", name="orig")
    resp = client.post(
        "/gerrit_vis/graphs/metadata/1234",
        data={"name": "hacked", "labels": "evil"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    from portal.graph_store import get_entry

    assert get_entry(graph_dir, "1234")["name"] == "orig"


def test_metadata_endpoint_updates_name_and_labels(client, login, graph_dir):
    add_entry(graph_dir, "1234", name="orig", subject="LU-1 foo", labels=["old"])
    login("alice", "alicepw")
    resp = client.post(
        "/gerrit_vis/graphs/metadata/1234",
        data={"name": "newname", "labels": "alpha, BETA, alpha"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    from portal.graph_store import get_entry

    entry = get_entry(graph_dir, "1234")
    assert entry["name"] == "newname"
    # Labels normalized: lowercased, deduped, sanitized
    assert entry["labels"] == ["alpha", "beta"]


def test_metadata_endpoint_preserves_generated_at(client, login, graph_dir):
    add_entry(graph_dir, "1234", name="x")
    from portal.graph_store import get_entry

    original_ts = get_entry(graph_dir, "1234")["generated_at"]
    login("alice", "alicepw")
    client.post(
        "/gerrit_vis/graphs/metadata/1234",
        data={"name": "y", "labels": ""},
    )
    assert get_entry(graph_dir, "1234")["generated_at"] == original_ts


def test_metadata_endpoint_404_on_missing(client, login, graph_dir):
    login("alice", "alicepw")
    resp = client.post(
        "/gerrit_vis/graphs/metadata/9999",
        data={"name": "x"},
    )
    assert resp.status_code == 404


def _listed(body):
    """The graphs a rendered list shows, in order. Each is one <tbody>."""
    import re

    return re.findall(r'<tbody class="entry" id="g-([^"]+)"', body)


def test_label_filter_url_param(client, graph_dir):
    add_entry(graph_dir, "1", labels=["alpha"])
    add_entry(graph_dir, "2", labels=["beta"])
    resp = client.get("/gerrit_vis/?label=alpha")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "filter-pill" in body
    assert _listed(body) == ["1"]


def test_delete_preserves_filter_when_results_remain(client, login, graph_dir):
    add_entry(graph_dir, "1", labels=["alpha"])
    add_entry(graph_dir, "2", labels=["alpha"])
    login("alice", "alicepw")
    resp = client.post(
        "/gerrit_vis/graphs/delete/1",
        data={"label": "alpha"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # Filter preserved because entry 2 still matches
    assert "label=alpha" in resp.headers["Location"]


def test_delete_drops_filter_when_results_empty(client, login, graph_dir):
    add_entry(graph_dir, "1", labels=["unique-label"])
    login("alice", "alicepw")
    resp = client.post(
        "/gerrit_vis/graphs/delete/1",
        data={"label": "unique-label"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # No more entries with that label, so filter is dropped
    assert "label=" not in resp.headers["Location"]


def test_delete_preserves_query_when_results_remain(client, login, graph_dir):
    add_entry(graph_dir, "1", name="foo")
    add_entry(graph_dir, "2", name="foo bar")
    login("alice", "alicepw")
    resp = client.post(
        "/gerrit_vis/graphs/delete/1",
        data={"q": "foo"},
        follow_redirects=False,
    )
    assert "q=foo" in resp.headers["Location"]


def test_label_filter_combined(client, graph_dir):
    add_entry(graph_dir, "1", labels=["alpha"])
    add_entry(graph_dir, "2", labels=["alpha", "beta"])
    resp = client.get("/gerrit_vis/?label=alpha&label=beta")
    assert _listed(resp.data.decode()) == ["2"]


def test_security_headers_present(client):
    resp = client.get("/gerrit_vis/")
    assert "Content-Security-Policy" in resp.headers
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    # The app frames its own dashboard shell, so 'self' rather than 'none'.
    assert "frame-ancestors 'self'" in csp


def test_csp_permissive_for_graph_html(client, graph_dir):
    import os

    add_entry(graph_dir, "1234")
    with open(os.path.join(graph_dir, "1234.html"), "w") as f:
        f.write("<html></html>")
    resp = client.get("/gerrit_vis/graphs/1234.html")
    csp = resp.headers["Content-Security-Policy"]
    # Graph HTML files need 'unsafe-inline' for embedded scripts
    assert "'unsafe-inline'" in csp


def test_private_area_redirects_anonymous_to_login(client):
    resp = client.get("/private/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_private_area_opens_for_the_configured_role(client, login_internal):
    login_internal()
    assert client.get("/private/").status_code == 200


def test_private_area_forbidden_without_the_role(client, login):
    """Signed in but without the role: 403, not a login redirect."""
    login("alice", "alicepw")
    assert client.get("/private/", follow_redirects=False).status_code == 403


def test_private_area_absent_unless_configured(bare_app):
    """The default portal has no private area at all -- not a hidden one."""
    c = bare_app.test_client()
    assert c.get("/private/").status_code == 404
    assert "private" not in set(bare_app.blueprints)


def test_authcheck_401_when_signed_out(client):
    """nginx auth_request gate: a bare 401, never a redirect."""
    resp = client.get("/_authcheck?role=internal")
    assert resp.status_code == 401
    assert resp.data == b""


def test_authcheck_401_without_the_role(client, login):
    login("alice", "alicepw")
    assert client.get("/_authcheck?role=internal").status_code == 401


def test_authcheck_200_with_the_role(client, login_internal):
    login_internal()
    resp = client.get("/_authcheck?role=internal")
    assert resp.status_code == 200
    assert resp.data == b""


def test_authcheck_without_a_role_just_checks_sign_in(client, login):
    assert client.get("/_authcheck").status_code == 401
    login("alice", "alicepw")
    assert client.get("/_authcheck").status_code == 200


def test_authcheck_works_for_an_arbitrary_role(client, login_admin):
    """An operator can gate any service on any role they invent."""
    assert client.get("/_authcheck?role=nonexistent-role").status_code == 401
    login_admin()
    assert client.get("/_authcheck?role=nonexistent-role").status_code == 401
    assert client.get("/_authcheck?role=admin").status_code == 200


def test_static_urls_carry_a_content_hash(client, app):
    """A deploy that changes the stylesheet must change its URL, or
    browsers keep the cached copy and show new pages with old styles."""
    import hashlib
    import os
    import re

    body = client.get("/gerrit_vis/").data.decode()
    m = re.search(r'href="/static/style\.css\?v=([0-9a-f]{10})"', body)
    assert m, "the stylesheet link carries no version"
    with open(os.path.join(app.static_folder, "style.css"), "rb") as f:
        assert m.group(1) == hashlib.sha256(f.read()).hexdigest()[:10]
    # And the versioned URL is still served.
    assert client.get(f"/static/style.css?v={m.group(1)}").status_code == 200
