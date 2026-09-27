"""The store, the spending ledger, the import, and the daily rescan."""

import json

from portal.promises.store import Store
from tests.promises_helpers import change_doc, th


def test_a_job_cannot_bring_back_an_untracked_change(tmp_path):
    store = Store(tmp_path)
    store.mutate_change(1, lambda d: d.update(change_number=1))
    store.archive_change(1)
    store.mutate_change(1, lambda d: d.update(x=1), create=False)
    assert not store.has_change(1)


def test_files_are_private(tmp_path):
    store = Store(tmp_path)
    store.mutate_change(1, lambda d: None)
    assert (tmp_path / "changes" / "1.json").stat().st_mode & 0o077 == 0


def test_spend_is_logged_and_totalled(tmp_path):
    store = Store(tmp_path)
    store.mutate_change(7, lambda d: None)
    store.record_spend(7, "classify", {"usd": 0.25, "model": "sonnet"})
    store.record_spend(7, "judge", {"usd": 1.5})
    store.record_spend(7, "judge", {"usd": 0.5})
    spend = store.load_change(7)["spend"]
    assert (spend["classify"], spend["judge"], spend["calls"]) == (0.25, 2.0, 3)
    assert store.spend_since("2000-01-01T00:00:00Z") == 2.25
    assert store.spend_since("2999-01-01T00:00:00Z") == 0


def test_gerrit_followup_data_imports_with_the_new_fields(tmp_path):
    from portal.promises.cli import import_followup_data

    src = tmp_path / "src"
    (src / "changes").mkdir(parents=True)
    (src / "overrides").mkdir()
    bot = th(tid="b_1", root_author="wc-checkpatch", replies=[])
    human = th(tid="h_1")
    for t in (bot, human):
        t.pop("mechanical")
        t.pop("keyword_hit")
    doc = change_doc(threads=[bot, human])
    doc.pop("candidates")
    (src / "changes" / "64620.json").write_text(json.dumps(doc))
    (src / "overrides" / "64620.json").write_text(
        json.dumps({"schema_version": 1, "overrides": {"h_1": {"status": "done"}}})
    )
    store = Store(tmp_path / "dst")
    assert import_followup_data(src, store, ("wc-checkpatch",)) == [64620]
    threads = {t["id"]: t for t in store.load_change(64620)["harvest"]["threads"]}
    assert threads["b_1"]["mechanical"] and not threads["h_1"]["mechanical"]
    assert threads["h_1"]["keyword_hit"]
    assert store.load_overrides(64620)["overrides"]["h_1"]["status"] == "done"
    assert import_followup_data(src, store, ("wc-checkpatch",)) == [], (
        "never replaces without --force"
    )


def test_the_daily_rescan_only_takes_stale_changes(app_env, monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_PROMISES", "1")
    monkeypatch.setenv("PORTAL_PROMISES_DIR", str(tmp_path / "p"))
    from portal import refresh
    from portal.app import create_app
    from portal.promises import jobs

    app = create_app(testing=True)
    store = Store(tmp_path / "p")
    old = change_doc(number=1)
    fresh = change_doc(number=2)
    fresh["harvest"]["harvested_at"] = "2999-01-01T00:00:00Z"
    for d in (old, fresh):
        store.mutate_change(d["change_number"], lambda x, d=d: (x.clear(), x.update(d)))
    done = []
    monkeypatch.setattr(jobs, "run_rescan", lambda s, st, n, **kw: done.append(n))
    refresh._rescan_promises(app)
    assert done == [1]
