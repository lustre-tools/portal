"""Tests for the HTML stats extractor."""

import textwrap

from portal.tools.graph_stats import extract_stats_from_html, review_health


def _write_html(tmp_path, graph_data):
    """Write a minimal HTML file embedding the given G object."""
    import json

    html = textwrap.dedent(f"""
        <html><head></head><body>
        <script>
        const G = {json.dumps(graph_data)};
        </script>
        </body></html>
    """)
    path = tmp_path / "test.html"
    path.write_text(html)
    return str(path)


def test_extract_basic(tmp_path):
    data = {
        "stats": {
            "node_count": 5,
            "status_counts": {"NEW": 3, "MERGED": 1, "ABANDONED": 1},
        },
        "nodes": [
            {
                "status": "NEW",
                "review": {
                    "verified_pass": True,
                    "cr_votes": [{"name": "rev1", "value": 1}, {"name": "rev2", "value": 1}],
                },
                "author": "me",
            },
            {"status": "NEW", "review": {"cr_veto": True}},
            {"status": "NEW", "review": {}},
            {"status": "MERGED", "review": {}},
            {"status": "ABANDONED", "review": {}},
        ],
    }
    path = _write_html(tmp_path, data)
    s = extract_stats_from_html(path)
    assert s["node_count"] == 5
    assert s["inflight"] == 3
    assert s["merged"] == 1
    assert s["abandoned"] == 1
    assert s["ready"] == 1  # only the first NEW node has 2 +1s
    assert s["blocked"] == 1  # the cr_veto one
    assert s["pending"] == 1  # third NEW with no review activity


def test_extract_returns_none_for_missing_file(tmp_path):
    s = extract_stats_from_html(str(tmp_path / "nonexistent.html"))
    assert s is None


def test_extract_returns_none_for_invalid_html(tmp_path):
    p = tmp_path / "bad.html"
    p.write_text("<html>no graph data here</html>")
    assert extract_stats_from_html(str(p)) is None


def test_extract_returns_none_for_invalid_json(tmp_path):
    p = tmp_path / "bad.html"
    p.write_text("<script>const G = {not valid json};</script>")
    assert extract_stats_from_html(str(p)) is None


def test_extract_handles_missing_status_counts(tmp_path):
    data = {"stats": {"node_count": 0}, "nodes": []}
    s = extract_stats_from_html(_write_html(tmp_path, data))
    assert s["inflight"] == 0
    assert s["merged"] == 0
    assert s["abandoned"] == 0


def test_review_health_abandoned_is_pending():
    # Non-NEW always returns "pending"
    assert review_health({"status": "ABANDONED"}) == "pending"
    assert review_health({"status": "MERGED"}) == "pending"


def test_review_health_cr_veto_blocks():
    n = {"status": "NEW", "review": {"cr_veto": True, "verified_pass": True}}
    assert review_health(n) == "bad_veto"


def test_review_health_maloo_failure():
    n = {
        "status": "NEW",
        "review": {
            "verified_fail": True,
            "verified_votes": [{"name": "Maloo", "value": -1}],
        },
    }
    assert review_health(n) == "bad_maloo"


def test_review_health_jenkins_failure():
    n = {
        "status": "NEW",
        "review": {
            "verified_fail": True,
            "verified_votes": [{"name": "jenkins", "value": -1}],
        },
    }
    assert review_health(n) == "bad_jenkins"


def test_review_health_ready_requires_two_non_author_plus():
    n = {
        "status": "NEW",
        "review": {
            "verified_pass": True,
            "cr_votes": [
                {"name": "alice", "value": 1},
                {"name": "bob", "value": 1},
            ],
        },
        "author": "me",
    }
    assert review_health(n) == "good"


def test_review_health_author_plus_doesnt_count():
    n = {
        "status": "NEW",
        "review": {
            "verified_pass": True,
            "cr_votes": [
                {"name": "me", "value": 1},  # self-vote
                {"name": "alice", "value": 1},  # only one non-author +1
            ],
        },
        "author": "me",
    }
    assert review_health(n) == "pending"


def test_review_health_no_verified_pass_is_pending():
    n = {
        "status": "NEW",
        "review": {
            "cr_votes": [
                {"name": "alice", "value": 1},
                {"name": "bob", "value": 1},
            ],
        },
        "author": "me",
    }
    assert review_health(n) == "pending"
