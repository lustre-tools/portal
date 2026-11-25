"""Sort open changes into the Patch Status buckets.

Per change the fetcher supplies: _number, subject, owner (with
_account_id), labels (Verified, Code-Review, every vote), hashtags,
topic, mergeable (False when wc-checkpatch says it needs a rebase),
messages (wc-checkpatch's and Maloo's on the current patch set), the
current revision number, _related (its series) and _staged (on the
landing branch, or None when that branch could not be read).

classify() adds: votes (V/R/C/S/D as ints, with the gate states behind
them), is_mw, is_test_only, series_position, dependees_ready and bucket.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from portal.patch_status import rules

IN_LINE_FOR_LANDING = "In line for landing"
READY_TO_LAND = "Ready to land"
NEEDS_REVIEW = "Needs review"
NEEDS_VERIFY = "Needs verify"
DEPENDEE_NOT_READY = "Dependee not ready"
FAILS = "Fails verify or review"
NEEDS_REBASE = "Needs rebase"
TEST_ONLY = "For test only"

BUCKET_ORDER = [
    IN_LINE_FOR_LANDING,
    READY_TO_LAND,
    NEEDS_REVIEW,
    NEEDS_VERIFY,
    FAILS,
    DEPENDEE_NOT_READY,
    NEEDS_REBASE,
    TEST_ONLY,
]

# Buckets whose members count as ready when they sit below another
# change in a series (see _strict_dependees_ready).
READY_BUCKETS = (IN_LINE_FOR_LANDING, READY_TO_LAND)

VERIFY_LABEL = "Verified"
REVIEW_LABEL = "Code-Review"


def _label_entries(label_body: dict) -> list[dict]:
    return list((label_body or {}).get("all") or [])


def verified_state(change: dict) -> dict:
    """Verified passes when jenkins AND Maloo are +1 and nobody is at -1.

    human_negatives lists the -1s of anyone but the two bots: a person
    marking a patch broken is a different signal from a CI verdict.
    """
    owner_id = (change.get("owner") or {}).get("_account_id")
    body = (change.get("labels") or {}).get(VERIFY_LABEL) or {}
    jenkins = maloo = 0
    has_neg = False
    human_negatives: list[tuple[str, int, bool]] = []
    for e in _label_entries(body):
        name = (e.get("name") or "").strip()
        val = e.get("value")
        if not isinstance(val, int) or val == 0:
            continue
        if val < 0:
            has_neg = True
        if name == rules.JENKINS:
            jenkins = val
        elif name == rules.MALOO:
            maloo = val
        elif val < 0:
            human_negatives.append((name, val, e.get("_account_id") == owner_id))
    return {
        "jenkins": jenkins,
        "maloo": maloo,
        "has_any_negative": has_neg,
        "human_negatives": human_negatives,
        "pass": jenkins == 1 and maloo == 1 and not has_neg,
    }


def _verified_column_score(state: dict) -> int:
    if state["pass"]:
        return 1
    if state["has_any_negative"]:
        return -1
    return 0


def owner_self_review_score(change: dict) -> int:
    """The owner's own Code-Review vote on their change, or 0."""
    owner_id = (change.get("owner") or {}).get("_account_id")
    body = (change.get("labels") or {}).get(REVIEW_LABEL) or {}
    score = 0
    for e in body.get("all") or []:
        if e.get("_account_id") != owner_id:
            continue
        v = e.get("value")
        if isinstance(v, int) and v != 0:
            score = v
    return score


def review_state(change: dict) -> dict:
    """Code-Review passes with two distinct non-owner +1s (a +2 counts as
    one of them) and no -1 or -2. The owner's own +1 never counts; the
    owner's own -1/-2 is a veto like anyone's."""
    owner_id = (change.get("owner") or {}).get("_account_id")
    body = (change.get("labels") or {}).get(REVIEW_LABEL) or {}
    plus_one_voters: set = set()
    plus_two_voters: set = set()
    has_neg = False
    # (name, value, is_owner) per negative voter, so the Comment column
    # can name who voted it down.
    negatives: list[tuple[str, int, bool]] = []
    for e in _label_entries(body):
        if e.get("_account_id") == owner_id:
            continue
        val = e.get("value")
        acc = e.get("_account_id")
        if not isinstance(val, int) or acc is None:
            continue
        if e.get("name") in rules.NOT_REVIEWERS:
            continue
        if val == 1:
            plus_one_voters.add(acc)
        elif val == 2:
            plus_two_voters.add(acc)
        elif val < 0:
            has_neg = True
            negatives.append(((e.get("name") or "").strip(), val, False))

    owner_self = owner_self_review_score(change)
    if owner_self < 0:
        has_neg = True
        owner_name = (change.get("owner") or {}).get("name") or ""
        negatives.append((owner_name.strip(), owner_self, True))

    effective_pluses = len(plus_one_voters | plus_two_voters)
    return {
        "plus_one_count": len(plus_one_voters),
        "plus_two_count": len(plus_two_voters),
        "has_plus_two": bool(plus_two_voters) or owner_self >= 2,
        "queued": is_staged(change),
        "has_any_negative": has_neg,
        "negatives": negatives,
        "owner_self": owner_self,
        "effective_pluses": effective_pluses,
        "pass": effective_pluses >= rules.REVIEW_THRESHOLD and not has_neg,
        "threshold": rules.REVIEW_THRESHOLD,
    }


def _review_column_score(state: dict) -> int:
    """+2 when the gate passes and someone voted +2, +1 when it passes on
    +1s, -1 for any negative, 0 otherwise."""
    if state["pass"]:
        return 2 if state["has_plus_two"] else 1
    if state["has_any_negative"]:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Style (S), from wc-checkpatch's messages
# ---------------------------------------------------------------------------

_STYLE_COUNT_RE = re.compile(r"(\d+)\s+style\s+warning", re.IGNORECASE)


def _latest_message_text(
    messages: Iterable[dict],
    author_name: str,
    current_revision_number: Optional[int],
) -> Optional[str]:
    latest_text: Optional[str] = None
    latest_date = ""
    for m in messages or []:
        if ((m.get("author") or {}).get("name") or "") != author_name:
            continue
        if current_revision_number is not None:
            rev = m.get("_revision_number")
            if rev is not None and rev != current_revision_number:
                continue
        date = m.get("date") or ""
        if date >= latest_date:
            latest_date = date
            latest_text = m.get("message") or ""
    return latest_text


def maloo_flavor(change: dict) -> Optional[str]:
    """Why Maloo voted -1: 'fortestonly' (by design, the patch says so),
    'untestable' (the sessions could not run; needs a re-test), or None
    (a real test failure, or no Maloo message on this patch set)."""
    cur = change.get("current_revision_number")
    blob = " ".join(
        (m.get("message") or "")
        for m in (change.get("messages") or [])
        if (m.get("author") or {}).get("name") == rules.MALOO
        and (cur is None or m.get("_revision_number") == cur)
    ).lower()
    if "fortestonly" in blob:
        return "fortestonly"
    if "untestable" in blob:
        return "untestable"
    return None


def _parse_checkpatch(text: str) -> int:
    """'Looks good to me.' -> +1; 'N style warning(s)' -> -1 when N > 0."""
    m = _STYLE_COUNT_RE.search(text)
    if m:
        return 1 if int(m.group(1)) == 0 else -1
    if "looks good to me" in text.lower():
        return 1
    return 0


# ---------------------------------------------------------------------------
# Test-only, landing branch, release blockers and priorities
# ---------------------------------------------------------------------------

_TEST_ONLY_SUBJECT_RE = re.compile(r"\bLU-?0+\b|^TEST-ONLY[:]", re.IGNORECASE)


def is_test_only(change: dict) -> bool:
    if _TEST_ONLY_SUBJECT_RE.search(change.get("subject") or ""):
        return True
    if (change.get("topic") or "").startswith("test-only/"):
        return True
    return any(h in rules.TEST_ONLY_HASHTAGS for h in change.get("hashtags") or [])


def is_staged(change: dict) -> bool:
    """On the landing branch; by its hashtag when the branch was not read."""
    staged = change.get("_staged")
    if staged is not None:
        return staged
    landing = rules.landing_branch(change.get("branch") or "master")
    return landing in (change.get("hashtags") or [])


def is_blocker(change: dict) -> bool:
    return any(h.endswith(rules.BLOCKER_SUFFIX) for h in change.get("hashtags") or [])


def is_most_wanted(change: dict) -> bool:
    return any(
        h in rules.WANTED_HASHTAGS or h.endswith(rules.WANTED_SUFFIXES)
        for h in change.get("hashtags") or []
    )


def wanted_rank(change: dict) -> int:
    """0 release blocker, 1 otherwise wanted, 2 the rest."""
    if is_blocker(change):
        return 0
    return 1 if is_most_wanted(change) else 2


# ---------------------------------------------------------------------------
# Votes and buckets
# ---------------------------------------------------------------------------

def compute_votes(change: dict) -> dict:
    """V/R/C/S/D as ints, with the gate states behind V and R."""
    vstate = verified_state(change)
    rstate = review_state(change)

    # C: needs no rebase. classify() sets _effective_mergeable first (a
    # series takes its first open change's verdict).
    mergeable = change.get("_effective_mergeable", change.get("mergeable"))
    c = 1 if mergeable is True else -1 if mergeable is False else 0

    s_text = _latest_message_text(
        change.get("messages") or [], rules.CHECKPATCH, change.get("current_revision_number"))
    s = _parse_checkpatch(s_text) if s_text is not None else 0

    deps = change.get("dependees_ready")
    d = 1 if deps is True else -1 if deps is False else 0

    return {
        "v": _verified_column_score(vstate),
        "r": _review_column_score(rstate),
        "c": c,
        "s": s,
        "d": d,
        "_verified": vstate,
        "_review": rstate,
    }


def assign_bucket(change: dict, votes: dict) -> str:
    """First match wins; a change's own state outranks its series':

      1. For test only        subject, topic or hashtag says so
      2. Needs rebase         wc-checkpatch's -1 (the series' first open
                              change's, for the ones above it)
      3. Fails                any Verified or Code-Review -1/-2 (style
                              warnings are advisory)
      4. Needs verify         jenkins or Maloo still owes its +1
      5. Needs review         CI is green, Code-Review is short
      6. Dependee not ready   ready itself, a change below it is not
      7. In line for landing  ready and on the landing branch
      8. Ready to land        ready, not picked up yet
    """
    if is_test_only(change):
        return TEST_ONLY
    if change.get("_effective_mergeable", change.get("mergeable")) is False:
        return NEEDS_REBASE
    vstate, rstate = votes["_verified"], votes["_review"]
    if vstate["has_any_negative"] or rstate["has_any_negative"]:
        return FAILS
    if not vstate["pass"]:
        return NEEDS_VERIFY
    if not rstate["pass"]:
        return NEEDS_REVIEW
    if change.get("dependees_ready") is False:
        return DEPENDEE_NOT_READY
    if rstate.get("queued"):
        return IN_LINE_FOR_LANDING
    return READY_TO_LAND


def _series_position(change: dict) -> Optional[tuple[int, int]]:
    """(position from the bottom, length) in its series, or None.

    Gerrit lists a series newest first; position 1 lands first.
    """
    related = change.get("_related") or []
    if len(related) <= 1:
        return None
    n = change.get("_number")
    self_idx = next((i for i, r in enumerate(related) if r.get("_change_number") == n), None)
    if self_idx is None:
        return None
    return (len(related) - self_idx, len(related))


def _effective_mergeable(change: dict, by_number: dict[int, dict]) -> Optional[bool]:
    """The needs-rebase verdict that applies to a change.

    wc-checkpatch only trial-picks a standalone change or the first open
    change of a series, so a change higher up inherits that one's
    verdict: if the bottom cannot be applied, nothing above it can land.
    A change's own -1 always counts.
    """
    related = change.get("_related") or []
    n = change.get("_number")
    if len(related) <= 1:
        return change.get("mergeable")
    if not any(r.get("_change_number") == n for r in related):
        return change.get("mergeable")

    # Newest first, so the bottom is the last open entry; merged ones are
    # on the branch already.
    bottom_num: Optional[int] = None
    for entry in reversed(related):
        if entry.get("status") == "NEW":
            bottom_num = entry.get("_change_number")
            break
    if bottom_num is None or bottom_num == n:
        return change.get("mergeable")
    bottom_ch = by_number.get(bottom_num)
    if bottom_ch is None:
        return change.get("mergeable")
    if change.get("mergeable") is False:
        return False

    inherited = bottom_ch.get("mergeable")
    if inherited is False:
        # So the Comment column can point at the one change to rebase
        # instead of telling every change in the series to.
        change["_merge_blocked_by"] = bottom_num
    return inherited


def _strict_dependees_ready(change: dict, by_number: dict[int, dict]) -> Optional[bool]:
    """True when every change below this one in its series is merged or
    itself ready; False when one is not; None while one below is not
    classified yet (try again in the next round)."""
    related = change.get("_related") or []
    if not related:
        return True
    n = change.get("_number")
    self_idx = next((i for i, r in enumerate(related) if r.get("_change_number") == n), None)
    if self_idx is None:
        return True

    # Nearest first: the Comment column names the nearest one.
    blockers: list[tuple[int, str]] = []
    deferred = False
    for a in related[self_idx + 1:]:
        status = a.get("status")
        if status == "MERGED":
            continue
        anc_num = a.get("_change_number")
        if status == "ABANDONED":
            blockers.append((anc_num, "abandoned"))
            continue
        if status != "NEW":
            blockers.append((anc_num, (status or "unknown").lower()))
            continue
        anc_ch = by_number.get(anc_num)
        if anc_ch is None:
            blockers.append((anc_num, "not_in_set"))
            continue
        anc_bucket = anc_ch.get("bucket")
        if anc_bucket is None:
            deferred = True
            continue
        if anc_bucket not in READY_BUCKETS:
            blockers.append((anc_num, anc_bucket))

    change["_blocking_ancestors"] = blockers
    if blockers:
        return False
    if deferred:
        return None
    return True


def classify(changes: list[dict]) -> list[dict]:
    """Annotate every change in place, and return the list.

    A change can only be bucketed once the changes below it in its series
    are, so buckets are assigned in rounds until nothing changes; what is
    left (a cycle, or a series reaching outside the set) counts as not
    ready.
    """
    by_number = {c.get("_number"): c for c in changes}

    for ch in changes:
        ch["_effective_mergeable"] = _effective_mergeable(ch, by_number)

    for ch in changes:
        ch["votes"] = compute_votes(ch)
        ch["is_mw"] = is_most_wanted(ch)
        ch["is_test_only"] = is_test_only(ch)
        ch["series_position"] = _series_position(ch)
        ch["_maloo_flavor"] = (
            maloo_flavor(ch) if ch["votes"]["_verified"]["maloo"] == -1 else None
        )
        # Every member of a series shares this, so the page keeps them
        # together.
        related = ch.get("_related") or []
        ch["_series_id"] = (
            tuple(sorted(r.get("_change_number") for r in related if r.get("_change_number")))
            if len(related) > 1 else None
        )
        ch.setdefault("_merge_blocked_by", None)
        ch.setdefault("_blocking_ancestors", [])
        ch.setdefault("_deps_forced", False)
        ch["bucket"] = None

    pending = list(changes)
    rounds = len(changes) + 1
    while pending and rounds > 0:
        rounds -= 1
        next_pending: list[dict] = []
        progress = False
        for ch in pending:
            verdict = _strict_dependees_ready(ch, by_number)
            if verdict is None:
                next_pending.append(ch)
                continue
            ch["dependees_ready"] = verdict
            ch["votes"]["d"] = 1 if verdict else -1
            ch["bucket"] = assign_bucket(ch, ch["votes"])
            progress = True
        if not progress:
            for ch in next_pending:
                ch["dependees_ready"] = False
                ch["_deps_forced"] = True
                ch["votes"]["d"] = -1
                ch["bucket"] = assign_bucket(ch, ch["votes"])
            break
        pending = next_pending

    return changes
