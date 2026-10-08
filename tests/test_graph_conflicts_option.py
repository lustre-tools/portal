"""`gc graph --conflicts`: on when a clone is configured, for public graphs."""

import pytest

from portal.tools import gc_graph
from tests.helpers import FakePopen, FakeSocket


@pytest.fixture
def clone(tmp_path):
    path = tmp_path / "data" / "lustre-release.git"
    path.mkdir(parents=True)
    return str(path)


def _run(app, monkeypatch, change="62757", project="fs/lustre-release", internal=False):
    monkeypatch.setattr(
        gc_graph,
        "_gerrit_get_change",
        lambda base, n: {"project": project, "subject": "LU-12669 ec: recover data from parity"},
    )
    FakePopen.last_cmd = None
    with app.app_context():
        gc_graph.run_gc_graph(
            {"change_number": change, "_internal_access": internal}, FakeSocket(), "room1"
        )
    return FakePopen.last_cmd


def test_no_clone_configured_no_conflict_checks(app, graph_dir, fake_gen, monkeypatch):
    app.config["GRAPH_CONFLICTS_REPO"] = None
    assert "--conflicts" not in _run(app, monkeypatch)


def test_a_public_graph_is_checked_against_the_clone(app, graph_dir, fake_gen, monkeypatch, clone):
    app.config["GRAPH_CONFLICTS_REPO"] = clone
    cmd = _run(app, monkeypatch)
    assert cmd[cmd.index("--conflicts") + 1] == clone
    assert FakePopen.last_kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_an_internal_graph_is_not(app, graph_dir, fake_gen, monkeypatch, clone):
    """The clone is of the public project; an internal change could not
    be fetched into it."""
    app.config["GRAPH_CONFLICTS_REPO"] = clone
    cmd = _run(app, monkeypatch, project="internal/example-project", internal=True)
    assert "--cross-project" in cmd and "--conflicts" not in cmd


def test_a_ticket_graph_is_checked_too(app, graph_dir, fake_gen, monkeypatch, clone):
    """Its project is known only afterwards; gc leaves the conflicts out
    of a graph whose branch the clone cannot fetch."""
    app.config["GRAPH_CONFLICTS_REPO"] = clone
    cmd = _run(app, monkeypatch, change="LU-12669")
    assert cmd[cmd.index("--conflicts") + 1] == clone


def test_a_missing_clone_is_skipped_not_fatal(app, graph_dir, fake_gen, monkeypatch, tmp_path):
    app.config["GRAPH_CONFLICTS_REPO"] = str(tmp_path / "gone")
    cmd = _run(app, monkeypatch)
    assert cmd is not None and "--conflicts" not in cmd


def test_the_clone_path_stays_out_of_the_console(app, clone):
    app.config["GRAPH_CONFLICTS_REPO"] = clone
    with app.app_context():
        line = gc_graph._sanitize_output(
            f"[8/8] Trial merges on master ({clone})\n", gc_graph._deployment_paths()
        )
    assert "data" not in line and "lustre-release.git" in line
