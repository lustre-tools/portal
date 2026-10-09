"""Tests for the HTML stats extractor."""

import textwrap

from portal.tools.graph_stats import extract_stats_from_html, review_health

#: Verified +1 from both CI systems -- what "ready" needs.
BOTH_CI = [{"name": "jenkins", "value": 1}, {"name": "Maloo", "value": 1}]
TWO_REVIEWS = [{"name": "alice", "value": 1}, {"name": "bob", "value": 1}]


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
                    "verified_votes": BOTH_CI,
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
            "verified_votes": BOTH_CI,
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
            "verified_votes": BOTH_CI,
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


# The rule as the graph page has it (gc graph's reviewHealth()); the list
# once fell behind on each of these and counted 14 ready where the graph
# showed 7. test_health_parity.py checks the page's own function too.


def test_ready_needs_a_plus_one_from_every_ci():
    one_ci = {
        "status": "NEW",
        "review": {
            "verified_pass": True,
            "verified_votes": [{"name": "jenkins", "value": 1}],
            "cr_votes": TWO_REVIEWS,
        },
    }
    assert review_health(one_ci) == "pending", "Maloo has not voted yet"
    one_ci["review"]["verified_votes"] = BOTH_CI
    assert review_health(one_ci) == "good"


def test_the_owner_self_vote_does_not_count_the_author_does():
    n = {
        "status": "NEW",
        "owner": "pat",
        "author": "alice",
        "review": {"verified_pass": True, "verified_votes": BOTH_CI, "cr_votes": TWO_REVIEWS},
    }
    assert review_health(n) == "good", "alice wrote it, pat owns it: her +1 counts"
    n["owner"] = "alice"
    assert review_health(n) == "pending"


def test_a_backport_needs_one_reviewer():
    n = {
        "status": "NEW",
        "is_backport": True,
        "review": {
            "verified_pass": True,
            "verified_votes": BOTH_CI,
            "cr_votes": [{"name": "alice", "value": 1}],
        },
    }
    assert review_health(n) == "good"
    n["is_backport"] = False
    assert review_health(n) == "pending"


def test_changes_the_series_only_sits_on_are_not_counted(tmp_path):
    """An unrelated in-flight parent is drawn dimmed and counted nowhere on
    the page; a merged change only holding up the trunk is not counted
    as landed."""
    from portal.tools.graph_stats import extract_patches

    ready = {"verified_pass": True, "verified_votes": BOTH_CI, "cr_votes": TWO_REVIEWS}
    data = {
        "stats": {"node_count": 3, "status_counts": {"NEW": 1, "MERGED": 1}},
        "nodes": [
            {"id": 1, "status": "NEW", "review": ready},
            {"id": 2, "status": "NEW", "review": ready, "unrelated_parent": True},
            {"id": 3, "status": "MERGED"},
            {"id": 4, "status": "MERGED", "trunk_structural": True},
        ],
    }
    path = _write_html(tmp_path, data)
    assert extract_stats_from_html(path)["ready"] == 1
    p = extract_patches(path)
    assert [x["id"] for x in p["ready"]] == [1]
    assert p["open_ids"] == [1] and p["merged_ids"] == [3]
