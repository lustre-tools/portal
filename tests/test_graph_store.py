"""Tests for graph_store: index management, schedule, labels, stats."""

import json
import os

from portal.graph_store import (
    add_entry,
    delete_entry,
    get_due_entries,
    get_entry,
    list_entries,
    update_schedule,
)


def test_add_entry_creates_index_and_entry(graph_dir):
    add_entry(graph_dir, "12345", name="my graph", subject="LU-1 foo", ticket="LU-1")
    entries = list_entries(graph_dir)
    assert len(entries) == 1
    assert entries[0]["change_number"] == "12345"
    assert entries[0]["name"] == "my graph"
    assert entries[0]["ticket"] == "LU-1"
    assert "generated_at" in entries[0]


def test_add_entry_replaces_existing(graph_dir):
    add_entry(graph_dir, "12345", name="first")
    add_entry(graph_dir, "12345", name="second")
    entries = list_entries(graph_dir)
    assert len(entries) == 1
    assert entries[0]["name"] == "second"


def test_add_entry_inserts_newest_first(graph_dir):
    add_entry(graph_dir, "111", name="a")
    add_entry(graph_dir, "222", name="b")
    add_entry(graph_dir, "333", name="c")
    entries = list_entries(graph_dir)
    assert [e["change_number"] for e in entries] == ["333", "222", "111"]


def test_add_entry_preserves_schedule_on_replace(graph_dir):
    add_entry(graph_dir, "555", name="orig")
    update_schedule(graph_dir, "555", interval_hours=24)
    add_entry(graph_dir, "555", name="updated")
    entry = get_entry(graph_dir, "555")
    assert entry["refresh_interval_hours"] == 24
    assert "next_refresh" in entry


def test_add_entry_preserves_labels_when_none_passed(graph_dir):
    add_entry(graph_dir, "777", name="x", labels=["alpha", "beta"])
    add_entry(graph_dir, "777", name="y")  # no labels arg
    entry = get_entry(graph_dir, "777")
    assert entry["labels"] == ["alpha", "beta"]


def test_add_entry_overrides_labels_when_passed(graph_dir):
    add_entry(graph_dir, "777", labels=["alpha"])
    add_entry(graph_dir, "777", labels=["gamma"])
    entry = get_entry(graph_dir, "777")
    assert entry["labels"] == ["gamma"]


def test_add_entry_preserves_stats_when_none_passed(graph_dir):
    stats = {
        "inflight": 3,
        "ready": 1,
        "merged": 2,
        "abandoned": 0,
        "pending": 2,
        "blocked": 0,
        "node_count": 5,
    }
    add_entry(graph_dir, "888", stats=stats)
    add_entry(graph_dir, "888", name="updated")
    entry = get_entry(graph_dir, "888")
    assert entry["stats"] == stats


def test_add_entry_touch_generated_at_false_preserves_timestamp(graph_dir):
    add_entry(graph_dir, "999")
    original = get_entry(graph_dir, "999")["generated_at"]
    # Sleep would be needed to detect change; just verify the flag works
    add_entry(graph_dir, "999", name="newname", touch_generated_at=False)
    entry = get_entry(graph_dir, "999")
    assert entry["generated_at"] == original


def test_delete_entry_removes_from_index_and_html(graph_dir):
    add_entry(graph_dir, "100")
    html_path = os.path.join(graph_dir, "100.html")
    with open(html_path, "w") as f:
        f.write("<html></html>")
    delete_entry(graph_dir, "100")
    assert get_entry(graph_dir, "100") is None
    assert not os.path.exists(html_path)


def test_delete_entry_nonexistent_is_noop(graph_dir):
    add_entry(graph_dir, "200")
    delete_entry(graph_dir, "999")  # does not exist
    assert get_entry(graph_dir, "200") is not None


def test_list_entries_text_query_matches_change_number(graph_dir):
    add_entry(graph_dir, "12345", name="a", ticket="LU-1")
    add_entry(graph_dir, "67890", name="b", ticket="LU-2")
    results = list_entries(graph_dir, query="123")
    assert len(results) == 1
    assert results[0]["change_number"] == "12345"


def test_list_entries_query_matches_ticket(graph_dir):
    add_entry(graph_dir, "1", ticket="LU-99")
    add_entry(graph_dir, "2", ticket="LU-100")
    results = list_entries(graph_dir, query="LU-99")
    assert [r["change_number"] for r in results] == ["1"]


def test_list_entries_query_matches_label(graph_dir):
    add_entry(graph_dir, "1", labels=["pcc", "ec"])
    add_entry(graph_dir, "2", labels=["wbc"])
    results = list_entries(graph_dir, query="pcc")
    assert [r["change_number"] for r in results] == ["1"]


def test_list_entries_label_filter_single(graph_dir):
    add_entry(graph_dir, "1", labels=["alpha"])
    add_entry(graph_dir, "2", labels=["beta"])
    add_entry(graph_dir, "3", labels=["alpha", "beta"])
    results = list_entries(graph_dir, labels=["alpha"])
    assert sorted(r["change_number"] for r in results) == ["1", "3"]


def test_list_entries_label_filter_multi_and(graph_dir):
    add_entry(graph_dir, "1", labels=["alpha"])
    add_entry(graph_dir, "2", labels=["alpha", "beta"])
    add_entry(graph_dir, "3", labels=["alpha", "beta", "gamma"])
    results = list_entries(graph_dir, labels=["alpha", "beta"])
    assert sorted(r["change_number"] for r in results) == ["2", "3"]


def test_list_entries_label_filter_combined_with_query(graph_dir):
    add_entry(graph_dir, "1", name="foo", labels=["alpha"])
    add_entry(graph_dir, "2", name="bar", labels=["alpha"])
    results = list_entries(graph_dir, query="foo", labels=["alpha"])
    assert [r["change_number"] for r in results] == ["1"]


def test_list_entries_empty(graph_dir):
    assert list_entries(graph_dir) == []


def test_update_schedule_sets_interval_and_next_refresh(graph_dir):
    add_entry(graph_dir, "1")
    update_schedule(graph_dir, "1", interval_hours=12)
    entry = get_entry(graph_dir, "1")
    assert entry["refresh_interval_hours"] == 12
    assert "next_refresh" in entry


def test_update_schedule_zero_clears(graph_dir):
    add_entry(graph_dir, "1")
    update_schedule(graph_dir, "1", interval_hours=24)
    update_schedule(graph_dir, "1", interval_hours=0)
    entry = get_entry(graph_dir, "1")
    assert "refresh_interval_hours" not in entry
    assert "next_refresh" not in entry


def test_transfer_schedule_copies_fields(graph_dir):
    from portal.graph_store import transfer_schedule

    add_entry(graph_dir, "old")
    add_entry(graph_dir, "new")
    update_schedule(graph_dir, "old", interval_hours=24)
    transfer_schedule(graph_dir, "old", "new")
    new_entry = get_entry(graph_dir, "new")
    old_entry = get_entry(graph_dir, "old")
    assert new_entry["refresh_interval_hours"] == 24
    assert "next_refresh" in new_entry
    # Source still has the schedule (transfer copies, doesn't move)
    assert old_entry["refresh_interval_hours"] == 24


def test_transfer_schedule_noop_when_no_source_schedule(graph_dir):
    from portal.graph_store import transfer_schedule

    add_entry(graph_dir, "old")
    add_entry(graph_dir, "new")
    transfer_schedule(graph_dir, "old", "new")
    assert "refresh_interval_hours" not in get_entry(graph_dir, "new")


def test_transfer_schedule_noop_when_destination_missing(graph_dir):
    from portal.graph_store import transfer_schedule

    add_entry(graph_dir, "old")
    update_schedule(graph_dir, "old", interval_hours=24)
    transfer_schedule(graph_dir, "old", "missing")
    # Just ensure it doesn't blow up; old entry untouched
    assert get_entry(graph_dir, "old")["refresh_interval_hours"] == 24


def test_get_due_entries(graph_dir):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")

    add_entry(graph_dir, "due1")
    add_entry(graph_dir, "due2")
    add_entry(graph_dir, "future")

    # Manually set next_refresh to past for due entries, future for the third
    path = os.path.join(graph_dir, "index.json")
    with open(path) as f:
        entries = json.load(f)
    past = (datetime.now(tz) - timedelta(hours=1)).isoformat()
    future = (datetime.now(tz) + timedelta(hours=1)).isoformat()
    for e in entries:
        if e["change_number"] in ("due1", "due2"):
            e["next_refresh"] = past
        elif e["change_number"] == "future":
            e["next_refresh"] = future
    with open(path, "w") as f:
        json.dump(entries, f)

    due = get_due_entries(graph_dir)
    due_nums = sorted(e["change_number"] for e in due)
    assert due_nums == ["due1", "due2"]


def test_index_file_locking_concurrent_writes(graph_dir):
    """Sanity check: many quick add_entry calls don't corrupt the index."""
    import threading

    errors = []

    def add(n):
        try:
            for i in range(5):
                add_entry(graph_dir, str(n * 100 + i), name=f"n{n}-{i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    entries = list_entries(graph_dir)
    assert len(entries) == 20
    # Confirm valid JSON on disk
    with open(os.path.join(graph_dir, "index.json")) as f:
        json.load(f)
