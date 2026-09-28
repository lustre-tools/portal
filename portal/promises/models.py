"""View models and domain constants. Pure -- no I/O.

The store keeps raw JSON documents; :func:`portal.promises.status.build_items`
turns them into the :class:`Item` views everything downstream (pages,
export, CLI) uses, so knowledge of the schema stays in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Bumping either one invalidates every stored result of that stage -- the
# deliberate mass-invalidation lever. Classification results do not
# depend on the model (a model upgrade alone does not re-bill every
# change); judgments do, and show as outdated when it changes.
CLASSIFY_PROMPT_VERSION = "c2"
JUDGE_PROMPT_VERSION = "j3"
# Grouping duplicates; bumping it regroups every change on the next run.
GROUP_PROMPT_VERSION = "g1"

# Classifier kinds that make a thread an item.
ITEM_KINDS = ("deferral", "open-item")

SEVERITIES = ("high", "med", "low")

BUCKET_OPEN = "open"
BUCKET_FOLLOWUP = "followup"
BUCKET_ATTENTION = "attention"
BUCKET_ADDRESSED = "addressed"
BUCKET_IGNORED = "ignored"

STATUS_BUCKET = {
    "open": BUCKET_OPEN,
    "in_followup": BUCKET_FOLLOWUP,
    "pending": BUCKET_ATTENTION,
    "stale": BUCKET_ATTENTION,
    "unclear": BUCKET_ATTENTION,
    "addressed": BUCKET_ADDRESSED,
    "invalid": BUCKET_IGNORED,
    "ignored": BUCKET_IGNORED,
}

BUCKET_ORDER = (BUCKET_OPEN, BUCKET_FOLLOWUP, BUCKET_ATTENTION, BUCKET_ADDRESSED, BUCKET_IGNORED)

BUCKET_LABELS = {
    BUCKET_OPEN: "Open",
    BUCKET_FOLLOWUP: "In flight",
    BUCKET_ATTENTION: "To check",
    BUCKET_ADDRESSED: "Kept",
    BUCKET_IGNORED: "Set aside",
}

# What each effective status is called on the page.
STATUS_LABELS = {
    "open": "Open",
    "pending": "Not checked",
    "stale": "Check outdated",
    "unclear": "Unclear",
    "addressed": "Kept",
    "in_followup": "In flight",
    "invalid": "Not needed",
    "ignored": "Ignored",
}


@dataclass
class Followup:
    """A follow-up change linked to an item, by hand or found by the portal."""

    number: int
    status: str = ""  # NEW / MERGED / ABANDONED, "" until fetched
    subject: str = ""
    current_patchset: int | None = None
    reason: str = ""  # why the portal suggests it, e.g. "names LU-19999"

    @property
    def merged(self) -> bool:
        return self.status == "MERGED"


@dataclass
class Item:
    """One promise -- a harvested thread, or an item added by hand."""

    change_number: int
    tid: str  # storage key: thread id, or a manual id ("m1")
    item_id: str  # display/export id, e.g. "64620-a08dac95"
    origin: str  # "thread" | "manual"

    summary: str = ""
    severity: str | None = None
    file_path: str | None = None
    line: int | None = None
    patch_set: int | None = None
    updated: str = ""  # newest activity, sortable

    # Thread payload (origin == "thread")
    root: dict | None = None
    replies: list = field(default_factory=list)
    code_context: str | None = None
    is_resolved: bool = False
    kind: str | None = None
    promise_quote: str | None = None
    promised_by: str | None = None  # author of the message with the quote
    asked_by: str | None = None  # who opened the thread
    tickets: list = field(default_factory=list)  # tickets the thread names
    keyword_hit: bool = False  # promise-like wording, before any AI

    classification: dict | None = None
    adjudication: dict | None = None
    override: dict | None = None

    effective_status: str = "pending"
    manual_status: bool = False  # status came from an override / manual item
    stale_info: str | None = None  # "judged at PS12, change now at PS14"
    prior_verdict: str | None = None  # the verdict shown while outdated
    followup: Followup | None = None  # linked by hand
    candidates: list = field(default_factory=list)  # Followups the portal found
    duplicate_of: str | None = None  # tid of the promise this one repeats
    duplicates: list = field(default_factory=list)  # Items that repeat this one
    group_reason: str = ""  # why they were grouped
    # The follow-up change in review that does what was promised -- linked
    # by hand, or named by the check.
    followup_in_review: Followup | None = None
    addressed_via_followup: bool = False
    gerrit_url: str | None = None
    # From the last check: what it looked at, and the changes it named as
    # working toward the promise without keeping it.
    looked_at: list = field(default_factory=list)  # Followups
    related: list = field(default_factory=list)  # Followups

    @property
    def bucket(self) -> str:
        return STATUS_BUCKET.get(self.effective_status, BUCKET_ATTENTION)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.effective_status, self.effective_status)

    @property
    def note(self) -> str:
        return (self.override or {}).get("note") or ""

    @property
    def resolved_but_open(self) -> bool:
        """A promise still open in a thread someone marked resolved --
        the case that gets lost."""
        return self.is_resolved and self.effective_status in ("open", "pending", "stale")

    @property
    def in_open_followup(self) -> bool:
        return self.effective_status == "in_followup"

    @property
    def kept_in_change(self) -> int | None:
        """The change the check says keeps the promise, if it named one."""
        adj = self.adjudication or {}
        ai = adj.get("addressed_in") or {}
        if self.stale_info or adj.get("verdict") != "addressed" or ai.get("kind") != "change":
            return None
        ref = str(ai.get("ref") or "").strip().lstrip("#")
        return int(ref) if ref.isdigit() else None

    @property
    def followups_ruled_out(self) -> bool:
        """The check looked at the possible follow-ups and none keeps the
        promise -- they are no longer worth showing up front."""
        adj = self.adjudication or {}
        return (
            bool(adj)
            and not self.stale_info
            and adj.get("verdict") in ("still-open", "unclear", "invalid")
        )

    def severity_rank(self) -> int:
        try:
            return SEVERITIES.index(self.severity)
        except ValueError:
            return len(SEVERITIES)


def summarize(items: list[Item]) -> dict[str, int]:
    counts = dict.fromkeys(BUCKET_ORDER, 0)
    for it in items:
        counts[it.bucket] += 1
    return counts


def short_ids(tids: list[str]) -> dict[str, str]:
    """Map each thread id to its first '_' segment, falling back to the
    full id where two threads of one change share it."""
    firsts: dict[str, int] = {}
    for t in tids:
        head = t.split("_")[0]
        firsts[head] = firsts.get(head, 0) + 1
    return {t: (t.split("_")[0] if firsts[t.split("_")[0]] == 1 else t) for t in tids}


def thread_text(root_message: str, reply_messages: list[str]) -> str:
    """The canonical thread text its text_hash is computed over."""
    return "\n--\n".join([root_message or ""] + [m or "" for m in reply_messages])


def normalize_path(fp: str | None) -> str | None:
    """Gerrit's /PATCHSET_LEVEL means change-level (None); other magic
    paths such as /COMMIT_MSG keep their name without the slash."""
    if not fp or fp == "/PATCHSET_LEVEL":
        return None
    return fp.lstrip("/") if fp.startswith("/") else fp


def sort_items(items: list[Item]) -> list[Item]:
    """Severity first (high before unset), newest activity within a tier.

    Timestamps come in two shapes (Gerrit "2026-08-28 13:56:46.000000000",
    ours "2026-08-31T12:00:00Z"); normalised so the comparison is
    chronological across both.
    """
    by_age = sorted(
        items, key=lambda it: (it.updated or "").replace("T", " ").rstrip("Z"), reverse=True
    )
    return sorted(by_age, key=Item.severity_rank)


def ticket_re(prefix: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(prefix)}-\d{{2,7}}\b")
