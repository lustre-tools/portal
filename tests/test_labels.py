"""Tests for label normalization and the autocomplete data."""

from portal.graph_store import add_entry
from portal.tools.gc_graph import _normalize_labels


def test_normalize_basic_csv():
    assert _normalize_labels("alpha, beta, gamma") == ["alpha", "beta", "gamma"]


def test_normalize_lowercases():
    assert _normalize_labels("Alpha, BETA") == ["alpha", "beta"]


def test_normalize_dedupes():
    assert _normalize_labels("alpha, alpha, ALPHA") == ["alpha"]


def test_normalize_strips_whitespace():
    assert _normalize_labels("  alpha  ,   beta  ") == ["alpha", "beta"]


def test_normalize_drops_empty_pieces():
    assert _normalize_labels(",,alpha,,") == ["alpha"]


def test_normalize_replaces_forbidden_chars():
    # Spaces, special chars get replaced with hyphen
    assert _normalize_labels("foo bar") == ["foo-bar"]
    assert _normalize_labels("hello!world") == ["hello-world"]


def test_normalize_keeps_allowed_chars():
    # a-z 0-9 . _ - all allowed
    result = _normalize_labels("foo.bar, foo_bar, foo-bar, v2.18")
    assert "foo.bar" in result
    assert "foo_bar" in result
    assert "foo-bar" in result
    assert "v2.18" in result


def test_normalize_empty_string():
    assert _normalize_labels("") == []


def test_normalize_only_whitespace():
    assert _normalize_labels("   ,  , ") == []


def test_normalize_drops_after_sanitization_if_empty():
    # All-special-char labels get reduced to "" which is dropped
    result = _normalize_labels("!!!, alpha")
    assert result == ["alpha"]


def test_index_passes_all_labels_for_autocomplete(client, graph_dir):
    add_entry(graph_dir, "1", labels=["alpha", "beta"])
    add_entry(graph_dir, "2", labels=["beta", "gamma"])
    add_entry(graph_dir, "3", labels=[])
    # Autocomplete data is only sent to authenticated users
    client.post("/login", data={"username": "alice", "password": "alicepw"})
    resp = client.get("/gerrit_vis/")
    body = resp.data.decode()
    assert "ALL_LABELS" in body
    # Should be sorted unique
    import re

    m = re.search(r"const ALL_LABELS = (\[[^\]]*\])", body)
    assert m
    import json

    labels = json.loads(m.group(1))
    assert labels == ["alpha", "beta", "gamma"]


def test_run_semaphore_respects_config(monkeypatch):
    """The interactive-run semaphore is bounded by the configured limit."""
    import portal.tools.gc_graph as gc

    gc._run_semaphore = None  # reset the memoised singleton
    try:
        sem = gc._get_run_semaphore(2)
        assert sem.acquire(blocking=False) is True
        assert sem.acquire(blocking=False) is True
        assert sem.acquire(blocking=False) is False
        sem.release()
        sem.release()
    finally:
        gc._run_semaphore = None


def test_run_semaphore_defaults_to_three(app):
    """An unconfigured portal allows three concurrent runs."""
    import portal.tools.gc_graph as gc

    gc._run_semaphore = None
    try:
        sem = gc._get_run_semaphore(app.config["INTERACTIVE_RUN_CONCURRENCY"])
        acquired = 0
        while sem.acquire(blocking=False):
            acquired += 1
        assert acquired == 3
        for _ in range(acquired):
            sem.release()
    finally:
        gc._run_semaphore = None
