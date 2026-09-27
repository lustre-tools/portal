"""What the harvest works out on its own: checkers, keywords, messages."""

from portal.promises import harvest


def raw_thread(authors, messages, resolved=True):
    root = {
        "id": "t1",
        "author": {"name": authors[0]},
        "message": messages[0],
        "file_path": "lustre/a.c",
        "line": 3,
        "patch_set": 2,
    }
    replies = [
        {"author": {"name": a}, "message": m}
        for a, m in zip(authors[1:], messages[1:], strict=True)
    ]
    return {"root_comment": root, "replies": replies, "is_resolved": resolved}


MECH = {m.lower() for m in harvest.DEFAULT_MECHANICAL}


def test_checkpatch_alone_is_mechanical():
    t = harvest._thread_view(raw_thread(["wc-checkpatch"], ["WARNING: line over 80"]), MECH)
    assert t["mechanical"]


def test_a_human_reply_to_a_checker_counts():
    t = harvest._thread_view(
        raw_thread(["wc-checkpatch", "Marc Vef"], ["WARNING", "will fix in a follow-up"]), MECH
    )
    assert not t["mechanical"] and t["keyword_hit"]


def test_the_ai_reviewer_and_the_gatekeeper_are_reviewers():
    for name in ("Gerrit AI review for Lustre", "Misc Code Checks Robot (Gatekeeper helper)"):
        assert not harvest._thread_view(raw_thread([name], ["this leaks"]), MECH)["mechanical"]


def test_promise_wording_is_spotted():
    for text in (
        "I will create a patch in LU-19999",
        "Let me port the new lfsck check from master to b2_15",
        "pre-existing, fix in a follow-up",
        "That could be done in a follow-on patch",
        "worth a separate ticket",
    ):
        assert harvest._has_promise_words([text]), text
    assert not harvest._has_promise_words(["Done", "Thanks, looks good"])


def test_review_messages_become_change_level_threads():
    msgs = [
        {
            "id": "m1",
            "author": {"name": "Andreas Dilger"},
            "date": "2026-08-01 10:00:00",
            "message": "Patch Set 7: Code-Review+1\n\n(2 comments)\n\nThe cleanups can follow after landing.",
            "patch_set": 7,
        },
        {
            "id": "m2",
            "author": {"name": "Marc Vef"},
            "date": "x",
            "message": "Patch Set 16: Code-Review+2",
            "patch_set": 16,
        },
        {
            "id": "m3",
            "author": {"name": "Maloo"},
            "date": "x",
            "message": "Patch Set 3: Verified-1\n\nTests failed",
            "patch_set": 3,
        },
        {
            "id": "m4",
            "author": {"name": "Marc Vef"},
            "date": "x",
            "message": "Hashtag added: mw",
            "patch_set": 16,
        },
        {
            "id": "m5",
            "author": {"name": "Marc Vef"},
            "date": "x",
            "patch_set": 9,
            "message": "Patch Set 9:\n\n[Marc Bot] Note: This change has been cherry-picked to master-next.",
        },
        {
            "id": "m6",
            "author": {"name": "Lustre RISC-V Builder"},
            "date": "x",
            "patch_set": 9,
            "message": "Patch Set 9:\n\nRISC-V Client Builder: build succesful",
        },
        {
            "id": "m7",
            "author": {"name": "Gerrit AI review for Lustre"},
            "date": "x",
            "patch_set": 9,
            "message": "Patch Set 9:\n\nAI code review results below. Some concerns, see inline.",
        },
    ]
    out = harvest._message_threads(msgs, MECH)
    # Votes, builders, tagged bot notes and review summaries that only point
    # at their inline comments go; a person's own review message stays.
    assert [t["id"] for t in out] == ["msg-m1"]
    assert out[0]["root"]["message"] == "The cleanups can follow after landing."
    assert out[0]["is_message"] and out[0]["keyword_hit"] and out[0]["file_path"] is None


def test_change_links_in_a_thread_are_picked_up():
    text = [
        "Addressed in https://review.whamcloud.com/c/fs/lustre-release/+/68697",
        "see also https://review.whamcloud.com/68700 and https://review.whamcloud.com/#/c/68701/",
        "this change https://review.whamcloud.com/64620 itself",
        "LU-123 and 2026 are not links",
    ]
    assert harvest.change_links(text, "https://review.whamcloud.com", own=64620) == [
        68697,
        68700,
        68701,
    ]


def test_review_summaries_are_dropped_but_people_keep_their_messages():
    msgs = [
        {
            "id": "a",
            "author": {"name": "Gerrit AI review for Lustre"},
            "date": "x",
            "patch_set": 99,
            "message": "Patch Set 99:\n\nAI code review results below, please doublecheck\n\nSome concerns, see inline comments.",
        },
        {
            "id": "b",
            "author": {"name": "Patrick Farrell"},
            "date": "x",
            "patch_set": 40,
            "message": "Patch Set 40:\n\nSashiko Automated Review\n===\nFound 6 issue(s)",
        },
        {
            "id": "c",
            "author": {"name": "Marc Vef"},
            "date": "x",
            "patch_set": 78,
            "message": "Patch Set 78:\n\n**[Marc Bot - AI review - opus]**  Mostly good - only minor comments inline",
        },
        {
            "id": "d",
            "author": {"name": "Andreas Dilger"},
            "date": "x",
            "patch_set": 95,
            "message": "Patch Set 95: Code-Review+1\n\nThe test cleanups can be done in a separate patch.",
        },
    ]
    assert [t["id"] for t in harvest._message_threads(msgs, MECH)] == ["msg-d"]


def test_follow_ups_named_by_number_are_links_too():
    text = [
        "Already addressed in follow-up: 68339 (direct child) flips the wording",
        "Addressed in followup: 67878 (LU-20566, higher in the stack)",
        "this is LU-20566 and 2026-09-01, and line 12345 of foo.c",
    ]
    assert harvest.change_links(text, "https://review.whamcloud.com", own=62757) == [68339, 67878]
