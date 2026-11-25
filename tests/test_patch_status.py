"""Patch Status: the landing rules, the anonymous fetch, the page body."""

import json
import re
from concurrent.futures import Future

import pytest

from portal.patch_status import fetch as fetch_mod
from portal.patch_status.classify import (
    BUCKET_ORDER,
    FAILS,
    IN_LINE_FOR_LANDING,
    NEEDS_REBASE,
    NEEDS_REVIEW,
    NEEDS_VERIFY,
    READY_TO_LAND,
    TEST_ONLY,
    classify,
    is_most_wanted,
)
from portal.patch_status.comment import comment_for
from portal.patch_status.fetch import (
    AnonymousGerrit,
    FetchError,
    add_related,
    extract_ticket,
    fetch_staged,
    is_rebase_vote,
    mark_staged,
    search_open,
    trim,
)
from portal.patch_status.orgs import Orgs
from portal.patch_status.render import COLUMNS, Page

GERRIT = "https://review.example.org"
JIRA = "https://jira.example.org"
NOOP = lambda *a: None  # noqa: E731

CI = [(683, 1, "jenkins"), (684, 1, "Maloo")]
TWO = [(10, 1), (20, 1)]


def change(*, number=100, subject="LU-1000 mdt: do a thing", owner_id=1,
           owner_email="owner@example.com", owner_name="Owner Person",
           verify_votes=CI, review_votes=TWO, messages=None, mergeable=True,
           hashtags=None, topic=None, created="2026-01-01 00:00:00.000000000",
           updated="2026-05-01 00:00:00.000000000"):
    def votes(items):
        out = []
        for v in items:
            acc, score, name = v if len(v) == 3 else (*v, f"user{v[0]}")
            out.append({"_account_id": acc, "value": score, "name": name})
        return {"all": out}

    return {
        "_number": number,
        "subject": subject,
        "project": "fs/lustre-release",
        "branch": "master",
        "status": "NEW",
        "owner": {"_account_id": owner_id, "email": owner_email, "name": owner_name},
        "labels": {"Verified": votes(verify_votes), "Code-Review": votes(review_votes)},
        "messages": messages or [],
        "current_revision": "rev",
        "current_revision_number": 1,
        "revisions": {"rev": {"_number": 1, "created": updated, "commit": {"message": subject}}},
        "mergeable": mergeable,
        "dependees_ready": True,
        "hashtags": hashtags or [],
        "topic": topic,
        "created": created,
        "updated": updated,
        "_related": [],
        "_jira_ticket": extract_ticket(subject),
    }


def add_review(ch, name, value, acc=900):
    ch["labels"]["Code-Review"]["all"].append({"_account_id": acc, "name": name, "value": value})
    return ch


# ---------- rules ----------

def test_ready_patch():
    ch = change()
    classify([ch])
    assert ch["bucket"] == READY_TO_LAND
    assert comment_for(ch) == "ready"


def test_a_ready_patch_on_the_landing_branch_is_in_line():
    ch = change()
    ch["_staged"] = True
    classify([ch])
    assert ch["bucket"] == IN_LINE_FOR_LANDING
    assert comment_for(ch) == ""
    ch["unresolved_comment_count"] = 2
    assert comment_for(ch) == "2 unresolved"
    assert ch["votes"]["r"] == 1  # nobody voted +2


def test_the_branch_overrides_a_stale_hashtag():
    ch = change(hashtags=["master-next"])
    ch["_staged"] = False
    classify([ch])
    assert ch["bucket"] == READY_TO_LAND


def test_the_hashtag_stands_in_when_the_branch_was_not_read():
    ch = change(hashtags=["master-next"])
    classify([ch])
    assert ch["bucket"] == IN_LINE_FOR_LANDING


def test_plus_two_does_not_queue_a_patch():
    ch = change(review_votes=[(10, 1), (20, 2)])
    classify([ch])
    assert ch["bucket"] == READY_TO_LAND


def test_two_reviews_are_needed_whatever_the_trailers_say():
    ch = change(review_votes=[(10, 1)])
    ch["revisions"]["rev"]["commit"]["message"] += "\n\nLustre-change: https://review.example.org/12345\n"
    classify([ch])
    assert ch["votes"]["_review"]["threshold"] == 2
    assert ch["bucket"] == NEEDS_REVIEW
    assert comment_for(ch) == "needs 1 more review (1/2)"


def test_nobody_stands_in_for_maloo():
    ch = change(verify_votes=[(683, 1, "jenkins"), (1, 1, "A Person")])
    classify([ch])
    assert ch["bucket"] == NEEDS_VERIFY
    assert comment_for(ch) == "waiting on Maloo"


def test_checkpatch_rebase_vote_is_a_rebase_not_a_review():
    ch = add_review(change(), "wc-checkpatch", -1)
    ch["mergeable"] = not is_rebase_vote(ch)
    classify([ch])
    assert ch["bucket"] == NEEDS_REBASE
    assert comment_for(ch) == "conflicts with branch tip"
    assert not ch["votes"]["_review"]["has_any_negative"]


def test_series_inherits_the_rebase_vote_of_its_first_patch():
    related = [{"_change_number": 2, "status": "NEW"}, {"_change_number": 1, "status": "NEW"}]
    bottom = add_review(change(number=1), "wc-checkpatch", -1)
    top = change(number=2)
    for ch in (bottom, top):
        ch["_related"] = related
        ch["mergeable"] = not is_rebase_vote(ch)
    classify([bottom, top])
    assert bottom["bucket"] == top["bucket"] == NEEDS_REBASE
    assert comment_for(top) == "rebase #1 (chain bottom) first"


def test_a_series_waits_for_the_change_below_it():
    related = [{"_change_number": 2, "status": "NEW"}, {"_change_number": 1, "status": "NEW"}]
    bottom = change(number=1, review_votes=[])
    top = change(number=2)
    for ch in (bottom, top):
        ch["_related"] = related
    classify([top, bottom])
    assert bottom["bucket"] == NEEDS_REVIEW
    assert top["bucket"] == "Dependee not ready"
    assert comment_for(top) == "blocked by #1 — needs review"
    assert top["series_position"] == (2, 2)


def test_other_bots_negative_votes_still_count():
    ch = add_review(change(), "Lustre Gerrit Janitor", -1)
    classify([ch])
    assert ch["bucket"] == FAILS
    assert comment_for(ch) == "-1 from Lustre Gerrit Janitor"


def test_a_veto_outranks_a_rebase():
    ch = add_review(change(mergeable=False), "Some Reviewer", -2)
    classify([ch])
    assert comment_for(ch) == "-2 from Some Reviewer"


@pytest.mark.parametrize("tag", ["218_review_blocker", "219_review_priority", "mw"])
def test_release_hashtags_are_wanted(tag):
    assert is_most_wanted(change(hashtags=[tag]))
    assert not is_most_wanted(change(hashtags=["pw_shepherd"]))


@pytest.mark.parametrize("kw", [{"hashtags": ["fortestonly"]}, {"subject": "LU-0000 tests: try"}])
def test_test_only_patches(kw):
    ch = change(**kw)
    classify([ch])
    assert ch["bucket"] == TEST_ONLY


def test_the_ticket_is_the_projects_own():
    assert extract_ticket("LU-123 lnet: UTF-8 names") == "LU-123"
    assert extract_ticket("tests: UTF-8 only") is None


# ---------- fetch ----------

class Reply:
    def __init__(self, body, status=200):
        self.status_code = status
        self.text = ")]}'\n" + json.dumps(body)


def raw(number, **kw):
    return {
        "_number": number, "current_revision": "s", "labels": {}, "messages": [],
        "revisions": {"s": {"_number": 2, "created": "2026-11-01 00:00:00.0",
                            "commit": {"message": "m"}}}, **kw,
    }


def test_gerrit_session_ignores_netrc_and_sends_no_credentials():
    g = AnonymousGerrit(GERRIT)
    assert g.session.trust_env is False and g.session.auth is None
    assert "Authorization" not in g.session.headers


def test_get_retries_server_errors_then_gives_up(monkeypatch):
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda s: None)
    g = AnonymousGerrit(GERRIT)
    replies = [Reply({}, 503), Reply({"ok": 1})]
    monkeypatch.setattr(g.session, "get", lambda *a, **k: replies.pop(0))
    assert g.get("/x") == {"ok": 1}
    monkeypatch.setattr(g.session, "get", lambda *a, **k: Reply({}, 429))
    with pytest.raises(FetchError):
        g.get("/x")


def test_search_pages_and_drops_duplicates():
    class G:
        starts = []

        def get(self, path, params):
            p = dict(x for x in params if x[0] != "o")
            self.starts.append(p["S"])
            return [raw(1), raw(2, _more_changes=True)] if p["S"] == 0 else [raw(2), raw(3)]

    g = G()
    out = search_open(g, "fs/lustre-release", "master", None, NOOP)
    assert [c["_number"] for c in out] == [1, 2, 3] and g.starts == [0, 2]


def test_private_changes_are_neither_asked_for_nor_kept():
    class G:
        def get(self, path, params):
            self.query = dict(x for x in params if x[0] != "o")["q"]
            return [raw(1), raw(2, is_private=True)]

    g = G()
    assert [c["_number"] for c in search_open(g, "fs/lustre-release", "master", None, NOOP)] == [1]
    assert "-is:private" in g.query


class Commits:
    """c3 - c2 - base (no Change-Id) - master"""

    def __init__(self, base=True):
        self.base = base

    def get(self, path):
        if path.endswith("/branches/master-next"):
            return {"revision": "c3"}
        sha = path.rsplit("/", 1)[1]
        n = 3 if sha == "c3" else 2
        if sha == "c1" and self.base:
            return {"message": "Restore memory pressure\n", "parents": [{"commit": "c0"}]}
        return {"message": f"s\n\nChange-Id: I{n:040x}\n",
                "parents": [{"commit": "c2" if n == 3 else "c1"}]}


def test_staged_patches_end_at_the_commit_without_change_id():
    assert fetch_staged(Commits(), "fs/lustre-release", "master-next") == {f"I{3:040x}", f"I{2:040x}"}


def test_unreadable_branch_falls_back_to_the_hashtag():
    f = Future()
    f.set_exception(FetchError("down"))
    ch = {"change_id": "I1"}
    mark_staged([ch], f, "master-next", NOOP)
    assert "_staged" not in ch
    with pytest.raises(FetchError, match="no base commit"):
        fetch_staged(Commits(base=False), "fs/lustre-release", "master-next")


def test_staged_is_matched_by_change_id():
    f = Future()
    f.set_result({"I1"})
    a, b = {"change_id": "I1"}, {"change_id": "I2"}
    mark_staged([a, b], f, "master-next", NOOP)
    assert a["_staged"] and not b["_staged"]


def test_too_many_series_failures_abort():
    class G:
        def get(self, path):
            raise FetchError("boom")

    with pytest.raises(FetchError, match="1 of 1"):
        add_related(G(), [raw(1)], 1, NOOP, NOOP)


def test_trim_keeps_the_style_result_posted_before_a_rebase_notice():
    def msg(text, author="wc-checkpatch", ps=2):
        return {"author": {"name": author}, "_revision_number": ps, "date": "d", "message": text}

    out = trim(raw(1, messages=[
        msg("Looks good to me."), msg("This change cannot be cherry-picked to master."),
        msg("old", ps=1), msg("chatter", author="Someone"),
    ]))
    assert [m["message"] for m in out["messages"]] == ["Looks good to me."]


# ---------- page ----------

def page(*changes, orgs=None):
    classify(list(changes))
    return Page(GERRIT, JIRA, "LU", orgs).render(
        {"branch": "master", "project": "fs/lustre-release", "head": "a" * 40}, list(changes))


def test_the_body_has_no_style_script_or_outside_resources():
    out = page(change())
    assert out.startswith('<div class="ps">')
    assert not re.search(r"<(script|style|link|img)\b", out)
    assert out.count("<td") >= len(COLUMNS) - 1


def test_page_escapes_and_hides_emails_and_other_jira():
    ch = change(subject="EX-1 llite: <script>alert(1)</script>", owner_email="a@b.com",
                hashtags=['"><b>x'])
    ch["_jira_ticket"] = "EX-1"
    out = page(ch)
    assert "<script>alert" not in out and '"><b>x' not in out
    assert "a@b.com" not in out
    assert f"{JIRA}/browse/EX-1" not in out


def test_the_org_column_needs_a_mapping():
    assert "<button type=\"button\">Org</button>" not in page(change())
    orgs = Orgs({"example.com": "Example Corp"}, {"x@gmail.com": "Other Corp"}, {})
    out = page(change(owner_email="dev@mail.example.com"), orgs=orgs)
    assert "<button type=\"button\">Org</button>" in out and ">Example Corp<" in out
    assert orgs.lookup("x@gmail.com") == "Other Corp" and orgs.lookup("y@gmail.com") == "Other"


def test_picked_but_not_ready_patch_is_a_red_row_in_the_queue():
    out = page(change(number=77, review_votes=[], hashtags=["master-next"]))
    queue = out.split(f"<h2>{IN_LINE_FOR_LANDING}")[1].split("</section>")[0]
    assert 'class="blocked"' in queue and "blocked — in: Needs review" in queue
    assert "+ 1 blocked" in queue


def test_blockers_and_priorities_look_different_and_sort_in_that_order():
    out = page(
        change(number=1, updated="2026-11-09 00:00:00.0"),
        change(number=2, hashtags=["218_review_priority"], updated="2026-11-08 00:00:00.0"),
        change(number=3, hashtags=["218_review_priority", "218_review_blocker"]),
    )
    body = out.split(f"<h2>{READY_TO_LAND}")[1].split("</section>")[0]
    rows = re.findall(r'<tr class="([^"]*)" data-ps.*?/\+/(\d+)"', body)
    assert rows == [("is-blocker", "3"), ("is-priority", "2"), ("", "1")]
    assert 'class="badge blocker"' in body and 'class="badge priority"' in body
    assert "Release blockers (1)" in out and "Release priority (1)" in out


def test_head_links_to_the_commit_in_git_web():
    classify([c := change()])
    out = Page(GERRIT, JIRA, "LU", commit_url="https://git.example.org/?p={project}.git;a=commit;h={sha}").render(
        {"branch": "master", "project": "fs/lustre-release", "head": "a" * 40}, [c])
    assert 'href="https://git.example.org/?p=fs/lustre-release.git;a=commit;h=' + "a" * 40 + '"' in out
    assert "gitiles" not in page(change())


def test_every_bucket_has_a_section_and_a_summary_row():
    out = page(change())
    for b in BUCKET_ORDER:
        assert f"<h2>{b} " in out and f">{b}</a></td>" in out
