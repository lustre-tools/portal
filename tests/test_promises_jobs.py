"""The pipelines, offline: Gerrit, git and Claude stubbed out."""

import pytest

from portal.promises import claude, jobs, judge
from portal.promises.store import Store
from tests.promises_helpers import PUBLIC, adjudication, change_doc, classification, th


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    return jobs.Settings(
        root=tmp_path / "promises",
        gerrit_url="https://review.example.com",
        public_project=PUBLIC,
        ticket_prefix="LU",
        classify_batch=2,
        claude=judge.ClaudeSettings(
            binary="/bin/true", home=str(tmp_path / "h"), neutral_dir=str(tmp_path / "n")
        ),
    )


@pytest.fixture
def store(settings):
    return Store(settings.root)


def seed(store, doc):
    store.mutate_change(doc["change_number"], lambda d: (d.clear(), d.update(doc)))


class Calls:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []
        self.kwargs = []

    def __call__(self, prompt, **kw):
        self.prompts.append(prompt)
        self.kwargs.append(kw)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return claude.Result(text=answer, usd=0.01, turns=1, model=kw.get("model", ""))


# ---------- classify ----------


def test_classify_skips_checker_threads_and_saves_per_chunk(settings, store, monkeypatch):
    threads = [th(tid=f"t{i}_x") for i in range(3)] + [th(tid="bot_x", mechanical=True)]
    seed(store, change_doc(threads=threads))
    calls = Calls(
        [
            '{"threads": {"t0_x": {"kind": "deferral", "summary": "a"}, "t1_x": {"kind": "none"}}}',
            '{"threads": {"t2_x": {"kind": "open-item", "summary": "c"}}}',
            '{"groups": [{"ids": ["t0_x", "t2_x"], "reason": "same fix"}]}',
        ]
    )
    monkeypatch.setattr(claude, "run", calls)
    doc = jobs.run_classify(settings, store, 64620)
    assert set(doc["classifications"]) == {"t0_x", "t1_x", "t2_x"}
    assert all("bot_x" not in p for p in calls.prompts)
    assert calls.kwargs[0]["tools"] == "", "classification needs no tools"
    assert doc["groups"]["groups"] == [{"ids": ["t0_x", "t2_x"], "reason": "same fix"}]
    assert "t1_x" not in calls.prompts[2], "only promises are grouped"
    assert doc["spend"]["calls"] == 3


def test_the_usage_limit_keeps_the_chunks_already_done(settings, store, monkeypatch):
    seed(store, change_doc(threads=[th(tid=f"t{i}_x") for i in range(4)]))
    monkeypatch.setattr(
        claude,
        "run",
        Calls(
            [
                '{"threads": {"t0_x": {"kind": "none"}, "t1_x": {"kind": "none"}}}',
                claude.QuotaReached("limit"),
            ]
        ),
    )
    with pytest.raises(claude.QuotaReached):
        jobs.run_classify(settings, store, 64620)
    assert set(store.load_change(64620)["classifications"]) == {"t0_x", "t1_x"}


def test_a_missing_answer_is_kept_visible_not_guessed(settings, store, monkeypatch):
    seed(store, change_doc(threads=[th(tid="t0_x")]))
    monkeypatch.setattr(claude, "run", Calls(['{"threads": {}}']))
    doc = jobs.run_classify(settings, store, 64620)
    assert doc["classifications"]["t0_x"]["kind"] == "none"
    assert "returned nothing" in doc["classifications"]["t0_x"]["summary"]


def test_nothing_runs_without_claude(settings, store, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    seed(store, change_doc())
    with pytest.raises(RuntimeError, match="not configured"):
        jobs.run_classify(settings, store, 64620)


# ---------- judge ----------


@pytest.fixture
def workspace(monkeypatch):
    """Skip git: a workspace with the two pins and empty trees."""

    def prepare(settings, doc, workdir, report):
        for sub in ("change", "master", "history", "tickets", "candidates"):
            (workdir / sub).mkdir(parents=True, exist_ok=True)
        return workdir / "mirror.git", {"change_sha": "c" * 40, "master_sha": "m" * 40}

    monkeypatch.setattr(jobs, "_prepare_workspace", prepare)
    monkeypatch.setattr(jobs.repo, "files_changed", lambda *a: ["lustre/mdd/mdd_object.c"])
    monkeypatch.setattr(jobs.repo, "history", lambda *a: "history\n")
    monkeypatch.setattr(jobs.repo, "ticket_log", lambda *a: "tickets\n")
    monkeypatch.setattr(jobs.repo, "diff_by_file", lambda *a: {})
    monkeypatch.setattr(jobs.repo, "fetch_many", lambda mirror, pairs: set(pairs))
    monkeypatch.setattr(jobs.repo, "changed_lines", lambda *a: "")
    monkeypatch.setattr(jobs.repo, "rev_parse", lambda mirror, ref: "f" * 40)
    monkeypatch.setattr(jobs.repo, "show", lambda mirror, sha: "a diff\n")
    monkeypatch.setattr(jobs.Settings, "gerrit", lambda self: NoGerrit())


class NoGerrit:
    def __init__(self, results=None):
        self.results = results or {}
        self.queries = []

    def search_all(self, query, max_results=500, page_size=100, options=None):
        self.queries.append(query)
        for key, value in self.results.items():
            if key in query:
                return value
        return []


def judged_doc(**kw):
    t = th()
    return t, change_doc(threads=[t], classifications={t["id"]: classification(t)}, **kw)


def test_judge_checks_pending_promises_with_read_only_tools(
    settings, store, monkeypatch, workspace
):
    t, doc = judged_doc()
    seed(store, doc)
    calls = Calls(
        [
            '{"verdict": "addressed", "evidence": "fixed in change/lustre/mdd/mdd_object.c:12", "addressed_in": {"kind": "patchset", "ref": "20"}}'
        ]
    )
    monkeypatch.setattr(claude, "run", calls)
    out = jobs.run_judge(settings, store, 64620)
    adj = out["adjudications"][t["id"]]
    assert adj["verdict"] == "addressed" and adj["judged_master"] == "m" * 40
    assert calls.kwargs[0]["tools"] == "Read,Grep,Glob"
    assert "MADE BY:   Marc Vef" in calls.prompts[0]
    assert not list((settings.root / "work").iterdir()), "the workspace is removed afterwards"


def test_server_paths_are_kept_out_of_the_verdict(settings, store, monkeypatch, workspace):
    t, doc = judged_doc()
    seed(store, doc)

    def answer(prompt, **kw):
        return claude.Result(
            text='{"verdict": "still-open", "evidence": "see '
            + kw["cwd"]
            + '/master/lustre/x.c and sk-ant-abcdefghijkl"}'
        )

    monkeypatch.setattr(claude, "run", answer)
    evidence = jobs.run_judge(settings, store, 64620)["adjudications"][t["id"]]["evidence"]
    assert "master/lustre/x.c" in evidence
    assert str(settings.root) not in evidence and "sk-ant-" not in evidence


def test_a_failure_never_replaces_a_usable_verdict(settings, store, monkeypatch, workspace):
    t, doc = judged_doc()
    doc["adjudications"] = {t["id"]: adjudication(t, revision="old")}  # outdated, so due again
    seed(store, doc)
    monkeypatch.setattr(claude, "run", Calls(["not json", "still not json"]))
    out = jobs.run_judge(settings, store, 64620)
    assert out["adjudications"][t["id"]]["evidence"] == "still present at the pinned revision"


def test_a_first_failure_is_stored_as_unclear(settings, store, monkeypatch, workspace):
    t, doc = judged_doc()
    seed(store, doc)
    monkeypatch.setattr(claude, "run", Calls(["nope", "nope"]))
    adj = jobs.run_judge(settings, store, 64620)["adjudications"][t["id"]]
    assert adj["verdict"] == "unclear" and "unparseable" in adj["raw_error"]


@pytest.mark.parametrize(
    "kw, why",
    [
        ({"project": "internal/example-project"}, "only supported for fs/lustre-release"),
        ({"branch": "b2_15"}, "only supported for changes on master"),
    ],
)
def test_checking_is_only_for_master_of_the_public_project(settings, store, kw, why):
    _, doc = judged_doc(**kw)
    seed(store, doc)
    with pytest.raises(RuntimeError, match=why):
        jobs.run_judge(settings, store, 64620)


def test_an_item_decided_meanwhile_costs_no_call(settings, store, monkeypatch, workspace):
    t, doc = judged_doc()
    seed(store, doc)
    store.mutate_overrides(64620, lambda ov: ov["overrides"].update({t["id"]: {"status": "done"}}))
    calls = Calls([])
    monkeypatch.setattr(claude, "run", calls)
    jobs.run_judge(settings, store, 64620)
    assert calls.prompts == []


# ---------- rescan ----------


def test_rescan_stores_the_harvest_and_the_candidates(settings, store, monkeypatch):
    t = th(replies=["I will create a patch in LU-19999."])
    fresh = change_doc(threads=[t])
    monkeypatch.setattr(
        jobs.harvest,
        "harvest_change",
        lambda url, n, mech: {"change_number": n, "harvest": fresh["harvest"]},
    )
    monkeypatch.setattr(jobs.Settings, "gerrit", lambda self: object())
    monkeypatch.setattr(
        jobs.followups,
        "find_candidates",
        lambda client, doc, **kw: {"by_ticket": {"LU-19999": []}, "own_ticket": "LU-19548"},
    )
    doc = jobs.run_add(settings, store, 64620, added_by="alice")
    assert doc["added_by"] == "alice"
    assert doc["candidates"]["own_ticket"] == "LU-19548"
    assert doc["harvest"]["threads"][0]["id"] == t["id"]


# ---------- parallel, cancel, same-ticket follow-ups ----------

import threading  # noqa: E402
import time  # noqa: E402


@pytest.fixture
def wide_gate(monkeypatch):
    monkeypatch.setattr(claude, "GATE", claude.Gate(max_parallel=10, min_free_mb=0))


def many_items(n):
    threads = [th(tid=f"t{i}_x") for i in range(n)]
    return change_doc(
        threads=threads, classifications={t["id"]: classification(t) for t in threads}
    )


def test_checks_run_in_parallel(settings, store, monkeypatch, workspace, wide_gate):
    seed(store, many_items(6))
    settings.claude.parallel = 3
    live, peak, lock = [0], [0], threading.Lock()

    def answer(prompt, **kw):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.2)
        with lock:
            live[0] -= 1
        return claude.Result(text='{"verdict": "still-open"}')

    monkeypatch.setattr(claude, "run", answer)
    out = jobs.run_judge(settings, store, 64620)
    assert len(out["adjudications"]) == 6
    assert peak[0] == 3, "three at a time, as configured"


def test_a_cancel_stops_new_checks_and_keeps_finished_ones(
    settings, store, monkeypatch, workspace, wide_gate
):
    seed(store, many_items(5))
    settings.claude.parallel = 1
    cancel = threading.Event()
    calls = []

    def answer(prompt, **kw):
        calls.append(1)
        cancel.set()  # pressed while the first one runs
        return claude.Result(text='{"verdict": "addressed"}')

    monkeypatch.setattr(claude, "run", answer)
    with pytest.raises(claude.Cancelled):
        jobs.run_judge(settings, store, 64620, cancel=cancel)
    assert len(calls) == 1
    assert len(store.load_change(64620)["adjudications"]) == 1
    assert not list((settings.root / "work").iterdir())


def test_the_usage_limit_stops_parallel_checks(settings, store, monkeypatch, workspace, wide_gate):
    seed(store, many_items(4))
    settings.claude.parallel = 1

    def answer(prompt, **kw):
        raise claude.QuotaReached("limit")

    monkeypatch.setattr(claude, "run", answer)
    with pytest.raises(claude.QuotaReached):
        jobs.run_judge(settings, store, 64620)
    assert store.load_change(64620)["adjudications"] == {}


def test_classify_batches_run_in_parallel(settings, store, monkeypatch, wide_gate):
    seed(store, change_doc(threads=[th(tid=f"t{i}_x") for i in range(6)]))
    settings.claude.parallel = 3
    live, peak, lock = [0], [0], threading.Lock()

    def answer(prompt, **kw):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.2)
        with lock:
            live[0] -= 1
        ids = [line.split()[2] for line in prompt.splitlines() if line.startswith("=== thread")]
        return claude.Result(
            text='{"threads": {' + ",".join(f'"{i}": {{"kind": "none"}}' for i in ids) + "}}"
        )

    monkeypatch.setattr(claude, "run", answer)
    doc = jobs.run_classify(settings, store, 64620)
    assert len(doc["classifications"]) == 6 and peak[0] == 3


def test_the_check_is_told_about_open_changes_on_the_same_ticket():
    from portal.promises import status

    t = th()
    found = {
        "own_ticket": "LU-19548",
        "by_ticket": {
            "LU-19548": [
                {
                    "number": 68697,
                    "status": "NEW",
                    "subject": "LU-19548 mdd: refuse",
                    "updated": "2026-09-07 19:52:00.000",
                },
                {
                    "number": 60000,
                    "status": "NEW",
                    "subject": "older",
                    "updated": "2026-01-01 00:00:00.000",
                },
                {
                    "number": 61000,
                    "status": "ABANDONED",
                    "subject": "dropped",
                    "updated": "2026-09-10 00:00:00.000",
                },
                {
                    "number": 64620,
                    "status": "MERGED",
                    "subject": "itself",
                    "updated": "2026-09-10 00:00:00.000",
                },
            ]
        },
    }
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)}, candidates=found)
    item = status.build_items(doc, {"overrides": {}, "manual_items": {}})[0][0]
    assert item.candidates == [], "not listed under the item on the page"
    got = jobs.judge_candidates(item, doc, "LU")
    assert [(c.number, c.reason) for c in got] == [(68697, "same ticket LU-19548")]


def test_the_registry_cancels_a_job():
    reg = jobs.JobRegistry()
    started = threading.Event()

    def fn(progress, cancel):
        started.set()
        while not cancel.is_set():
            time.sleep(0.01)
        raise claude.Cancelled("stop")

    assert reg.start(1, "check promises", fn)
    started.wait(1)
    assert reg.cancel(1)
    for _ in range(100):
        if reg.snapshot()["1"]["state"] != "running":
            break
        time.sleep(0.01)
    assert reg.snapshot()["1"]["state"] == "cancelled"
    assert not reg.cancel(1), "nothing left to cancel"


def test_group_answers_are_sanitised(settings, monkeypatch):
    from portal.promises import status

    doc = many_items(3)
    items, _ = status.build_items(doc, {"overrides": {}, "manual_items": {}})
    answer = '{"groups": [{"ids": ["t0_x", "t1_x", "t0_x"]}, {"ids": ["t1_x", "t2_x"]}, {"ids": ["nope", "t2_x"]}, {"ids": ["t2_x"]}]}'
    monkeypatch.setattr(claude, "run", Calls([answer]))
    got = judge.group_duplicates(settings.claude, doc, items)
    assert got == [{"ids": ["t0_x", "t1_x"], "reason": ""}], (
        "known ids, each once, groups of two or more"
    )


def test_no_grouping_call_when_nothing_changed(settings, store, monkeypatch):
    from portal.promises import status

    doc = many_items(3)
    items, _ = status.build_items(
        doc, {"overrides": {}, "manual_items": {}}, settings.claude.judge_model
    )
    doc["groups"] = {
        "prompt_version": "g1",
        "input_hash": status.group_input_hash(items),
        "groups": [],
    }
    seed(store, doc)
    calls = Calls([])
    monkeypatch.setattr(claude, "run", calls)
    jobs.run_classify(settings, store, 64620)
    assert calls.prompts == []


def test_the_check_of_a_primary_sees_its_duplicates(
    settings, store, monkeypatch, workspace, wide_gate
):
    doc = many_items(2)
    doc["groups"] = {"prompt_version": "g1", "groups": [{"ids": ["t0_x", "t1_x"]}]}
    doc["harvest"]["threads"][1]["patch_set"] = 25
    doc["harvest"]["threads"][0]["root"]["message"] = "the earlier wording of it"
    seed(store, doc)
    calls = Calls(['{"verdict": "still-open"}'])
    monkeypatch.setattr(claude, "run", calls)
    out = jobs.run_judge(settings, store, 64620)
    assert list(out["adjudications"]) == ["t1_x"], "one check for the group"
    assert (
        "also raised in other threads" in calls.prompts[0]
        and "the earlier wording of it" in calls.prompts[0]
    )


# ---------- audit fixes ----------


def test_an_unexpected_error_stops_the_other_checks(
    settings, store, monkeypatch, workspace, wide_gate
):
    seed(store, many_items(5))
    settings.claude.parallel = 1
    calls = []

    def answer(prompt, **kw):
        calls.append(1)
        raise RuntimeError("a bug")

    monkeypatch.setattr(claude, "run", answer)
    with pytest.raises(RuntimeError, match="a bug"):
        jobs.run_judge(settings, store, 64620)
    assert len(calls) == 1, "no more spending after a bug"


def test_one_failed_batch_keeps_the_others_and_says_so(settings, store, monkeypatch, wide_gate):
    seed(store, change_doc(threads=[th(tid=f"t{i}_x") for i in range(4)]))
    settings.claude.parallel = 1

    def answer(prompt, **kw):
        if "t0_x" in prompt:
            raise claude.ClaudeError("claude exited 1: boom")
        ids = [line.split()[2] for line in prompt.splitlines() if line.startswith("=== thread")]
        return claude.Result(
            text='{"threads": {' + ",".join(f'"{i}": {{"kind": "none"}}' for i in ids) + "}}"
        )

    monkeypatch.setattr(claude, "run", answer)
    with pytest.raises(claude.ClaudeError, match="1 of 2 failed; the rest is kept"):
        jobs.run_classify(settings, store, 64620)
    assert set(store.load_change(64620)["classifications"]) == {"t2_x", "t3_x"}


def test_the_check_is_given_the_whole_follow_up_pool_most_likely_first():
    from portal.promises import status

    t = th(tid="aa_1", replies=["Those AIO tests are added to the always_except list."])
    other = th(
        tid="bb_2", replies=['I filed LU-20566 "recover data from parity for DIO+AIO" for this.']
    )
    doc = change_doc(
        number=62757,
        threads=[t, other],
        classifications={x["id"]: classification(x) for x in (t, other)},
        candidates={
            "own_ticket": "LU-19548",
            "stacked": [
                {"number": 68339, "status": "NEW", "subject": "serialize readers"},
                {"number": 64079, "status": "ABANDONED"},
            ],
            "by_ticket": {
                "LU-20566": [
                    {
                        "number": 67878,
                        "status": "NEW",
                        "subject": "recover O_DIRECT",
                        "created": "2026-09-01 00:00:00",
                    }
                ],
                "LU-19548": [
                    {
                        "number": 69000,
                        "status": "NEW",
                        "subject": "sibling",
                        "updated": "2026-09-20 00:00:00",
                    }
                ],
            },
        },
    )
    item = {i.tid: i for i in status.build_items(doc, {"overrides": {}, "manual_items": {}})[0]}[
        "aa_1"
    ]
    assert item.candidates == [], "nothing of its own: the thread names no ticket or change"
    pool = jobs.judge_candidates(item, doc, "LU")
    assert [(c.number, c.reason) for c in pool] == [
        (68339, "stacked on this change"),
        (67878, "promised in LU-20566"),
        (69000, "same ticket LU-19548"),
    ]


def test_the_prompt_lists_the_follow_ups_and_the_new_verdict(
    settings, store, monkeypatch, workspace, wide_gate
):
    doc = many_items(1)
    doc["candidates"] = {
        "stacked": [
            {"number": 67878, "status": "NEW", "subject": "LU-20566 ec: recover O_DIRECT reads"}
        ]
    }
    seed(store, doc)
    calls = Calls(
        ['{"verdict": "in-followup", "addressed_in": {"kind": "change", "ref": "67878"}}']
    )
    monkeypatch.setattr(claude, "run", calls)
    out = jobs.run_judge(settings, store, 64620)
    prompt = calls.prompts[0]
    assert "67878" in prompt and "stacked on this change" in prompt and '"in-followup"' in prompt
    assert out["adjudications"]["t0_x"]["verdict"] == "in-followup"


# ---------- what a check looks at ----------


def aio_doc():
    """62757's shape: a promise on the commit message about tests the
    change parked in always_except under another ticket."""
    t = th(
        tid="91cfac09_e607d390",
        file_path="/COMMIT_MSG",
        line=50,
        root_msg="those AIO tests are added to the always_except list.",
        replies=[],
    )
    cls = classification(
        t,
        summary="Re-enable the AIO tests currently parked in the always_except list",
        quote=None,
    )
    return change_doc(
        number=62757,
        threads=[t],
        classifications={t["id"]: cls},
        subject="LU-12669 ec: recover data from parity",
        candidates={"change": {"created": "2025-11-27 03:24:38.000000000"}},
    )


def _gerrit_change(n, subject, updated="2026-09-01 00:00:00.000000000", ps=3):
    return {
        "_number": n,
        "status": "NEW",
        "subject": subject,
        "updated": updated,
        "created": "2026-08-10 00:00:00.000000000",
        "current_revision": "r",
        "revisions": {"r": {"_number": ps}},
    }


@pytest.fixture
def aio_world(monkeypatch, workspace):
    gerrit = NoGerrit(
        {
            'message:"LU-20566"': [_gerrit_change(67878, "LU-20566 ec: recover O_DIRECT reads")],
            'path:"lustre/tests/sanity-ec.sh"': [
                _gerrit_change(68204, "LU-19631 tests: enable sanity-ec 12a"),
                _gerrit_change(65921, "LU-19548 lfs: mirror extend support for EC", "2026-09-28"),
                _gerrit_change(64919, "LU-12668 lov: slow OST detection"),
            ],
        }
    )
    monkeypatch.setattr(jobs.Settings, "gerrit", lambda self: gerrit)
    monkeypatch.setattr(
        jobs.repo,
        "diff_by_file",
        lambda *a: {
            "lustre/tests/sanity-ec.sh": "+always_except LU-20566 41j 41k 41l 41m 41n",
            "lustre/llite/file.c": "+\treturn -EIO;",
        },
    )
    lines = {68204: "-always_except LU-19631 12a", 64919: "+\tlocal aio=$DIR/$tfile.aio"}
    monkeypatch.setattr(
        jobs.repo,
        "changed_lines",
        lambda mirror, ref, paths=None: lines.get(int(ref.split("/")[-1].split("-")[0]), ""),
    )
    return gerrit


def test_a_promise_on_the_commit_message_is_checked_against_its_file_and_ticket(
    settings, store, monkeypatch, aio_world
):
    seed(store, aio_doc())
    calls = Calls(['{"verdict": "still-open", "related": ["67878", "x"]}'])
    monkeypatch.setattr(claude, "run", calls)
    out = jobs.run_judge(settings, store, 62757)
    adj = out["adjudications"]["91cfac09_e607d390"]
    got = [(c["number"], c["reason"]) for c in adj["looked_at"]]
    assert got[0][0] == 67878 and got[0][1].startswith(
        "LU-20566, left in lustre/tests/sanity-ec.sh"
    )
    assert got[1] == (68204, "touches lustre/tests/sanity-ec.sh (always_except)"), (
        "a change whose diff has what the promise is about comes before one that only "
        "touches the file"
    )
    assert {n for n, _ in got[2:]} == {65921, 64919}, "the rest, fewer of them"
    assert adj["depth"] == "normal" and adj["related"] == [67878]
    assert "67878" in calls.prompts[0] and "deeper check" not in calls.prompts[0]
    assert all(f'project:"{PUBLIC}"' in q and "-change:62757" in q for q in aio_world.queries), (
        "every search stays in the change's project"
    )
    assert any("after:2026-08-28" in q for q in aio_world.queries if "path:" in q), (
        "a quick check looks at files changed since the promise"
    )


def test_a_deeper_check_searches_further_back_and_thinks_harder(
    settings, store, monkeypatch, aio_world
):
    seed(store, aio_doc())
    calls = Calls(['{"verdict": "still-open"}'])
    monkeypatch.setattr(claude, "run", calls)
    out = jobs.run_judge(settings, store, 62757, depth="deep")
    kw = calls.kwargs[0]
    assert kw["effort"] == "high"
    assert kw["budget_usd"] == settings.claude.judge_budget_usd * 2
    assert kw["timeout"] == int(settings.claude.judge_timeout * 1.5)
    assert "deeper check" in calls.prompts[0]
    assert any("after:2025-11-27" in q for q in aio_world.queries if "path:" in q)
    assert any('path:"lustre/llite/file.c"' in q for q in aio_world.queries), (
        "a deeper check also searches the rest of the change's files"
    )
    assert out["adjudications"]["91cfac09_e607d390"]["depth"] == "deep"


def test_preview_shows_what_a_check_would_get_without_claude(
    settings, store, monkeypatch, aio_world
):
    seed(store, aio_doc())
    monkeypatch.setattr(claude, "run", Calls([]))  # any call would fail: no answers
    got = jobs.preview_candidates(settings, store, 62757)
    one = got["91cfac09_e607d390"]
    assert one["files"] == ["lustre/tests/sanity-ec.sh"]
    assert one["tickets"] == ["LU-20566"], "master's log is read for the parked ticket"
    assert [c["number"] for c in one["candidates"]][:2] == [67878, 68204]


def test_the_pool_keeps_open_work_on_a_promised_ticket_even_if_older():
    """An open change nobody touched since the promise is still the
    follow-up; a change that landed before the promise cannot be."""
    from portal.promises import status

    t = th(replies=["Tracked in LU-20566, will do it there."])
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        candidates={
            "by_ticket": {
                "LU-20566": [
                    {"number": 67878, "status": "NEW", "created": "2026-08-10 00:00:00",
                     "updated": "2026-08-12 00:00:00"},
                    {"number": 60000, "status": "MERGED", "created": "2026-01-01 00:00:00",
                     "updated": "2026-01-02 00:00:00"},
                ]
            },
            "fixes": [{"number": 69999, "status": "NEW", "subject": "fix it"}],
        },
    )  # fmt: skip
    item = status.build_items(doc, {"overrides": {}, "manual_items": {}})[0][0]
    pool = jobs.judge_candidates(item, doc, "LU")
    assert [(c.number, c.reason) for c in pool] == [
        (69999, "cites this commit"),
        (67878, "promised in LU-20566"),
    ]


def test_one_umbrella_ticket_does_not_fill_the_pool(monkeypatch):
    """Open changes and the best matches first, a few per ticket."""
    from portal.promises import discover

    entries = [
        (
            {"number": 100 + i, "status": "MERGED", "updated": f"2026-09-{10 + i:02d}"},
            "ticket",
            "LU-12668",
            "LU-12668, left in lov_io.c: ...",
        )
        for i in range(12)
    ] + [({"number": 99, "status": "NEW", "updated": "2026-01-01"}, "ticket", "LU-12668", "x")]
    found = {"idents": {"always_except"}, "files": [], "tickets": ["LU-12668"], "entries": entries}
    got = jobs._rank(found, None, set(), discover.DEPTHS["normal"])
    assert len(got) == discover.DEPTHS["normal"].per_ticket
    assert 99 in [c["number"] for c in got], "the open one is kept, however old"


def test_a_change_found_twice_says_both_and_old_merges_are_dropped():
    from portal.promises import status

    t = th(replies=["Tracked in LU-20566, will do it there."])
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        candidates={
            "stacked": [{"number": 67878, "status": "NEW", "subject": "recover O_DIRECT"}],
            "by_ticket": {"LU-20566": [{"number": 67878, "status": "NEW"}]},
        },
    )
    item = status.build_items(doc, {"overrides": {}, "manual_items": {}})[0][0]
    found = [
        {"number": 67878, "status": "NEW", "reason": "LU-20566, left in sanity-ec.sh: ..."},
        {"number": 62000, "status": "MERGED", "updated": "2026-01-02 00:00:00", "reason": "old"},
    ]
    pool = jobs.judge_candidates(item, doc, "LU", found=found)
    assert [(c.number, c.reason) for c in pool] == [
        (
            67878,
            "names LU-20566; stacked on this change; LU-20566, left in sanity-ec.sh: ...",
        )
    ]


def test_rescan_refreshes_the_changes_the_checks_named(settings, store, monkeypatch):
    t = th()
    doc = change_doc(
        threads=[t],
        adjudications={
            t["id"]: adjudication(
                t,
                verdict="in-followup",
                addressed_in={"kind": "change", "ref": "68204", "detail": ""},
                related=[67878, 64620],
            )
        },
    )
    seed(store, doc)
    monkeypatch.setattr(
        jobs.harvest,
        "harvest_change",
        lambda url, n, mech: {"change_number": n, "harvest": doc["harvest"]},
    )
    monkeypatch.setattr(jobs.Settings, "gerrit", lambda self: object())
    monkeypatch.setattr(jobs.followups, "find_candidates", lambda client, d, **kw: {})
    asked = []
    monkeypatch.setattr(
        jobs.followups,
        "fetch_linked_changes",
        lambda client, nums: asked.extend(nums) or {str(n): {"status": "MERGED"} for n in nums},
    )
    out = jobs.run_rescan(settings, store, 64620)
    assert sorted(set(asked)) == [67878, 68204], "not the change itself"
    assert out["linked_changes"]["68204"]["status"] == "MERGED"
