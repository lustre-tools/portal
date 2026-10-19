"""Graphs that follow hashtags or topics: the list's second set of figures
(gc's stats.focus), and graphs built from a hashtag or topic alone."""

from pathlib import Path

import pytest

from portal.graph_store import (
    GRAPH_FILE_RE,
    add_entry,
    get_entry,
    is_valid_graph_id,
    set_derived,
)
from portal.stats_view import MISSING, entry_view, focus_label
from portal.tools import gc_graph
from portal.tools.gc_graph import _parse_identifier
from portal.tools.graph_stats import extract_focus
from tests.helpers import FakePopen, FakeSocket
from tests.test_stats_view import NOW, sample_summary, write_graph

TAG = "218_review_priority"
READY = {
    "verified_pass": True,
    "verified_votes": [{"name": "jenkins", "value": 1}, {"name": "maloo", "value": 1}],
    "cr_votes": [{"name": "a", "value": 1}, {"name": "b", "value": 1}],
}


def graph(anchor=1, on_by_default=False, members=True):
    """A graph of 4 changes, 2 of them (1 and 2) carrying the hashtag."""
    tagged = [TAG] if members else []
    return {
        "anchor": anchor,
        "nodes": [
            {"id": 1, "status": "NEW", "hashtags": tagged, "review": READY},
            {"id": 2, "status": "MERGED", "hashtags": tagged},
            {"id": 3, "status": "NEW", "hashtags": ["other"], "review": READY},
            {"id": 4, "status": "NEW", "review": {"cr_veto": True}},
        ],
        "stats": {
            "node_count": 4,
            "status_counts": {"NEW": 3, "MERGED": 1},
            "summary": sample_summary(),
            "focus": {
                "hashtags": [TAG],
                "topics": [],
                "on_by_default": on_by_default,
                "node_count": 2 if members else 0,
                "status_counts": {"NEW": 1, "MERGED": 1} if members else {},
                "summary": sample_summary(patches=2, open=1, merged=1, abandoned=0)
                if members
                else {
                    "patches": 0,
                    "open": 0,
                    "merged": 0,
                    "abandoned": 0,
                    "recent_events": {"opened": [], "merged": [], "abandoned": []},
                    "time_to_merge": {"count": 0, "median": None, "p90": None},
                    "oldest_open": None,
                    "merged_by_month": [],
                },
            },
        },
    }


# ---------- the figures ----------


def test_focus_counts_the_changes_carrying_the_hashtag(tmp_path):
    f = extract_focus(write_graph(tmp_path, graph()))
    assert f["applies"], "the anchor (1) carries the hashtag"
    assert f["hashtags"] == [TAG] and f["topics"] == []
    assert f["stats"] == {
        "node_count": 2,
        "inflight": 1,
        "ready": 1,  # change 1; change 3 is ready too, but not tagged
        "pending": 0,
        "blocked": 0,
        "merged": 1,
        "abandoned": 0,
    }
    assert f["summary"]["patches"] == 2
    assert "last_30d" not in f["summary"], "trimmed like the whole graph's"


def test_the_list_leads_with_it_only_when_the_graph_is_about_it(tmp_path):
    not_about = extract_focus(write_graph(tmp_path, graph(anchor=3)))
    assert not not_about["applies"], "anchor 3 does not carry the hashtag"
    built_from_it = extract_focus(write_graph(tmp_path, graph(anchor=3, on_by_default=True)))
    assert built_from_it["applies"], "a hashtag: graph is about it, whatever its anchor"


def test_no_focus_no_new_figures(tmp_path):
    g = graph()
    del g["stats"]["focus"]
    assert extract_focus(write_graph(tmp_path, g)) is None
    entry = {"change_number": "100", "stats": {"inflight": 3}, "summary": sample_summary()}
    assert entry_view(entry, NOW)["focus"] is None


def test_the_row_shows_the_hashtags_figures_and_keeps_the_whole_graphs(tmp_path):
    f = extract_focus(write_graph(tmp_path, graph()))
    entry = {
        "change_number": "67069",
        "stats": {"inflight": 3, "ready": 2, "pending": 0, "blocked": 1},
        "summary": sample_summary(),
        "focus": f,
    }
    v = entry_view(entry, NOW)
    assert v["focus"]["label"] == f"#{TAG}"
    assert (v["focus"]["ready"], v["focus"]["open"], v["focus"]["merged"]) == (1, 1, 1)
    assert (v["ready"], v["open"], v["merged"]) == (2, 6, 12), "the whole graph, unchanged"


def test_a_hashtag_no_change_carries_shows_zeros_and_placeholders(tmp_path):
    f = extract_focus(write_graph(tmp_path, graph(on_by_default=True, members=False)))
    v = entry_view({"change_number": "x", "stats": {}, "focus": f}, NOW)["focus"]
    assert (v["ready"], v["open"], v["merged"], v["merged_30d"]) == (0, 0, 0, 0)
    assert v["ttm_median"] == MISSING and v["oldest_open"] is None


def test_label_names_hashtags_and_topics():
    assert focus_label({"hashtags": ["a", "b"], "topics": ["ec2"]}) == "#a, #b, topic ec2"


def test_focus_is_replaced_on_regeneration_kept_on_edits_and_out_of_search(tmp_path, graph_dir):
    from portal.graph_store import list_entries

    f = extract_focus(write_graph(tmp_path, graph()))
    add_entry(graph_dir, "67069", name="Prio", stats={"inflight": 3}, focus=f)
    add_entry(graph_dir, "67069", name="Prio renamed", touch_generated_at=False)
    assert get_entry(graph_dir, "67069")["focus"]["stats"]["ready"] == 1, "a metadata edit"
    assert list_entries(graph_dir, TAG[:6]) == [], "not searched like the stats"
    add_entry(graph_dir, "67069", name="Prio", stats={"inflight": 3}, focus=None)
    assert "focus" not in get_entry(graph_dir, "67069"), "regenerated without one"


def test_backfill_reads_and_drops_focus(app, tmp_path, graph_dir):
    from portal.refresh import _backfill

    add_entry(graph_dir, "100")
    add_entry(graph_dir, "200", focus={"applies": True, "stats": {}})
    d = Path(graph_dir)
    write_graph(d, graph(), "100.html")
    g = graph()
    del g["stats"]["focus"]
    write_graph(d, g, "200.html")
    _backfill(graph_dir, app.config["CI_VOTERS"])
    assert get_entry(graph_dir, "100")["focus"]["stats"]["node_count"] == 2
    assert "focus" not in get_entry(graph_dir, "200"), "the file has none"
    set_derived(graph_dir, "100", stats={"inflight": 1})
    assert "focus" in get_entry(graph_dir, "100"), "kept unless given"


def test_the_list_page_shows_both(client, tmp_path, graph_dir):
    """The row counts the hashtag's changes and names the hashtag beside
    the change it was built from, not in the figures; expanded, each set
    of figures sits under its own heading."""
    f = extract_focus(write_graph(tmp_path, graph()))
    add_entry(
        graph_dir, "67069", name="Prio", stats={"inflight": 3}, summary=sample_summary(), focus=f
    )
    add_entry(graph_dir, "100", name="Plain", summary=sample_summary())
    body = client.get("/gerrit_vis/").data.decode()
    head, prio = body.split('id="g-67069"', 1)
    prio = prio.split("</tbody>", 1)[0]
    row, detail = prio.split('class="entry-detail"', 1)
    meta = row.split('class="entry-meta"', 1)[1].split("</div>", 1)[0]
    assert "/+/67069" in meta and 'class="focus-pill"' in meta and f"#{TAG}</a>" in meta
    figures = row.split('class="col-num col-status"', 1)[1].split('class="col-updated"', 1)[0]
    assert f"#{TAG}<" not in figures, "the hashtag is not in the number cells"
    assert "Only the 2 changes with" in figures, "but the tooltip says what they count"
    assert 'data-open="1"' in head.rsplit("<tbody", 1)[1] + row[:300]
    titles = detail.split('class="figures-title')
    assert len(titles) == 3, "one heading for the hashtag's changes, one for the whole graph"
    assert (
        f"#{TAG}" in titles[1] and "2 changes" in titles[1] and "what the row counts" in titles[1]
    )
    assert "Whole graph &middot; 20 changes" in titles[2]
    plain = body.split('id="g-100"', 1)[1].split("</tbody>", 1)[0]
    assert "focus-pill" not in plain and "figures-title" not in plain


# ---------- hashtag and topic graphs ----------


@pytest.mark.parametrize(
    "raw, parsed",
    [
        (f"hashtag:{TAG}", ("hashtag", f"hashtag:{TAG}", f"hashtag-{TAG}")),
        (f"#{TAG}", ("hashtag", f"hashtag:{TAG}", f"hashtag-{TAG}")),
        ("topic:ec2", ("topic", "topic:ec2", "topic-ec2")),
        ("Topic:ec2", ("topic", "topic:ec2", "topic-ec2")),
        # the graph's own id: what a scheduled refresh and Rerun replay
        (f"hashtag-{TAG}", ("hashtag", f"hashtag:{TAG}", f"hashtag-{TAG}")),
        # a hashtag that is all digits is not a ticket "HASHTAG-123"
        ("hashtag:123", ("hashtag", "hashtag:123", "hashtag-123")),
    ],
)
def test_hashtags_and_topics_are_identifiers(raw, parsed):
    assert _parse_identifier(raw) == parsed
    assert is_valid_graph_id(parsed[2]) and GRAPH_FILE_RE.match(parsed[2] + ".html")


@pytest.mark.parametrize(
    "raw", ["hashtag:", "#", "hashtag:a b", "hashtag:../x", "topic:x;y", "hashtag:" + "a" * 101]
)
def test_bad_names_are_refused(raw):
    assert _parse_identifier(raw) == (None, None, None)


def test_ids_stay_apart_from_changes_and_tickets():
    assert _parse_identifier("62796")[2] == "62796"
    assert _parse_identifier("LU-12345")[2] == "LU-12345"
    assert not is_valid_graph_id("hashtag:x") and not is_valid_graph_id("#x")
    assert not is_valid_graph_id("HASHTAG-x"), "the prefix is lowercase"


def _run(app, params):
    with app.app_context():
        gc_graph.run_gc_graph(params, FakeSocket(), "room1")
    return FakePopen.last_cmd


def test_a_hashtag_graph_is_built_like_a_ticket_graph(app, graph_dir, fake_gen, tmp_path):
    clone = tmp_path / "clone.git"
    clone.mkdir()
    app.config["GRAPH_CONFLICTS_REPO"] = str(clone)
    cmd = _run(app, {"change_number": f"#{TAG}", "_internal_access": False, "branch": "b2_15"})
    assert cmd[2] == f"hashtag:{TAG}"
    assert "--cross-project" not in cmd
    assert "--branch" not in cmd, "another branch needs the internal role"
    assert cmd[cmd.index("--conflicts") + 1] == str(clone)
    e = get_entry(graph_dir, f"hashtag-{TAG}")
    assert e["name"] == f"#{TAG}" and e["ticket"] == ""
    assert e["anchor_change_number"] == "62796", "from gc's final line"
    assert e["file"] == f"hashtag-{TAG}.html"


def test_an_internal_topic_graph_is_classified_after_the_run(app, graph_dir, fake_gen):
    fake_gen["project"] = "internal/example-project"
    cmd = _run(app, {"change_number": "topic:ec2", "_internal_access": True, "branch": "b2_15"})
    assert cmd[2] == "topic:ec2" and cmd[cmd.index("--branch") + 1] == "b2_15"
    assert "--cross-project" not in cmd
    e = get_entry(graph_dir, "topic-ec2")
    assert e["project"] == "internal/example-project" and e["name"] == "topic ec2"


def test_a_scheduled_refresh_replays_a_hashtag_graph(app, graph_dir, fake_gen):
    from portal.refresh import _refresh_one

    add_entry(graph_dir, f"hashtag-{TAG}", name="Prio", params={"change_number": f"#{TAG}"})
    key, ok = _refresh_one(app, get_entry(graph_dir, f"hashtag-{TAG}"))
    assert ok and key == f"hashtag-{TAG}"
    assert FakePopen.last_cmd[2] == f"hashtag:{TAG}"
    assert get_entry(graph_dir, f"hashtag-{TAG}")["name"] == "Prio", "a custom name stays"


def test_the_list_links_a_hashtag_graph_to_the_gerrit_search(client, graph_dir):
    add_entry(graph_dir, f"hashtag-{TAG}", name=f"#{TAG}", anchor_change_number="68000")
    body = client.get("/gerrit_vis/").data.decode()
    row = body.split(f'id="g-hashtag-{TAG}"', 1)[1].split("</tbody>", 1)[0]
    assert f"/q/hashtag:{TAG}" in row and "/+/68000" not in row
    assert client.get(f"/gerrit_vis/row/hashtag-{TAG}").status_code == 200


def test_portal_refresh_regenerates_named_graphs_scheduled_or_not(
    app, graph_dir, fake_gen, monkeypatch
):
    from portal import refresh

    add_entry(graph_dir, "67069", name="Prio", params={"include_hashtag": TAG})
    add_entry(graph_dir, "100", name="Other")
    monkeypatch.setattr("portal.app.create_app", lambda: app)
    assert refresh.main(["--graph", "67069"]) == 0
    assert FakePopen.last_cmd[2] == "67069"
    assert FakePopen.last_cmd[FakePopen.last_cmd.index("--include-hashtag") + 1] == TAG
    assert "next_refresh" not in get_entry(graph_dir, "67069"), "no schedule made up"
    assert refresh.main(["--graph", "nope"]) == 2
