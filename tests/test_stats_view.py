"""The graph summary: extraction, storage, render-time figures, and the list."""

import json
from pathlib import Path

import pytest

from portal.graph_store import add_entry, get_entry, list_entries, set_derived, update_schedule
from portal.stats_view import (
    DAY,
    MISSING,
    bars,
    entry_view,
    fmt_ago,
    fmt_count,
    fmt_duration,
    merged_trend,
)
from portal.tools.graph_stats import extract_stats_from_html, extract_summary

NOW = 1_790_000_000


def sample_summary(**over):
    """A summary shaped like the engine's, with round numbers."""
    s = {
        "as_of": NOW - 3 * 3600,
        "patches": 20,
        "open": 6,
        "merged": 12,
        "abandoned": 2,
        "last_30d": {"opened": 99, "merged": 99, "abandoned": 99},
        "prev_30d": {"opened": 99, "merged": 99, "abandoned": 99},
        "open_30d_ago": 5,
        "recent_events": {
            "opened": [NOW - 2 * DAY],
            "merged": [NOW - 1 * DAY, NOW - 10 * DAY, NOW - 40 * DAY],
            "abandoned": [],
        },
        "time_to_merge": {"count": 12, "median": 20 * DAY, "p90": 200 * DAY},
        "time_to_first_review": {"count": 10, "median": 3 * DAY, "p90": 15 * DAY},
        "patchsets_to_merge": {"count": 12, "median": 4, "max": 17},
        "oldest_open": {"id": 111, "ticket": "LU-1", "opened_at": NOW - 400 * DAY},
        "longest_idle": {"id": 222, "ticket": "LU-2", "last_activity": NOW - 30 * DAY},
        "merged_by_month": [[f"2025-{m:02d}", m % 3] for m in range(10, 13)]
        + [[f"2026-{m:02d}", m % 4] for m in range(1, 10)],
    }
    s.update(over)
    return s


def write_graph(tmp_path, g, name="g.html"):
    path = tmp_path / name
    path.write_text(f"<html><script>\nconst G = {json.dumps(g)};\nrender(G);\n</script></html>")
    return str(path)


# ---------- extraction ----------


def test_extract_summary_keeps_what_the_list_shows(tmp_path):
    path = write_graph(tmp_path, {"nodes": [], "stats": {"summary": sample_summary()}})
    s = extract_summary(path)
    assert s["patches"] == 20
    assert s["longest_idle"]["id"] == 222
    assert len(s["merged_by_month"]) == 12
    # Fixed at generation time, so recounted instead of stored.
    assert "last_30d" not in s and "prev_30d" not in s


def test_extract_summary_is_none_for_an_older_graph(tmp_path):
    path = write_graph(tmp_path, {"nodes": [], "stats": {"node_count": 3}})
    assert extract_summary(path) is None
    assert extract_summary(str(tmp_path / "missing.html")) is None


def test_a_brace_semicolon_inside_a_string_does_not_cut_the_payload(tmp_path):
    """The old non-greedy regex stopped at the first "};" anywhere."""
    g = {
        "nodes": [{"status": "NEW", "subject": "LU-1 fix `x = {a};` parsing"}],
        "stats": {"node_count": 1, "status_counts": {"NEW": 1}, "summary": sample_summary()},
    }
    path = write_graph(tmp_path, g)
    assert extract_stats_from_html(path)["inflight"] == 1
    assert extract_summary(path)["patches"] == 20


# ---------- render-time figures ----------


@pytest.mark.parametrize(
    "seconds, text",
    [
        (None, MISSING),
        (-5, "0 h"),
        (47 * 3600, "47 h"),
        (48 * 3600, "2.0 d"),
        (9.9 * DAY, "9.9 d"),
        (10 * DAY, "10 d"),
        (89 * DAY, "89 d"),
        (90 * DAY, "3.0 mo"),
        (729 * DAY, "23.9 mo"),
        (730 * DAY, "2.0 y"),
    ],
)
def test_fmt_duration(seconds, text):
    assert fmt_duration(seconds) == text


@pytest.mark.parametrize(
    "seconds, text",
    [
        (None, MISSING),
        (5, "just now"),
        (25 * 60, "25 min ago"),
        (47 * 3600, "47 h ago"),
        (3 * DAY, "3 d ago"),
        (120 * DAY, "3 mo ago"),
    ],
)
def test_fmt_ago(seconds, text):
    assert fmt_ago(seconds) == text


def test_fmt_count():
    assert (fmt_count(None), fmt_count(20.0), fmt_count(24.5), fmt_count(3)) == (
        MISSING,
        "20",
        "24.5",
        "3",
    )


def test_merged_trend_windows_are_half_open():
    """An event exactly 30 days old belongs to the earlier window only,
    and one exactly 60 days old to neither."""
    s = {"recent_events": {"merged": [NOW, NOW - 30 * DAY, NOW - 30 * DAY + 1, NOW - 60 * DAY]}}
    assert merged_trend(s, NOW) == (2, 1)


def test_merged_trend_is_recounted_not_read():
    """last_30d says 99; what counts is the events relative to now."""
    assert merged_trend(sample_summary(), NOW) == (2, 1)
    # A month later, the same graph has nothing recent.
    assert merged_trend(sample_summary(), NOW + 61 * DAY) == (0, 0)


def test_merged_trend_without_events():
    assert merged_trend({}, NOW) == (0, 0)
    assert merged_trend(None, NOW) == (0, 0)


def test_bars_are_bottom_aligned_and_keep_empty_months_visible():
    b = bars([0, 2, 4], 30, 10, gap=0)
    assert [x["h"] for x in b] == [1, 5.0, 10.0]
    assert all(x["y"] + x["h"] == 10 for x in b)
    assert b[0]["empty"] and not b[2]["empty"]
    assert bars([], 30, 10) == []


def test_entry_view_formats_a_full_summary():
    v = entry_view({"summary": sample_summary(), "stats": {"ready": 2}}, NOW)
    assert v["has_summary"]
    assert (v["merged_30d"], v["merged_prev_30d"], v["trend"]) == (2, 1, "up")
    assert v["ttm_median"] == "20 d"
    assert v["longest_idle"]["age"] == "30 d"
    assert v["oldest_open"]["age"] == "13.1 mo"
    assert v["as_of_ago"] == "3 h ago"
    assert v["ps_median"] == "4"
    assert v["months"][0]["label"] == "Oct 2025"
    assert v["ready"] == 2
    assert round(sum(v["share"].values())) == 100


def test_entry_view_null_fields_become_placeholders():
    s = sample_summary(
        time_to_merge={"count": 0, "median": None, "p90": None},
        time_to_first_review=None,
        oldest_open=None,
        longest_idle=None,
        patchsets_to_merge={"count": 0, "median": None, "max": None},
    )
    v = entry_view({"summary": s}, NOW)
    assert v["ttm_median"] == MISSING and v["ttfr_median"] == MISSING
    assert v["oldest_open"] is None and v["longest_idle"] is None
    assert v["ps_median"] == MISSING


def test_patch_ids_that_are_not_change_numbers_are_dropped():
    """They are interpolated into a Gerrit link."""
    s = sample_summary(longest_idle={"id": "../x", "ticket": "LU-2", "last_activity": NOW})
    assert entry_view({"summary": s}, NOW)["longest_idle"]["id"] is None


def test_entry_view_without_summary_falls_back_to_the_counts():
    v = entry_view({"stats": {"inflight": 4, "merged": 1, "ready": 1}}, NOW)
    assert not v["has_summary"]
    assert v["open"] == 4 and v["merged"] == 1
    assert entry_view({}, NOW)["open"] is None


# ---------- storage ----------


def test_summary_is_replaced_on_regeneration_and_kept_on_metadata_edits(graph_dir):
    add_entry(graph_dir, "100", summary=sample_summary(patches=1))
    add_entry(graph_dir, "100", name="renamed")  # metadata-only edit
    assert get_entry(graph_dir, "100")["summary"]["patches"] == 1

    add_entry(graph_dir, "100", summary=sample_summary(patches=2))
    assert get_entry(graph_dir, "100")["summary"]["patches"] == 2

    # Regenerated by an engine without summaries: the old one is stale.
    add_entry(graph_dir, "100", summary=None)
    assert "summary" not in get_entry(graph_dir, "100")


def test_search_does_not_match_inside_the_summary(graph_dir):
    """Timestamps and counts would otherwise match almost any number."""
    add_entry(graph_dir, "100", name="x", summary=sample_summary())
    assert list_entries(graph_dir, "222") == []  # longest_idle id
    assert list_entries(graph_dir, "LU-2") == []  # longest_idle ticket


def test_set_derived_touches_only_the_numbers(graph_dir):
    add_entry(graph_dir, "100", name="kept")
    update_schedule(graph_dir, "100", 24)
    before = get_entry(graph_dir, "100")

    assert set_derived(graph_dir, "100", stats={"inflight": 1}, summary=sample_summary())
    after = get_entry(graph_dir, "100")
    assert after["summary"]["patches"] == 20 and after["stats"] == {"inflight": 1}
    for k in ("name", "generated_at", "next_refresh", "refresh_interval_hours"):
        assert after[k] == before[k]

    assert not set_derived(graph_dir, "nope", summary=sample_summary())


def test_backfill_reads_summaries_from_existing_files(app, graph_dir):
    from portal.refresh import _backfill

    add_entry(graph_dir, "100")
    add_entry(graph_dir, "200")
    d = Path(graph_dir)
    write_graph(
        d, {"nodes": [], "stats": {"node_count": 20, "summary": sample_summary()}}, "100.html"
    )
    write_graph(d, {"nodes": [], "stats": {}}, "200.html")

    assert _backfill(graph_dir, app.config["CI_VOTERS"]) == 0
    assert get_entry(graph_dir, "100")["summary"]["patches"] == 20
    assert get_entry(graph_dir, "100")["patches"]["open_ids"] == []
    assert "summary" not in get_entry(graph_dir, "200")


def test_a_run_stores_the_summary(app, graph_dir, fake_gen, monkeypatch):
    from portal.tools import gc_graph
    from tests.helpers import FakeSocket

    monkeypatch.setattr(gc_graph, "extract_summary", lambda path: sample_summary())
    monkeypatch.setattr(
        gc_graph,
        "_gerrit_get_change",
        lambda base, n: {"project": "fs/lustre-release", "subject": "LU-1 thing"},
    )
    with app.app_context():
        gc_graph.run_gc_graph(
            {"change_number": "12345", "_internal_access": False}, FakeSocket(), "r"
        )
    assert get_entry(graph_dir, "12345")["summary"]["patches"] == 20


# ---------- the list ----------


def test_list_shows_stats_and_links_to_the_stats_view(client, graph_dir):
    add_entry(graph_dir, "100", name="Series", summary=sample_summary())
    body = client.get("/gerrit_vis/").data.decode()
    assert 'id="g-100"' in body
    assert "/gerrit_vis/graphs/100.html#stats" in body
    assert "Merged per month" in body
    assert 'class="spark"' in body


def test_list_without_a_summary_still_renders(client, graph_dir):
    add_entry(graph_dir, "100", name="Old", stats={"inflight": 3, "merged": 1, "abandoned": 0})
    body = client.get("/gerrit_vis/").data.decode()
    assert 'id="g-100"' in body
    assert "appear once this graph is regenerated" in body
    assert MISSING in body


# ---------- ready / blocked lists ----------


def _node(change, status="NEW", **review):
    return {
        "id": change,
        "status": status,
        "subject": f"LU-{change} subject",
        "author": "me",
        "last_activity": NOW - 2 * DAY,
        "review": review,
    }


READY = {"verified_pass": True, "cr_votes": [{"name": "a", "value": 1}, {"name": "b", "value": 1}]}


def test_extract_patches_sorts_open_patches_into_lists(tmp_path):
    from portal.tools.graph_stats import extract_patches

    g = {
        "nodes": [
            _node(3, **READY),
            _node(1, cr_veto=True, cr_rejected_by="Rev Iewer"),
            _node(2, verified_fail=True, verified_votes=[{"name": "Maloo", "value": -1}]),
            _node(4),  # in review: neither list
            _node(5, status="MERGED"),
            _node(6, status="ABANDONED"),
            {"id": "not-a-number", "status": "NEW"},
        ]
    }
    p = extract_patches(write_graph(tmp_path, g))
    assert [x["id"] for x in p["ready"]] == [3]
    assert [(x["id"], x["reason"]) for x in p["blocked"]] == [
        (1, "−2 by Rev Iewer"),
        (2, "Maloo −1"),
    ]
    assert p["open_ids"] == [1, 2, 3, 4]
    assert p["merged_ids"] == [5]


def test_patch_lists_are_trimmed_for_display():
    from portal.stats_view import LIST_LIMIT

    many = [{"id": i, "subject": "s", "reason": "Verified −1"} for i in range(LIST_LIMIT + 3)]
    v = entry_view({"patches": {"ready": [], "blocked": many}}, NOW)
    assert len(v["blocked_list"]) == LIST_LIMIT and v["blocked_more"] == 3
    assert v["ready_list"] == [] and v["ready_more"] == 0


def test_patches_are_replaced_on_regeneration_and_kept_on_edits(graph_dir):
    add_entry(graph_dir, "100", patches={"ready": [{"id": 1, "subject": "x"}]})
    add_entry(graph_dir, "100", name="renamed")
    assert get_entry(graph_dir, "100")["patches"]["ready"][0]["id"] == 1
    add_entry(graph_dir, "100", patches=None)
    assert "patches" not in get_entry(graph_dir, "100")


def test_expanded_row_lists_ready_and_blocked(client, graph_dir):
    add_entry(
        graph_dir,
        "100",
        name="Series",
        patches={
            "ready": [{"id": 64321, "subject": "LU-1 land me", "last_activity": NOW}],
            "blocked": [{"id": 64322, "subject": "LU-1 stuck", "reason": "Maloo −1"}],
            "open_ids": [64321, 64322],
            "merged_ids": [],
        },
    )
    body = client.get("/gerrit_vis/").data.decode()
    assert "Ready to land" in body and "LU-1 land me" in body
    assert "Maloo −1" in body


# ---------- totals for a filtered view ----------


def _with_patches(ready, blocked, open_ids, merged_ids, merged_events=()):
    return {
        "patches": {
            "ready": [{"id": i} for i in ready],
            "blocked": [{"id": i} for i in blocked],
            "open_ids": open_ids,
            "merged_ids": merged_ids,
        },
        "summary": {"recent_events": {"merged": list(merged_events)}},
    }


def test_group_totals_count_a_shared_patch_once():
    from portal.stats_view import group_totals

    shared_merge = NOW - 5 * DAY
    a = _with_patches([1], [2], [1, 2, 3], [10, 11], [shared_merge])
    b = _with_patches([1], [], [1, 4], [11, 12], [shared_merge, NOW - 40 * DAY])
    t = group_totals([a, b], NOW)
    assert (t["graphs"], t["ready"], t["blocked"], t["open"], t["merged"]) == (2, 1, 1, 4, 3)
    assert t["in_review"] == 2  # 3 and 4
    assert (t["merged_30d"], t["merged_prev_30d"], t["trend"]) == (1, 1, "flat")
    assert not t["approx"]


def test_group_totals_fall_back_to_counts_for_older_graphs():
    from portal.stats_view import group_totals

    old = {"stats": {"ready": 2, "inflight": 5, "merged": 1, "blocked": 1, "pending": 2}}
    t = group_totals([old, _with_patches([7], [], [7], [])], NOW)
    assert (t["ready"], t["open"], t["merged"]) == (3, 6, 1)
    assert t["approx"]


def test_totals_show_only_on_a_filtered_view(client, graph_dir):
    add_entry(graph_dir, "100", labels=["2.18"], **_with_patches([1], [], [1, 2], [3]))
    add_entry(graph_dir, "200", labels=["2.18"], **_with_patches([1], [], [1], [3]))
    assert "group-summary" not in client.get("/gerrit_vis/").data.decode()
    body = client.get("/gerrit_vis/?label=2.18").data.decode()
    assert 'class="group-summary"' in body
    assert "counted once" in body


# ---------- the row endpoint ----------


def test_row_endpoint_renders_the_same_row(client, graph_dir):
    add_entry(graph_dir, "100", name="Series", labels=["2.18"], summary=sample_summary())
    resp = client.get("/gerrit_vis/row/100?label=2.18")
    assert resp.status_code == 200
    assert "no-store" in resp.headers["Cache-Control"]
    d = resp.get_json()
    assert d["html"].lstrip().startswith('<tbody class="entry" id="g-100"')
    assert d["matches"] is True
    assert d["rerun"] is None  # signed out: nothing to act with

    assert client.get("/gerrit_vis/row/100?label=other").get_json()["matches"] is False


def test_row_endpoint_gives_the_actions_to_a_signed_in_user(client, login, graph_dir):
    add_entry(graph_dir, "100", name="Series", params={"change_number": "100"})
    login("alice", "alicepw")
    d = client.get("/gerrit_vis/row/100").get_json()
    assert d["rerun"]["params"]["change_number"] == "100"
    assert "btn-rerun" in d["html"]


def test_row_endpoint_hides_internal_entries_like_missing_ones(client, login, graph_dir):
    add_entry(graph_dir, "100", project="internal/example-project")
    login("alice", "alicepw")
    hidden = client.get("/gerrit_vis/row/100")
    missing = client.get("/gerrit_vis/row/999")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.data == missing.data
    assert client.get("/gerrit_vis/row/..%2Findex").status_code == 404


def test_row_endpoint_shows_internal_entries_to_insiders(client, login_internal, graph_dir):
    add_entry(graph_dir, "100", project="internal/example-project")
    login_internal()
    assert client.get("/gerrit_vis/row/100").status_code == 200
