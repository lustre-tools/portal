"""The internal-project authorization model.

Anonymous visitors and signed-in accounts without the internal role MUST NOT:
- see internal entries in the list
- reach internal graph HTML by URL (404, identical to "no such graph")
- see internal labels in autocomplete
- delete, metadata-edit or rerun internal entries

Accounts holding ``internal`` -- directly or via ``admin`` -- must be able
to do all of those, on public and internal entries alike. A public graph
must never get --cross-project.
"""

import os

from portal.graph_store import (
    add_entry,
    entry_project,
    get_entry,
    is_internal_entry,
)

PUBLIC_PROJECT = "fs/lustre-release"


# ---------- helpers ----------

INTERNAL_PROJECT = "internal/example-project"


def _seed(graph_dir):
    """Two entries: one public, one internal."""
    add_entry(graph_dir, "1111", name="public graph", labels=["pub"], project=PUBLIC_PROJECT)
    add_entry(graph_dir, "2222", name="internal graph", labels=["secret"], project=INTERNAL_PROJECT)
    # Write matching HTML files
    for n in ("1111", "2222"):
        with open(os.path.join(graph_dir, f"{n}.html"), "w") as f:
            f.write(f"<html>graph {n}</html>")


def _login(client, user, pw):
    return client.post("/login", data={"username": user, "password": pw})


def _login_admin(client):
    return _login(client, "root_user", "adminpw")


def _login_internal(client):
    """An account with the internal role but not admin."""
    return _login(client, "insider", "insiderpw")


# ---------- graph_store helpers ----------


def test_entry_project_defaults_to_public(graph_dir):
    """Legacy entries (no project field) are treated as public."""
    add_entry(graph_dir, "9999", name="legacy")
    e = get_entry(graph_dir, "9999")
    # The add_entry path doesn't set the field by default
    assert "project" not in e
    assert entry_project(e, PUBLIC_PROJECT) == PUBLIC_PROJECT
    assert not is_internal_entry(e, PUBLIC_PROJECT)


def test_add_entry_persists_project(graph_dir):
    add_entry(graph_dir, "8888", project=INTERNAL_PROJECT)
    e = get_entry(graph_dir, "8888")
    assert e["project"] == INTERNAL_PROJECT
    assert is_internal_entry(e, PUBLIC_PROJECT)


def test_add_entry_preserves_project_on_replace(graph_dir):
    """If a second add_entry omits project, the existing one is kept."""
    add_entry(graph_dir, "8888", project=INTERNAL_PROJECT)
    add_entry(graph_dir, "8888", name="renamed")  # no project arg
    e = get_entry(graph_dir, "8888")
    assert e["project"] == INTERNAL_PROJECT


# ---------- list visibility ----------


def test_public_user_does_not_see_internal_in_list(client, graph_dir):
    _seed(graph_dir)
    resp = client.get("/gerrit_vis/")
    body = resp.data.decode()
    assert "1111" in body
    assert "2222" not in body


def test_lustre_user_does_not_see_internal_in_list(client, graph_dir):
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    resp = client.get("/gerrit_vis/")
    body = resp.data.decode()
    assert "1111" in body
    assert "2222" not in body


def test_admin_sees_internal_in_list(client, graph_dir):
    _seed(graph_dir)
    _login_admin(client)
    resp = client.get("/gerrit_vis/")
    body = resp.data.decode()
    assert "1111" in body
    assert "2222" in body
    # Internal badge appears
    assert "internal-badge" in body


def test_insider_sees_internal_in_list(client, graph_dir):
    _seed(graph_dir)
    _login_internal(client)
    resp = client.get("/gerrit_vis/")
    body = resp.data.decode()
    assert "1111" in body
    assert "2222" in body


# ---------- direct URL access (this is the critical "even with the URL" rule) ----------


def test_public_user_404s_on_internal_graph_url(client, graph_dir):
    _seed(graph_dir)
    resp = client.get("/gerrit_vis/graphs/2222.html")
    assert resp.status_code == 404
    # Public file still works
    assert client.get("/gerrit_vis/graphs/1111.html").status_code == 200


def test_lustre_user_404s_on_internal_graph_url(client, graph_dir):
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    resp = client.get("/gerrit_vis/graphs/2222.html")
    assert resp.status_code == 404


def test_admin_can_access_internal_graph_url(client, graph_dir):
    _seed(graph_dir)
    _login_admin(client)
    resp = client.get("/gerrit_vis/graphs/2222.html")
    assert resp.status_code == 200
    assert b"graph 2222" in resp.data


def test_insider_can_access_internal_graph_url(client, graph_dir):
    _seed(graph_dir)
    _login_internal(client)
    resp = client.get("/gerrit_vis/graphs/2222.html")
    assert resp.status_code == 200


# ---------- autocomplete label leak ----------


def test_internal_labels_not_in_autocomplete_for_lustre(client, graph_dir):
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    resp = client.get("/gerrit_vis/")
    body = resp.data.decode()
    assert '"pub"' in body or "pub" in body
    # "secret" only appears on the internal entry; lustre must not see it
    assert "secret" not in body


def test_internal_labels_in_autocomplete_for_admin(client, graph_dir):
    _seed(graph_dir)
    _login_admin(client)
    resp = client.get("/gerrit_vis/")
    body = resp.data.decode()
    assert "secret" in body


# ---------- delete authorization ----------


def test_lustre_cannot_delete_internal_entry(client, graph_dir):
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    resp = client.post("/gerrit_vis/graphs/delete/2222")
    assert resp.status_code == 404
    # Entry still exists
    assert get_entry(graph_dir, "2222") is not None


def test_admin_can_delete_internal_entry(client, graph_dir):
    _seed(graph_dir)
    _login_admin(client)
    resp = client.post("/gerrit_vis/graphs/delete/2222", follow_redirects=False)
    assert resp.status_code == 302
    assert get_entry(graph_dir, "2222") is None


def test_ddn_can_delete_internal_entry(client, graph_dir):
    _seed(graph_dir)
    _login_internal(client)
    resp = client.post("/gerrit_vis/graphs/delete/2222", follow_redirects=False)
    assert resp.status_code == 302
    assert get_entry(graph_dir, "2222") is None


def test_ddn_can_delete_public_entry(client, graph_dir):
    """The internal role also confers full rights on public graphs."""
    _seed(graph_dir)
    _login_internal(client)
    resp = client.post("/gerrit_vis/graphs/delete/1111", follow_redirects=False)
    assert resp.status_code == 302
    assert get_entry(graph_dir, "1111") is None


# ---------- metadata authorization ----------


def test_lustre_cannot_edit_metadata_of_internal_entry(client, graph_dir):
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    resp = client.post(
        "/gerrit_vis/graphs/metadata/2222",
        data={"name": "pwned", "labels": "leaked"},
    )
    assert resp.status_code == 404
    e = get_entry(graph_dir, "2222")
    assert e["name"] == "internal graph"


def test_ddn_can_edit_metadata_of_internal_entry(client, graph_dir):
    _seed(graph_dir)
    _login_internal(client)
    resp = client.post(
        "/gerrit_vis/graphs/metadata/2222",
        data={"name": "renamed", "labels": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    e = get_entry(graph_dir, "2222")
    assert e["name"] == "renamed"


def test_ddn_can_edit_metadata_of_public_entry(client, graph_dir):
    _seed(graph_dir)
    _login_internal(client)
    resp = client.post(
        "/gerrit_vis/graphs/metadata/1111",
        data={"name": "insider-renamed", "labels": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    e = get_entry(graph_dir, "1111")
    assert e["name"] == "insider-renamed"


# ---------- label filter visibility ----------


def test_lustre_label_filter_does_not_show_internal_match(client, graph_dir):
    """Even searching for the internal label, the lustre user must not see
    the internal row."""
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    resp = client.get("/gerrit_vis/?label=secret")
    body = resp.data.decode()
    assert "2222" not in body


# ---------- the internal role also gets the write UI ----------


def test_insider_sees_write_buttons(client, graph_dir):
    _seed(graph_dir)
    _login_internal(client)
    resp = client.get("/gerrit_vis/")
    assert b"accordion-btn" in resp.data  # New Graph button
    assert b"btn-edit" in resp.data
    assert b"btn-rerun" in resp.data
    assert b"btn-danger" in resp.data  # Delete


# ---------- nav labels ----------


def test_navbar_shows_insider(client, graph_dir):
    _login_internal(client)
    resp = client.get("/gerrit_vis/")
    assert b"insider" in resp.data


def test_navbar_shows_admin_marker(client, graph_dir):
    _login_admin(client)
    resp = client.get("/gerrit_vis/")
    assert b"(admin)" in resp.data


# ---------- defense in depth: rerun_data doesn't leak internal entries ----------


def test_rerun_data_excludes_internal_for_lustre(client, graph_dir):
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    resp = client.get("/gerrit_vis/")
    body = resp.data.decode()
    # RERUN_DATA is rendered as JSON in the page; internal entry must not be keyed
    assert '"2222"' not in body
    assert "internal graph" not in body


def test_serve_graph_path_traversal_still_blocked(client, graph_dir):
    """Make sure the new auth check didn't open a hole."""
    resp = client.get("/gerrit_vis/graphs/../../etc/passwd")
    assert resp.status_code in (308, 404, 403)


def test_serve_graph_index_json_not_served(client, graph_dir):
    """The index.json file MUST NOT be served via /gerrit_vis/graphs/.
    It would leak internal entries to anyone who knows the URL."""
    _seed(graph_dir)
    resp = client.get("/gerrit_vis/graphs/index.json")
    assert resp.status_code == 404
    # Same after login
    _login(client, "alice", "alicepw")
    assert client.get("/gerrit_vis/graphs/index.json").status_code == 404
    # Even admin can't reach it through this route
    _login_admin(client)
    assert client.get("/gerrit_vis/graphs/index.json").status_code == 404


def test_serve_graph_random_files_not_served(client, graph_dir):
    """Non <digits>.html filenames in the graph dir must not be served."""
    with open(os.path.join(graph_dir, "secret.txt"), "w") as f:
        f.write("hush")
    assert client.get("/gerrit_vis/graphs/secret.txt").status_code == 404


def test_serve_graph_orphan_html_not_served(client, graph_dir):
    """An HTML file present on disk but with NO index entry must not be
    served. Otherwise, an internal graph file accidentally left behind
    after deletion could still be viewed."""
    with open(os.path.join(graph_dir, "99999.html"), "w") as f:
        f.write("<html>orphan</html>")
    assert client.get("/gerrit_vis/graphs/99999.html").status_code == 404


# ---------- Enumeration oracle fixes ----------


def test_delete_nonexistent_and_internal_both_404_for_lustre(client, graph_dir):
    """Both 'no such entry' and 'exists-but-internal' must return identical
    404s so the response cannot be used to enumerate internal change numbers."""
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    r_missing = client.post("/gerrit_vis/graphs/delete/77777777")
    r_internal = client.post("/gerrit_vis/graphs/delete/2222")
    assert r_missing.status_code == 404
    assert r_internal.status_code == 404


def test_metadata_nonexistent_and_internal_both_404_for_lustre(client, graph_dir):
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    r_missing = client.post("/gerrit_vis/graphs/metadata/77777777", data={"name": "x"})
    r_internal = client.post("/gerrit_vis/graphs/metadata/2222", data={"name": "x"})
    assert r_missing.status_code == 404
    assert r_internal.status_code == 404
    # Bodies must be byte-identical so they don't form an oracle
    assert r_missing.data == r_internal.data


def test_delete_existing_public_still_succeeds_for_lustre(client, graph_dir):
    """The 404-everywhere fix must NOT break legitimate public deletes."""
    _seed(graph_dir)
    _login(client, "alice", "alicepw")
    r = client.post("/gerrit_vis/graphs/delete/1111", follow_redirects=False)
    assert r.status_code == 302
    assert get_entry(graph_dir, "1111") is None


# ---------- Schedule interval allowlist ----------


def test_schedule_rejects_non_canonical_intervals(client, login_admin, graph_dir):
    """Intervals are restricted to the dropdown values."""
    add_entry(graph_dir, "1234")
    login_admin()
    for bad in ("1", "5", "7", "13", "100", "150"):
        resp = client.post("/gerrit_vis/graphs/schedule/1234", data={"interval": bad})
        assert resp.status_code == 400, f"interval={bad} should be rejected"


def test_schedule_accepts_all_canonical_intervals(client, login_admin, graph_dir):
    add_entry(graph_dir, "1234")
    login_admin()
    for good in ("0", "6", "12", "24", "48", "168"):
        resp = client.post(
            "/gerrit_vis/graphs/schedule/1234",
            data={"interval": good},
            follow_redirects=False,
        )
        assert resp.status_code == 302, f"interval={good} should be accepted"


def test_graph_responses_set_no_store_cache_control(client, graph_dir):
    """Graph HTML must not be cached, or a browser could replay an
    internal graph from cache after the session ends."""
    _seed(graph_dir)
    _login_admin(client)
    resp = client.get("/gerrit_vis/graphs/2222.html")
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "no-store" in cc and "private" in cc
    resp2 = client.get("/gerrit_vis/")
    assert "no-store" in resp2.headers.get("Cache-Control", "")


# ---------- Logout ----------


def test_logout_clears_the_session(client):
    _login_internal(client)
    with client.session_transaction() as s:
        assert s.get("auth_user") == "insider"
    client.post("/logout")
    with client.session_transaction() as s:
        assert "auth_user" not in s


def test_login_replaces_a_previous_identity(client):
    """Signing in as someone else must not leave the old roles behind."""
    _login_internal(client)
    _login(client, "alice", "alicepw")
    resp = client.get("/gerrit_vis/")
    body = resp.data.decode()
    assert "alice" in body
    assert "insider" not in body


def test_revoking_a_role_takes_effect_without_re_login(client, graph_dir, temp_dirs):
    """Roles are resolved per request, so a revocation is immediate.

    A login-time snapshot would leave the old session privileged until
    the user happened to log out.
    """
    _seed(graph_dir)
    _login_internal(client)
    assert b"2222" in client.get("/gerrit_vis/").data

    import json

    path = temp_dirs["users_file"]
    with open(path) as f:
        data = json.load(f)
    data["users"]["insider"]["roles"] = []
    with open(path, "w") as f:
        json.dump(data, f)

    assert b"2222" not in client.get("/gerrit_vis/").data


def test_deleting_an_account_logs_that_session_out(client, temp_dirs):
    _login_internal(client)
    import json

    path = temp_dirs["users_file"]
    with open(path) as f:
        data = json.load(f)
    del data["users"]["insider"]
    with open(path, "w") as f:
        json.dump(data, f)

    resp = client.get("/gerrit_vis/")
    assert b"insider" not in resp.data
    assert b"Login" in resp.data


# ---------- Login does not leak which usernames exist ----------


def test_login_hashes_even_for_an_unknown_user(client, monkeypatch):
    """The dummy-hash mitigation: an unknown username must still cost one
    hash comparison, or response time reveals which accounts are real.
    """
    from portal import users as users_mod

    calls = []
    real = users_mod.check_password_hash

    def counting(stored, password):
        calls.append(stored)
        return real(stored, password)

    monkeypatch.setattr(users_mod, "check_password_hash", counting)

    client.post("/login", data={"username": "nobody-here", "password": "x"})
    assert len(calls) == 1, "unknown user must still trigger a hash check"
    assert calls[0] == users_mod._DUMMY_HASH

    calls.clear()
    client.post("/login", data={"username": "alice", "password": "wrong"})
    assert len(calls) == 1, "known user with a bad password: same work"


def test_login_failure_message_is_identical_either_way(client):
    unknown = client.post("/login", data={"username": "nobody-here", "password": "x"})
    known = client.post("/login", data={"username": "alice", "password": "wrong"})
    assert unknown.status_code == known.status_code
    assert unknown.data == known.data


# ---------- WebSocket behaviour ----------


def _socket(app, client):
    """A Socket.IO test client sharing the HTTP client's session."""
    from portal.app import socketio

    return socketio.test_client(app, flask_test_client=client)


def test_websocket_refuses_an_anonymous_handshake(app, client):
    """An anonymous caller does not get a socket at all."""
    sock = _socket(app, client)
    assert not sock.is_connected()


def test_deleting_an_account_stops_its_open_socket(app, client, login, temp_dirs):
    """Every event re-checks, against the live account store.

    A WebSocket only carries cookies during the handshake, so the
    identity on an open socket is fixed for its lifetime -- but whether
    that identity still exists is looked up on each event. Deleting the
    account stops an already-connected socket dead.
    """
    import json

    login()
    sock = _socket(app, client)
    assert sock.is_connected()

    path = temp_dirs["users_file"]
    with open(path) as f:
        data = json.load(f)
    del data["users"]["alice"]
    with open(path, "w") as f:
        json.dump(data, f)

    sock.emit("start_tool", {"run_id": "r1", "tool_id": "gc-graph", "params": {}})
    completes = [p["args"][0] for p in sock.get_received() if p["name"] == "complete"]
    assert completes and completes[0]["ok"] is False
    assert completes[0]["error"] == "Unauthorized"


def test_websocket_rejects_a_malformed_request(app, client, login):
    login()
    sock = _socket(app, client)
    sock.emit("start_tool", {"run_id": "r1", "tool_id": "gc-graph", "params": "nope"})
    completes = [p["args"][0] for p in sock.get_received() if p["name"] == "complete"]
    assert completes and completes[0]["error"] == "Invalid request"


def test_websocket_rejects_an_unknown_tool(app, client, login):
    login()
    sock = _socket(app, client)
    sock.emit("start_tool", {"run_id": "r1", "tool_id": "no-such-tool", "params": {}})
    completes = [p["args"][0] for p in sock.get_received() if p["name"] == "complete"]
    assert completes and completes[0]["error"] == "Tool not found"


def test_websocket_output_does_not_reach_another_connection(app, client, login):
    """Two sessions, two sockets: one must never receive the other's run.

    The run_id is chosen by the client, so if it were used as the stream
    target a caller could name someone else's channel.
    """
    login()
    mine = _socket(app, client)

    # A second, separately signed-in session -- the realistic threat,
    # since an anonymous one cannot open a socket at all.
    other_http = app.test_client()
    other_http.post(
        "/login", data={"username": "insider", "password": "insiderpw", "csrf_token": "t"}
    )
    with other_http.session_transaction() as sess:
        sess["_csrf_token"] = "t"
    other_http.post(
        "/login", data={"username": "insider", "password": "insiderpw", "csrf_token": "t"}
    )
    eavesdropper = _socket(app, other_http)
    assert eavesdropper.is_connected()

    # Both name the same run_id. If run_id were the channel, the second
    # socket would receive the first one's stream.
    mine.emit("start_tool", {"run_id": "shared-id", "tool_id": "no-such-tool", "params": {}})
    assert [p for p in mine.get_received() if p["name"] == "complete"]
    assert eavesdropper.get_received() == []


def test_websocket_overrides_a_client_supplied_internal_access(
    app, client, login, graph_dir, fake_gen
):
    """The capability flag must come from the session, never the client.

    Without this an unprivileged caller could set _internal_access in the
    params dict and graph an internal change.
    """
    login()  # alice: signed in, no roles
    fake_gen["project"] = INTERNAL_PROJECT
    sock = _socket(app, client)
    sock.emit(
        "start_tool",
        {
            "run_id": "r1",
            "tool_id": "gc-graph",
            "params": {"change_number": "LU-19921", "_internal_access": True, "branch": "b_es7_0"},
        },
    )
    # --branch is privileged, so it must have been dropped despite the
    # client claiming internal access.
    from tests.helpers import FakePopen

    assert "--branch" not in (FakePopen.last_cmd or [])


def test_websocket_silently_drops_an_unauthorised_replace(app, client, login, graph_dir, fake_gen):
    """Replacing an entry the session cannot act on must look exactly like
    replacing one that does not exist -- otherwise the socket becomes an
    oracle for which internal entries exist.
    """
    _seed(graph_dir)
    login()  # alice cannot act on the internal entry 2222

    def run(original):
        sock = _socket(app, client)
        sock.emit(
            "start_tool",
            {
                "run_id": "r1",
                "tool_id": "gc-graph",
                "params": {"change_number": "LU-19921"},
                "original_change_number": original,
            },
        )
        return [p["args"][0] for p in sock.get_received() if p["name"] == "complete"]

    internal = run("2222")
    missing = run("77777777")
    assert internal == missing


# ---------- Tool behaviour: project gating and disclosure ----------


def _run_tool(app, params, graph_dir):
    from portal.tools import gc_graph
    from tests.helpers import FakeSocket

    sock = FakeSocket()
    with app.app_context():
        gc_graph.run_gc_graph(params, sock, "room")
    return sock


def test_public_anchor_never_gets_cross_project(app, graph_dir, fake_gen, monkeypatch):
    """A public graph must not follow into other projects even for a
    privileged user: the HTML is world-readable and its embedded JSON
    would carry internal change identifiers.
    """
    from portal.tools import gc_graph
    from tests.helpers import FakePopen

    monkeypatch.setattr(
        gc_graph,
        "_gerrit_get_change",
        lambda base, n: {"project": PUBLIC_PROJECT, "subject": "LU-1 thing"},
    )
    _run_tool(app, {"change_number": "62796", "_internal_access": True}, graph_dir)
    assert "--cross-project" not in FakePopen.last_cmd


def test_internal_anchor_gets_cross_project_when_authorised(app, graph_dir, fake_gen, monkeypatch):
    from portal.tools import gc_graph
    from tests.helpers import FakePopen

    monkeypatch.setattr(
        gc_graph,
        "_gerrit_get_change",
        lambda base, n: {"project": INTERNAL_PROJECT, "subject": "LU-1 thing"},
    )
    _run_tool(app, {"change_number": "62796", "_internal_access": True}, graph_dir)
    assert "--cross-project" in FakePopen.last_cmd


def test_unauthorised_project_error_does_not_name_the_project(
    app, graph_dir, fake_gen, monkeypatch
):
    """Naming the project would confirm the change exists in one the
    caller is not allowed to know about."""
    from portal.tools import gc_graph

    monkeypatch.setattr(
        gc_graph,
        "_gerrit_get_change",
        lambda base, n: {"project": INTERNAL_PROJECT, "subject": "secret thing"},
    )
    sock = _run_tool(app, {"change_number": "62796", "_internal_access": False}, graph_dir)
    out = sock.output()
    assert INTERNAL_PROJECT not in out
    assert "secret thing" not in out
    assert sock.completes()[0]["error"] == "Not available"


def test_fetch_failure_is_indistinguishable_from_unauthorised(
    app, graph_dir, fake_gen, monkeypatch
):
    """An unprivileged caller must not be able to tell "does not exist"
    from "exists but you may not see it"."""
    from portal.tools import gc_graph

    def boom(base, n):
        raise RuntimeError("network")

    monkeypatch.setattr(gc_graph, "_gerrit_get_change", boom)
    failed = _run_tool(app, {"change_number": "62796", "_internal_access": False}, graph_dir)

    monkeypatch.setattr(
        gc_graph,
        "_gerrit_get_change",
        lambda base, n: {"project": INTERNAL_PROJECT, "subject": "x"},
    )
    denied = _run_tool(app, {"change_number": "62796", "_internal_access": False}, graph_dir)
    assert failed.output() == denied.output()
    assert failed.completes() == denied.completes()


def test_internal_access_is_not_persisted_into_stored_params(app, graph_dir, fake_gen, monkeypatch):
    """If the flag reached the stored params, a later rerun could read it
    back out of the index and treat it as a granted capability."""
    from portal.tools import gc_graph

    monkeypatch.setattr(
        gc_graph,
        "_gerrit_get_change",
        lambda base, n: {"project": PUBLIC_PROJECT, "subject": "LU-1 thing"},
    )
    _run_tool(app, {"change_number": "62796", "_internal_access": True}, graph_dir)
    entry = get_entry(graph_dir, "62796")
    assert entry is not None
    assert "_internal_access" not in (entry.get("params") or {})
