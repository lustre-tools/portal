"""Offline builders for Gerrit Promises documents: no Gerrit, no Claude."""

from __future__ import annotations

import hashlib

from portal.promises.models import CLASSIFY_PROMPT_VERSION, JUDGE_PROMPT_VERSION, thread_text

REV = "aaaa000000000000000000000000000000000001"
PUBLIC = "fs/lustre-release"


def th(
    tid="a08dac95_b61396c6",
    file_path="lustre/mdd/mdd_object.c",
    line=2042,
    ps=19,
    resolved=True,
    root_msg="Keeping the flag means the victim is never checked.",
    replies=None,
    root_author="Gerrit AI review for Lustre",
    reply_author="Marc Vef",
    mechanical=False,
    keyword_hit=True,
):
    replies = replies if replies is not None else ["Pre-existing, will fix in a follow-up patch."]
    text = thread_text(root_msg, replies)
    return {
        "id": tid,
        "file_path": file_path,
        "line": line,
        "patch_set": ps,
        "is_resolved": resolved,
        "root": {
            "author": root_author,
            "updated": "2026-08-28 13:56:46.000000000",
            "message": root_msg,
        },
        "replies": [
            {"author": reply_author, "updated": "2026-08-29 10:00:00.000000000", "message": m}
            for m in replies
        ],
        "code_context": "    2042: entry_vic->lcme_flags &= ...",
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "mechanical": mechanical,
        "keyword_hit": keyword_hit,
    }


def classification(
    thread,
    kind="deferral",
    summary="Enforce the rule on the MDS",
    severity="med",
    quote="will fix in a follow-up patch",
    model="sonnet",
):
    return {
        "kind": kind,
        "summary": summary,
        "severity": severity,
        "promise_quote": quote,
        "text_hash": thread["text_hash"],
        "prompt_version": CLASSIFY_PROMPT_VERSION,
        "model": model,
        "classified_at": "2026-08-30T10:00:00Z",
    }


def adjudication(
    thread,
    verdict="still-open",
    revision=REV,
    ps=20,
    model="opus",
    prompt_version=JUDGE_PROMPT_VERSION,
    **extra,
):
    entry = {
        "verdict": verdict,
        "addressed_in": None,
        "evidence": "still present at the pinned revision",
        "suggested_action": "add the refusal gated on mrd_obj",
        "judged_at": "2026-08-30T11:00:00Z",
        "judged_revision": revision,
        "judged_patchset": ps,
        "judged_master": "bbbb000000000000000000000000000000000002",
        "text_hash": thread["text_hash"],
        "prompt_version": prompt_version,
        "model": model,
        "raw_error": None,
    }
    entry.update(extra)
    return entry


def change_doc(
    number=64620,
    threads=None,
    classifications=None,
    adjudications=None,
    linked=None,
    revision=REV,
    ps=20,
    status="NEW",
    project=PUBLIC,
    branch="master",
    candidates=None,
    subject="LU-19548 lfs: update mirror split for EC support",
):
    threads = threads if threads is not None else [th()]
    return {
        "schema_version": 1,
        "change_number": number,
        "added_at": "2026-08-30T09:00:00Z",
        "added_by": "alice",
        "harvest": {
            "harvested_at": "2026-08-30T09:30:00Z",
            "gerrit_url": f"https://review.example.com/c/{project}/+/{number}",
            "change": {
                "subject": subject,
                "project": project,
                "branch": branch,
                "status": status,
                "owner": "Marc Vef",
                "current_revision": revision,
                "current_patchset": ps,
            },
            "threads": threads,
        },
        "linked_changes": linked or {},
        "candidates": candidates or {},
        "classifications": classifications or {},
        "adjudications": adjudications or {},
        "spend": {},
    }


def overrides_doc(overrides=None, manual_items=None, next_id=1):
    return {
        "schema_version": 1,
        "overrides": overrides or {},
        "manual_items": manual_items or {},
        "next_manual_id": next_id,
    }
