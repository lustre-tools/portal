"""The Comment column: what a change still needs, in a few words.

The bucket heading already says which pile a change is in. This adds the
specific missing thing -- who voted it down, the one change in the series
that must be rebased first -- so a row can be acted on without opening
Gerrit.

One primary clause, at most one secondary, joined by " · ". The order
follows assign_bucket, so a comment never contradicts the heading it is
under, with two deliberate exceptions: a Code-Review -2 outranks a needed
rebase (rebasing a vetoed change is wasted work), and a person's
Verified -1 outranks both CI bots.
"""

from __future__ import annotations

from typing import Optional

from portal.patch_status.classify import (
    DEPENDEE_NOT_READY,
    IN_LINE_FOR_LANDING,
    READY_TO_LAND,
    is_test_only,
)
from portal.patch_status.rules import landing_branch

SEP = " · "

# Short names for the bucket of a blocking change below.
_BUCKET_SHORT = {
    "Needs review": "needs review",
    "Needs verify": "needs verify",
    "Fails verify or review": "failing",
    "Dependee not ready": "blocked chain",
    "Needs rebase": "needs rebase",
    "For test only": "test only",
}


def _worst_negative(negatives: list) -> Optional[tuple]:
    """-2 before -1; at the same value, someone else before the owner."""
    if not negatives:
        return None
    return sorted(negatives, key=lambda t: (t[1], t[2]))[0]


def _vote_phrase(entry: tuple, extra: int = 0) -> str:
    """'-1 from Name', '-2 from Name (uploader)', with '+N' for the rest."""
    name, value, is_owner = entry
    out = f"{value:+d} from {name}" if name else f"{value:+d} review"
    if is_owner:
        # The Gerrit owner is the uploader, often not the git author.
        out += " (uploader)"
    if extra > 0:
        out += f" +{extra}"
    return out


def _ci_pending(v: dict) -> Optional[str]:
    """What CI still owes when Verified is short without a -1."""
    if v.get("pass"):
        return None
    jenkins, maloo = v.get("jenkins", 0), v.get("maloo", 0)
    if jenkins == 0 and maloo == 0:
        return "CI has not run"
    if jenkins == 0:
        return "waiting on Jenkins"
    if maloo == 0:
        return "waiting on Maloo"
    return "Verified incomplete"


def _nearest_blocker(change: dict) -> Optional[tuple]:
    blockers = change.get("_blocking_ancestors") or []
    return blockers[0] if blockers else None


def _primary(change: dict) -> str:
    votes = change.get("votes") or {}
    v = votes.get("_verified") or {}
    r = votes.get("_review") or {}
    bucket = change.get("bucket")

    if is_test_only(change):
        return "test only — not for landing"

    negatives = r.get("negatives") or []
    worst = _worst_negative(negatives)
    if worst and worst[1] <= -2:
        return _vote_phrase(worst, extra=len(negatives) - 1)

    # An abandoned change below is for good; neither CI nor review fixes it.
    blocker = _nearest_blocker(change)
    if blocker and blocker[1] == "abandoned":
        return f"parent #{blocker[0]} abandoned — reparent"

    if change.get("_effective_mergeable") is False:
        bottom = change.get("_merge_blocked_by")
        if bottom:
            return f"rebase #{bottom} (chain bottom) first"
        return "conflicts with branch tip"

    human_negs = v.get("human_negatives") or []
    if human_negs:
        name, value, is_owner = human_negs[0]
        return f"Verified {value:+d} from {name or 'a reviewer'}" + (" (uploader)" if is_owner else "")

    jenkins, maloo = v.get("jenkins", 0), v.get("maloo", 0)
    if jenkins == -1 and maloo == -1:
        return "Jenkins -1, Maloo -1"
    if jenkins == -1:
        return "Jenkins -1"
    if maloo == -1:
        flavor = change.get("_maloo_flavor")
        if flavor == "fortestonly":
            return "Maloo -1 (fortestonly — by design)"
        if flavor == "untestable":
            return "Maloo -1 (untestable — re-test)"
        return "Maloo -1"

    if worst:
        return _vote_phrase(worst, extra=len(negatives) - 1)

    pending = _ci_pending(v)
    if pending:
        return pending

    if not r.get("pass"):
        have = r.get("effective_pluses", 0)
        need = r.get("threshold", 2)
        if have == 0:
            if r.get("owner_self", 0) > 0:
                return f"only a self +1 (0/{need})"
            return f"no review yet (0/{need})"
        return f"needs {need - have} more review ({have}/{need})"

    if change.get("_deps_forced"):
        return "chain order unresolved"
    if change.get("dependees_ready") is False or bucket == DEPENDEE_NOT_READY:
        if blocker:
            num, kind = blocker
            rest = len(change.get("_blocking_ancestors") or []) - 1
            tail = f" (+{rest} more)" if rest > 0 else ""
            if kind == "not_in_set":
                return f"parent #{num} not on this branch{tail}"
            return f"blocked by #{num} — {_BUCKET_SHORT.get(kind, kind.lower())}{tail}"
        return "waiting on a parent change"

    if bucket == IN_LINE_FOR_LANDING or r.get("queued"):
        return "in " + landing_branch(change.get("branch") or "master")
    if bucket == READY_TO_LAND:
        return "ready"
    return f"in: {bucket}" if bucket else ""


def _secondary(change: dict, primary: str) -> Optional[str]:
    """One more clause, only when it outlasts fixing the primary and no
    other column shows it."""
    r = (change.get("votes") or {}).get("_review") or {}

    # A Code-Review veto the primary did not name already.
    if "from " not in primary:
        worst = _worst_negative(r.get("negatives") or [])
        if worst:
            return _vote_phrase(worst)

    # A review shortfall hides that the series blocks it too.
    if primary.startswith(("no review", "only a self +1", "needs ")):
        blocker = _nearest_blocker(change)
        if blocker:
            return f"blocked by #{blocker[0]}"

    unresolved = change.get("unresolved_comment_count") or 0
    if unresolved and not primary.startswith(("test only", "rebase", "conflicts with")):
        return f"{unresolved} unresolved"
    return None


def comment_for(change: dict) -> str:
    """Plain text; the page turns '#<number>' into change links."""
    primary = _primary(change)
    if not primary:
        return ""
    secondary = _secondary(change, primary)
    if secondary and len(primary) + len(SEP) + len(secondary) <= 78:
        return primary + SEP + secondary
    return primary
