"""The landing rules Patch Status applies: Lustre's, on master.

V: jenkins and Maloo both +1, no -1. R: two +1s from people other than the
owner, no -1 or -2; wc-checkpatch's Code-Review vote is not a review but
the "needs rebase" signal (the C column). A change is in line for landing
when it is on the landing branch (<branch>-next): the gatekeeper votes +2
only in the moment of landing, so a +2 never shows on an open change.
"""

CHECKPATCH = "wc-checkpatch"
JENKINS = "jenkins"
MALOO = "Maloo"

# Code-Review voters that are not reviewers.
NOT_REVIEWERS = frozenset({CHECKPATCH})
REVIEW_THRESHOLD = 2

LANDING_SUFFIX = "-next"
# Release blockers and priorities are tracked with per-release hashtags
# (2.18_review_blocker, ...), so they are matched by suffix.
BLOCKER_SUFFIX = "_review_blocker"
WANTED_HASHTAGS = ("mw",)
WANTED_SUFFIXES = ("_review_blocker", "_review_priority")
TEST_ONLY_HASHTAGS = ("test-only", "fortestonly")


def landing_branch(branch: str) -> str:
    return branch + LANDING_SUFFIX
