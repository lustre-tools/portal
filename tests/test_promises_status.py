"""Effective status, staleness, and what is derived without any AI."""

from portal.promises import status
from portal.promises.models import JUDGE_PROMPT_VERSION
from tests.promises_helpers import REV, adjudication, change_doc, classification, overrides_doc, th


def build(doc, ov=None, model="opus"):
    return status.build_items(doc, ov or overrides_doc(), model)


def test_unjudged_promise_is_pending():
    t = th()
    items, non = build(change_doc(threads=[t], classifications={t["id"]: classification(t)}))
    assert [it.effective_status for it in items] == ["pending"] and non == []


def test_a_fresh_verdict_decides():
    t = th()
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={t["id"]: adjudication(t, verdict="addressed")},
    )
    assert build(doc)[0][0].effective_status == "addressed"


def test_a_decision_by_hand_beats_the_verdict():
    t = th()
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={t["id"]: adjudication(t)},
    )
    it = build(doc, overrides_doc({t["id"]: {"status": "done"}}))[0][0]
    assert (it.effective_status, it.manual_status) == ("addressed", True)


def test_a_merged_linked_follow_up_beats_the_verdict():
    t = th()
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={t["id"]: adjudication(t)},
        linked={"70000": {"status": "MERGED", "subject": "fix"}},
    )
    it = build(doc, overrides_doc({t["id"]: {"followup_change": 70000}}))[0][0]
    assert it.effective_status == "addressed" and it.addressed_via_followup


def test_a_new_patchset_makes_the_verdict_outdated_but_keeps_it():
    t = th()
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={t["id"]: adjudication(t, revision="old")},
        revision=REV,
    )
    it = build(doc)[0][0]
    assert it.effective_status == "stale" and it.prior_verdict == "still-open"
    assert "newer patchset" in it.stale_info


def test_prompt_and_model_changes_make_the_verdict_outdated():
    t = th()
    old_prompt = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={t["id"]: adjudication(t, prompt_version="j1")},
    )
    assert build(old_prompt)[0][0].effective_status == "stale"
    assert JUDGE_PROMPT_VERSION != "j1"
    other_model = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={t["id"]: adjudication(t, model="sonnet")},
    )
    assert build(other_model, model="opus")[0][0].stale_info == "checked with a different model"


def test_a_model_change_does_not_invalidate_classifications():
    """Classifying costs money; a model upgrade alone must not re-bill
    every change."""
    t = th()
    assert not status.classification_is_stale(
        classification(t, model="whatever-old"), t["text_hash"]
    )
    assert status.classification_is_stale(classification(t), "other-hash")


def test_threads_only_checkers_took_part_in_are_neither_items_nor_listed():
    t = th(mechanical=True, root_author="wc-checkpatch", reply_author="wc-checkpatch")
    items, non = build(change_doc(threads=[t]))
    assert items == [] and non == []
    assert status.unclassified_count(change_doc(threads=[t])) == 0


def test_the_ai_reviewer_counts_as_a_reviewer():
    t = th(root_author="Gerrit AI review for Lustre", replies=[])
    assert status.unclassified_count(change_doc(threads=[t])) == 1


def test_promised_by_is_the_author_of_the_quote():
    t = th(
        replies=["Agreed, but not in this patch.", "Pre-existing, will fix in a follow-up patch."],
        reply_author="Pat Promiser",
    )
    t["replies"][0]["author"] = "Somebody Else"
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t, quote="will fix in a follow-up patch")},
    )
    it = build(doc)[0][0]
    assert it.promised_by == "Pat Promiser"
    assert it.asked_by == "Gerrit AI review for Lustre"


def test_promised_by_is_unknown_without_a_quote_match():
    t = th()
    doc = change_doc(
        threads=[t], classifications={t["id"]: classification(t, quote="words nobody wrote")}
    )
    assert build(doc)[0][0].promised_by is None


def test_a_promise_in_a_resolved_thread_is_flagged():
    t = th(resolved=True)
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)})
    assert build(doc)[0][0].resolved_but_open
    doc["adjudications"] = {t["id"]: adjudication(t, verdict="addressed")}
    assert not build(doc)[0][0].resolved_but_open


def test_tickets_named_in_a_thread_bring_their_candidates():
    t = th(replies=["I will create a patch in LU-19999."])
    found = {
        "by_ticket": {
            "LU-19999": [{"number": 70001, "subject": "LU-19999 mdt: fix", "status": "MERGED"}],
            "LU-19548": [{"number": 1}],
        }
    }
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)}, candidates=found)
    it = build(doc)[0][0]
    assert it.tickets == ["LU-19999"]
    assert [(c.number, c.reason) for c in it.candidates] == [(70001, "names LU-19999")]


def test_the_changes_own_ticket_is_not_a_candidate_per_item():
    """Every patch of a series shares it: listing them under each promise
    says nothing."""
    t = th(replies=["Follow-up under LU-19548."])
    found = {"by_ticket": {"LU-19548": [{"number": 5}]}}
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)}, candidates=found)
    assert build(doc)[0][0].candidates == []


def test_possible_promises_are_unclassified_keyword_hits():
    hit = th(tid="aa_1", keyword_hit=True)
    miss = th(tid="bb_2", keyword_hit=False, replies=["Done."])
    items, non = build(change_doc(threads=[hit, miss]))
    assert [p.tid for p in status.possible_promises(change_doc(threads=[hit, miss]), non)] == [
        "aa_1"
    ]


def test_pending_and_stale_excludes_decisions_by_hand():
    a, b = th(tid="aa_1"), th(tid="bb_2")
    doc = change_doc(
        threads=[a, b], classifications={a["id"]: classification(a), b["id"]: classification(b)}
    )
    ov = overrides_doc({"bb_2": {"status": "ignored"}})
    assert status.pending_and_stale_tids(doc, ov, "opus") == ["aa_1"]


def test_manual_items_are_open_until_decided():
    ov = overrides_doc(manual_items={"m1": {"summary": "port the check", "status": None}})
    items, _ = build(change_doc(threads=[]), ov)
    assert [(it.item_id, it.effective_status) for it in items] == [("64620-m1", "open")]


def test_not_needed_can_be_ticked_off_by_hand():
    t = th()
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={t["id"]: adjudication(t)},
    )
    it = build(doc, overrides_doc({t["id"]: {"status": "not_needed"}}))[0][0]
    assert (it.effective_status, it.status_label, it.bucket) == ("invalid", "Not needed", "ignored")
    assert (
        status.pending_and_stale_tids(
            doc, overrides_doc({t["id"]: {"status": "not_needed"}}), "opus"
        )
        == []
    )


def test_a_follow_up_in_review_counts_as_handled_until_it_merges_or_is_dropped():
    t = th()

    def with_link(state):
        doc = change_doc(
            threads=[t],
            classifications={t["id"]: classification(t)},
            adjudications={t["id"]: adjudication(t)},
            linked={"70001": {"status": state}},
        )
        return build(doc, overrides_doc({t["id"]: {"followup_change": 70001}}))[0][0]

    assert (with_link("NEW").effective_status, with_link("NEW").status_label) == (
        "in_followup",
        "In flight",
    )
    assert with_link("NEW").bucket == "followup", "its own section, not kept yet"
    assert with_link("NEW").followup_in_review.number == 70001
    assert with_link("MERGED").effective_status == "addressed"
    assert with_link("ABANDONED").effective_status == "open", "back to the verdict"


# ---------- duplicates ----------


def grouped_doc(groups, verdict_on=None):
    a = th(tid="aa_1", ps=5, line=10)
    b = th(tid="bb_2", ps=9, line=12)
    c = th(tid="cc_3", ps=7, line=99)
    doc = change_doc(
        threads=[a, b, c], classifications={t["id"]: classification(t) for t in (a, b, c)}
    )
    if verdict_on:
        doc["adjudications"] = {verdict_on: adjudication(b, verdict="addressed")}
    doc["groups"] = {"prompt_version": "g1", "groups": groups}
    return doc


def by(items):
    return {it.tid: it for it in items}


def test_duplicates_fold_into_the_latest_patchset():
    items, _ = build(
        grouped_doc([{"ids": ["aa_1", "bb_2"], "reason": "same MDS check"}], verdict_on="bb_2")
    )
    it = by(items)
    assert it["aa_1"].duplicate_of == "bb_2" and [d.tid for d in it["bb_2"].duplicates] == ["aa_1"]
    assert it["aa_1"].effective_status == "addressed", "a duplicate shows its primary's status"
    assert [t.tid for t in status.top_level(items)] == ["bb_2", "cc_3"]
    assert it["aa_1"].group_reason == "same MDS check"


def test_only_primaries_are_checked():
    doc = grouped_doc([{"ids": ["aa_1", "bb_2"]}])
    assert set(status.pending_and_stale_tids(doc, overrides_doc(), "opus")) == {"bb_2", "cc_3"}


def test_not_a_duplicate_takes_an_item_out():
    items, _ = build(
        grouped_doc([{"ids": ["aa_1", "bb_2"]}]), overrides_doc({"aa_1": {"not_duplicate": True}})
    )
    assert all(not it.duplicate_of for it in items)


def test_duplicate_of_by_hand_merges():
    items, _ = build(grouped_doc([]), overrides_doc({"cc_3": {"duplicate_of": "aa_1"}}))
    it = by(items)
    assert it["aa_1"].duplicate_of == "cc_3", "the later patchset is the primary"
    assert it["aa_1"].group_reason == "" and it["cc_3"].duplicates[0].tid == "aa_1"


def test_a_duplicate_with_its_own_decision_keeps_it():
    items, _ = build(
        grouped_doc([{"ids": ["aa_1", "bb_2"]}]), overrides_doc({"aa_1": {"status": "ignored"}})
    )
    assert by(items)["aa_1"].effective_status == "ignored"


def test_grouping_is_redone_when_the_promises_change():
    doc = grouped_doc([])
    items, _ = build(doc)
    assert status.groups_stale(doc, items), "never grouped"
    doc["groups"]["input_hash"] = status.group_input_hash(items)
    assert not status.groups_stale(doc, items)
    doc["classifications"]["cc_3"]["summary"] = "something else"
    assert status.groups_stale(doc, build(doc)[0])


def test_the_gerrit_link_goes_to_the_comment_itself():
    t = th(tid="0931272e_0a1b2c3d", file_path=None)
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)})
    it = build(doc)[0][0]
    assert (
        it.gerrit_url
        == "https://review.example.com/c/fs/lustre-release/+/64620/comment/0931272e_0a1b2c3d/"
    )
    m = dict(th(tid="msg-abc"), is_message=True)
    doc = change_doc(threads=[m], classifications={m["id"]: classification(m)})
    assert build(doc)[0][0].gerrit_url == "https://review.example.com/c/fs/lustre-release/+/64620"


# ---------- which tickets and changes are possible follow-ups ----------


def test_a_ticket_in_quoted_code_is_not_a_promise_target():
    """62757: the comment quoted `always_except LU-12668 41d 53a` and every
    patch of the EC feature (LU-12668) was listed as a follow-up."""
    t = th(
        root_msg="These two subtests are disabled: `always_except LU-12668 41d 53a`",
        replies=["Acknowledged"],
    )
    assert status.promise_tickets(t, "LU") == []
    assert status.thread_tickets(t, "LU") == ["LU-12668"]


def test_tickets_count_when_the_promise_names_them():
    t = th(
        replies=[
            'I filed LU-20566 "FLR-EC: recover data from parity code for DIO+AIO" for this.',
            "Done. This needs to be done in other places too, but this is deferred to LU-20565",
            "LU-19999 is unrelated noise here.",
        ]
    )
    assert status.promise_tickets(t, "LU") == ["LU-20566", "LU-20565"]


def test_changes_older_than_the_thread_are_not_follow_ups():
    t = th(replies=["I will create a patch in LU-19999."])  # thread from 2026-08-28
    found = {
        "by_ticket": {
            "LU-19999": [
                {"number": 1, "status": "MERGED", "created": "2026-05-01 10:00:00.000"},
                {"number": 2, "status": "NEW", "created": "2026-09-02 10:00:00.000"},
            ]
        }
    }
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)}, candidates=found)
    assert [c.number for c in build(doc)[0][0].candidates] == [2]


def test_what_a_check_says_about_the_follow_ups():
    t = th(replies=["I will create a patch in LU-19999."])
    found = {
        "by_ticket": {
            "LU-19999": [{"number": 70001, "status": "MERGED", "created": "2026-09-02 10:00:00"}]
        }
    }
    base = change_doc(threads=[t], classifications={t["id"]: classification(t)}, candidates=found)
    it = build(base)[0][0]
    assert it.candidates and not it.followups_ruled_out and it.kept_in_change is None
    base["adjudications"] = {t["id"]: adjudication(t, verdict="still-open")}
    assert build(base)[0][0].followups_ruled_out
    base["adjudications"] = {
        t["id"]: adjudication(
            t, verdict="addressed", addressed_in={"kind": "change", "ref": "70001"}
        )
    }
    it = build(base)[0][0]
    assert it.kept_in_change == 70001 and not it.followups_ruled_out


# ---------- "in a follow-up" found by the check ----------


def followup_verdict(state):
    t = th()
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={
            t["id"]: adjudication(
                t,
                verdict="in-followup",
                addressed_in={
                    "kind": "change",
                    "ref": "67878",
                    "detail": "re-enables the AIO tests",
                },
            )
        },
        candidates={
            "stacked": [
                {"number": 67878, "status": state, "subject": "LU-20566 ec: recover O_DIRECT reads"}
            ]
        },
    )
    return build(doc)[0][0]


def test_a_follow_up_in_review_named_by_the_check():
    it = followup_verdict("NEW")
    assert (it.effective_status, it.bucket, it.status_label) == (
        "in_followup",
        "followup",
        "In flight",
    )
    assert it.followup_in_review.number == 67878 and "O_DIRECT" in it.followup_in_review.subject


def test_it_is_kept_once_the_follow_up_merges_and_open_if_dropped():
    assert followup_verdict("MERGED").effective_status == "addressed"
    assert followup_verdict("ABANDONED").effective_status == "open"
