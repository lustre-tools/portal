"""Tests for LU-ticket mode in the Gerrit visualizer.

Covers the pure identifier/ticket helpers, project classification from a
generated graph, the shared graph-id validation, the server-side branch
gate + command construction in _do_run, and the widened route guards.
"""

import json
import os

import pytest

from portal.graph_store import (
    GRAPH_FILE_RE,
    add_entry,
    get_entry,
    is_valid_graph_id,
)
from portal.tools import gc_graph
from portal.tools.gc_graph import _normalize_tickets, _parse_identifier
from portal.tools.graph_stats import classify_graph_project
from tests.helpers import FakePopen, FakeSocket

# --------------------------------------------------------------------------
# _parse_identifier
# --------------------------------------------------------------------------


def test_parse_identifier_number():
    assert _parse_identifier("62796") == ("number", "62796", "62796")


def test_parse_identifier_number_strips_whitespace():
    assert _parse_identifier("  62796 ") == ("number", "62796", "62796")


def test_parse_identifier_ticket_uppercase():
    assert _parse_identifier("LU-19921") == ("ticket", "LU-19921", "LU-19921")


def test_parse_identifier_ticket_lowercased_input_is_normalized():
    # The gc CLI only detects uppercase tickets, so the web form must
    # uppercase before invoking.
    assert _parse_identifier("lu-19921") == ("ticket", "LU-19921", "LU-19921")


def test_parse_identifier_url_extracts_trailing_number():
    url = "https://review.whamcloud.com/c/fs/lustre-release/+/62796"
    assert _parse_identifier(url) == ("url", url, "62796")


def test_parse_identifier_rejects_garbage():
    assert _parse_identifier("not a thing") == (None, None, None)


def test_parse_identifier_rejects_empty():
    assert _parse_identifier("") == (None, None, None)
    assert _parse_identifier("   ") == (None, None, None)


def test_parse_identifier_rejects_url_without_number():
    assert _parse_identifier("https://example.com/foo/bar") == (None, None, None)


def test_ticket_and_number_file_ids_are_disjoint():
    # LU-19921 must not collapse to 19921 (the collision the old code had).
    _, _, ticket_id = _parse_identifier("LU-19921")
    _, _, number_id = _parse_identifier("19921")
    assert ticket_id == "LU-19921"
    assert number_id == "19921"
    assert ticket_id != number_id


# --------------------------------------------------------------------------
# _normalize_tickets
# --------------------------------------------------------------------------


def test_normalize_tickets_uppercases_and_dedupes():
    assert _normalize_tickets("lu-1, LU-1, LU-2") == "LU-1,LU-2"


def test_normalize_tickets_drops_invalid_tokens():
    assert _normalize_tickets("LU-18222, garbage, LU-17916") == "LU-18222,LU-17916"


def test_normalize_tickets_empty():
    assert _normalize_tickets("") == ""
    assert _normalize_tickets(None) == ""


def test_normalize_tickets_caps_count():
    raw = ",".join(f"LU-{i}" for i in range(1, 25))
    out = _normalize_tickets(raw, cap=10)
    assert len(out.split(",")) == 10


# --------------------------------------------------------------------------
# classify_graph_project
# --------------------------------------------------------------------------


def _write_graph_html(path, nodes):
    g = {"nodes": nodes, "stats": {}}
    with open(path, "w", encoding="utf-8") as f:
        f.write("<html><script>const G = " + json.dumps(g) + ";</script></html>")


def test_classify_all_fs_nodes_is_public(tmp_path):
    p = tmp_path / "g.html"
    _write_graph_html(
        p,
        [
            {"id": 1, "project": "fs/lustre-release", "subject": "A"},
            {"id": 2, "project": "fs/lustre-release", "subject": "B"},
        ],
    )
    project, _ = classify_graph_project(str(p), "fs/lustre-release")
    assert project == "fs/lustre-release"


def test_classify_any_internal_node_is_internal(tmp_path):
    p = tmp_path / "g.html"
    _write_graph_html(
        p,
        [
            {"id": 1, "project": "fs/lustre-release", "subject": "A"},
            {"id": 2, "project": "internal/example-project", "subject": "B"},
        ],
    )
    project, _ = classify_graph_project(str(p), "fs/lustre-release")
    assert project == "internal/example-project"


def test_classify_returns_anchor_subject(tmp_path):
    p = tmp_path / "g.html"
    _write_graph_html(
        p,
        [
            {"id": 62796, "project": "fs/lustre-release", "subject": "LU-19921 fix thing"},
        ],
    )
    project, subject = classify_graph_project(str(p), "fs/lustre-release", anchor_id="62796")
    assert project == "fs/lustre-release"
    assert subject == "LU-19921 fix thing"


def test_classify_unparseable_returns_none(tmp_path):
    p = tmp_path / "empty.html"
    p.write_text("<html>no graph data here</html>")
    assert classify_graph_project(str(p), "fs/lustre-release") == (None, "")


# --------------------------------------------------------------------------
# graph-id validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("62796", True),
        ("LU-19921", True),
        ("lu-19921", False),  # lowercase never valid as an id
        ("abc", False),
        ("../etc", False),
        ("62796.html", False),
        ("", False),
    ],
)
def test_is_valid_graph_id(value, expected):
    assert is_valid_graph_id(value) is expected


@pytest.mark.parametrize(
    "filename,group",
    [
        ("62796.html", "62796"),
        ("LU-19921.html", "LU-19921"),
    ],
)
def test_graph_file_re_matches(filename, group):
    m = GRAPH_FILE_RE.match(filename)
    assert m and m.group(1) == group


@pytest.mark.parametrize("filename", ["index.json", "lu-1.html", "../x.html", "62796.htm"])
def test_graph_file_re_rejects(filename):
    assert GRAPH_FILE_RE.match(filename) is None


# --------------------------------------------------------------------------
# _do_run: command construction, branch gate, classification
# --------------------------------------------------------------------------


def test_ticket_public_drops_branch_and_normalizes(app, graph_dir, fake_gen):
    sock = FakeSocket()
    with app.app_context():
        gc_graph.run_gc_graph(
            {
                "change_number": "lu-19921",
                "_internal_access": False,
                "branch": "b_es7_0",
                "ticket": "lu-18222, garbage, LU-17916",
            },
            sock,
            "room1",
        )
    cmd = FakePopen.last_cmd
    # positional is the uppercased ticket
    assert cmd[2] == "LU-19921"
    # public user: --branch is dropped even though supplied
    assert "--branch" not in cmd
    # ticket mode never crosses projects
    assert "--cross-project" not in cmd
    # extra tickets validated + uppercased
    assert cmd[cmd.index("--ticket") + 1] == "LU-18222,LU-17916"

    e = get_entry(graph_dir, "LU-19921")
    assert e is not None
    assert e["file"] == "LU-19921.html"
    assert e["project"] == "fs/lustre-release"
    assert e["anchor_change_number"] == "62796"
    assert e["ticket"] == "LU-19921"


def test_ticket_internal_keeps_branch_and_classifies_internal(app, graph_dir, fake_gen):
    fake_gen["project"] = "internal/example-project"
    sock = FakeSocket()
    with app.app_context():
        gc_graph.run_gc_graph(
            {
                "change_number": "LU-19921",
                "_internal_access": True,
                "branch": "b_es7_0",
            },
            sock,
            "room1",
        )
    cmd = FakePopen.last_cmd
    assert cmd[cmd.index("--branch") + 1] == "b_es7_0"
    assert "--cross-project" not in cmd  # ticket mode never cross-project

    e = get_entry(graph_dir, "LU-19921")
    # Classified internal from the anchor node's project -> hidden on serve.
    assert e["project"] == "internal/example-project"


def test_ticket_internal_rejects_bad_branch(app, graph_dir, fake_gen):
    sock = FakeSocket()
    with app.app_context():
        gc_graph.run_gc_graph(
            {
                "change_number": "LU-19921",
                "_internal_access": True,
                "branch": "-injected",  # leading dash rejected
            },
            sock,
            "room1",
        )
    assert "--branch" not in FakePopen.last_cmd


def test_ticket_invalid_identifier_errors(app, graph_dir, fake_gen):
    FakePopen.last_cmd = None  # detect whether Popen gets called
    sock = FakeSocket()
    with app.app_context():
        gc_graph.run_gc_graph(
            {"change_number": "totally invalid", "_internal_access": False},
            sock,
            "room1",
        )
    comps = sock.completes()
    assert comps and comps[-1]["ok"] is False
    # invalid input is rejected before any subprocess runs
    assert FakePopen.last_cmd is None


# --------------------------------------------------------------------------
# route guards accept ticket ids
# --------------------------------------------------------------------------


def test_serve_ticket_graph_public(client, graph_dir):
    add_entry(graph_dir, "LU-19921", project="fs/lustre-release")
    with open(os.path.join(graph_dir, "LU-19921.html"), "w") as f:
        f.write("<html>ticket graph</html>")
    resp = client.get("/gerrit_vis/graphs/LU-19921.html")
    assert resp.status_code == 200
    assert b"ticket graph" in resp.data


def test_serve_internal_ticket_graph_404_for_anonymous(client, graph_dir):
    add_entry(graph_dir, "LU-19921", project="internal/example-project")
    with open(os.path.join(graph_dir, "LU-19921.html"), "w") as f:
        f.write("<html>internal</html>")
    resp = client.get("/gerrit_vis/graphs/LU-19921.html")
    assert resp.status_code == 404


def test_serve_internal_ticket_graph_visible_to_internal_role(client, login_internal, graph_dir):
    add_entry(graph_dir, "LU-19921", project="internal/example-project")
    with open(os.path.join(graph_dir, "LU-19921.html"), "w") as f:
        f.write("<html>internal</html>")
    login_internal()
    resp = client.get("/gerrit_vis/graphs/LU-19921.html")
    assert resp.status_code == 200


def test_delete_accepts_ticket_id(client, login, graph_dir):
    add_entry(graph_dir, "LU-19921", project="fs/lustre-release")
    login("alice", "alicepw")
    resp = client.post("/gerrit_vis/graphs/delete/LU-19921", follow_redirects=False)
    assert resp.status_code == 302
    assert get_entry(graph_dir, "LU-19921") is None


def test_metadata_accepts_ticket_id(client, login, graph_dir):
    add_entry(graph_dir, "LU-19921", name="orig", subject="LU-19921 x", project="fs/lustre-release")
    login("alice", "alicepw")
    resp = client.post(
        "/gerrit_vis/graphs/metadata/LU-19921",
        data={"name": "renamed", "labels": "pcc"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    e = get_entry(graph_dir, "LU-19921")
    assert e["name"] == "renamed"
    assert "pcc" in e["labels"]


def test_delete_still_rejects_garbage_id(client, login, graph_dir):
    login("alice", "alicepw")
    resp = client.post("/gerrit_vis/graphs/delete/not-an-id")
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Output sanitising: server paths never reach the browser
# --------------------------------------------------------------------------


def test_sanitizer_strips_the_configured_data_dir(app, tmp_path):
    """The static list cannot know where an operator put the data. The
    first real install used /var/lib/portal and the path leaked."""
    from portal.tools.gc_graph import _deployment_paths, _sanitize_output

    with app.app_context():
        graph_dir = app.config["GRAPH_OUTPUT_DIR"]
        line = f'{{"html_path": "{graph_dir}/61965.html", "anchor": 61965}}\n'
        out = _sanitize_output(line, _deployment_paths())
    assert graph_dir not in out
    assert "61965.html" in out


def test_sanitizer_strips_common_system_prefixes():
    from portal.tools.gc_graph import _sanitize_output

    for prefix in (
        "/root/x/y",
        "/srv/http/z",
        "/etc/secret",
        "/home/me/f",
        "/var/lib/p/g",
        "/opt/app/q",
    ):
        line = f'File "{prefix}/thing.py", line 3\n'
        out = _sanitize_output(line)
        assert prefix not in out, prefix
        assert "thing.py" in out


def test_sanitizer_leaves_ordinary_text_alone():
    from portal.tools.gc_graph import _sanitize_output

    line = "Building graph for LU-12187: 48 merged, 19 chains\n"
    assert _sanitize_output(line, ("/var/lib/portal",)) == line


# ---------- a run that omits name / labels ----------


def _run(app, params):
    sock = FakeSocket()
    with app.app_context():
        gc_graph.run_gc_graph(params, sock, "room1")
    return sock


def test_a_run_without_name_or_labels_keeps_the_graphs_own(app, graph_dir, fake_gen):
    """A script passing just the id used to wipe the labels and reset the
    name to the subject -- it happened to a live graph."""
    add_entry(
        graph_dir,
        "LU-19921",
        name="My series",
        labels=["2.18", "ec"],
        params={"change_number": "LU-19921", "name": "My series", "labels": "2.18, ec"},
    )
    _run(app, {"change_number": "LU-19921", "_internal_access": False})
    e = get_entry(graph_dir, "LU-19921")
    assert e["name"] == "My series"
    assert e["labels"] == ["2.18", "ec"]
    assert e["params"]["labels"] == "2.18, ec", "and later reruns carry them too"


def test_explicit_empty_fields_still_clear_them(app, graph_dir, fake_gen):
    """The page always sends both fields; emptying them must still work."""
    add_entry(graph_dir, "LU-19921", name="My series", labels=["2.18"])
    _run(app, {"change_number": "LU-19921", "_internal_access": False, "name": "", "labels": ""})
    e = get_entry(graph_dir, "LU-19921")
    assert e["labels"] == []
    assert e["name"] != "My series"


def test_an_internal_graphs_labels_are_not_borrowed_by_a_public_run(app, graph_dir, fake_gen):
    add_entry(graph_dir, "LU-19921", labels=["secret-label"], project="internal/example-project")
    sock = _run(app, {"change_number": "LU-19921", "_internal_access": False})
    assert "secret-label" not in sock.output()
