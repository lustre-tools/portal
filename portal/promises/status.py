"""Effective status: stored documents -> :class:`Item` views.

Pure logic, fully unit-testable. The one rule everything relies on:

    manual override  >  merged follow-up link  >  judgment  >  pending

with staleness in between: a judgment whose stamps no longer match the
harvested thread, the current patchset or the configured prompt/model is
shown as "stale" -- bucket "to check" -- with its verdict still visible.
Judgments are never silently thrown away; re-judging only happens when
someone asks, because it is the part that costs money.
"""

from __future__ import annotations

import hashlib
import re

from .models import (
    CLASSIFY_PROMPT_VERSION,
    GROUP_PROMPT_VERSION,
    ITEM_KINDS,
    JUDGE_PROMPT_VERSION,
    Followup,
    Item,
    normalize_path,
    short_ids,
    ticket_re,
)

_VERDICT_STATUS = {
    "still-open": "open",
    "in-followup": "in_followup",
    "addressed": "addressed",
    "invalid": "invalid",
    "unclear": "unclear",
}

# Decisions made by hand. "not_needed": the promise no longer applies --
# the thing already exists, or the reason for it went away.
_OVERRIDE_STATUS = {
    "done": "addressed",
    "open": "open",
    "not_needed": "invalid",
    "ignored": "ignored",
}
DECISIONS = tuple(_OVERRIDE_STATUS)


def classification_is_stale(cls: dict | None, text_hash: str) -> bool:
    """True when the thread must go through classification (again)."""
    if not cls:
        return True
    return cls.get("text_hash") != text_hash or cls.get("prompt_version") != CLASSIFY_PROMPT_VERSION


def adjudication_staleness(
    adj: dict | None, text_hash: str, current_revision: str | None, model: str
) -> str | None:
    """None when fresh; otherwise a short reason, shown on the page."""
    if not adj:
        return None
    if adj.get("text_hash") != text_hash:
        return "the thread changed since it was checked"
    if current_revision and adj.get("judged_revision") != current_revision:
        return f"checked at PS{adj.get('judged_patchset', '?')}; the change has a newer patchset"
    if adj.get("prompt_version") != JUDGE_PROMPT_VERSION:
        return "the check itself was improved since"
    if adj.get("model", "") != (model or ""):
        return "checked with a different model"
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def promised_by(thread: dict, quote: str | None) -> str | None:
    """The author of the message the promise quote comes from.

    Deterministic, so older classifications -- stored before the
    classifier was asked -- get it too. Matches on the first 60
    characters of the quote, whitespace-normalised, newest message first
    (a promise is usually the answer, not the question).
    """
    if not quote:
        return None
    needle = _norm(quote)[:60]
    if not needle:
        return None
    messages = [thread.get("root") or {}] + list(thread.get("replies") or [])
    for m in reversed(messages):
        if needle in _norm(m.get("message", "")):
            return m.get("author") or None
    return None


def thread_tickets(thread: dict, prefix: str) -> list[str]:
    """Tickets named anywhere in the thread, in order of appearance."""
    pat = ticket_re(prefix)
    seen: list[str] = []
    messages = [thread.get("root") or {}] + list(thread.get("replies") or [])
    for m in messages:
        for t in pat.findall(m.get("message", "")):
            if t not in seen:
                seen.append(t)
    return seen


# Words that make a ticket in the same sentence the promise's target:
# "I filed LU-20566", "deferred to LU-20565", "a patch in LU-19999".
_PROMISE_TICKET_WORDS = re.compile(
    r"\b(file[ds]?|filing|ticket|jira|track(ed|ing)?|defer(red)?|follow[- ]?(up|on)|followup"
    r"|separate|later|future|patch (in|for|under)|fix(ed)? in|address(ed)? in|open(ed)?|creat(e|ed))\b",
    re.I,
)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def promise_tickets(thread: dict, prefix: str) -> list[str]:
    """Tickets a promise points at -- not every ticket in the thread.

    Only replies count (a review message counts as its own reply), and
    only a ticket in the same sentence as promise wording. The comment
    that opens a thread quotes code and context -- "always_except
    LU-12668 41d 53a" -- and a ticket there is where the problem is
    tracked, not where it will be fixed; an umbrella ticket found that
    way listed every patch of the feature as a "follow-up".
    """
    pat = ticket_re(prefix)
    messages = list(thread.get("replies") or [])
    if thread.get("is_message"):
        messages = [thread.get("root") or {}]
    seen: list[str] = []
    for m in messages:
        for sentence in _SENTENCE.findall(m.get("message", "") or ""):
            if not _PROMISE_TICKET_WORDS.search(sentence):
                continue
            for t in pat.findall(sentence):
                if t not in seen:
                    seen.append(t)
    return seen


def _followup_for(override: dict | None, manual: dict | None, linked: dict) -> Followup | None:
    src = override or manual or {}
    num = src.get("followup_change")
    if not num:
        return None
    info = linked.get(str(num), {})
    return Followup(
        number=int(num),
        status=info.get("status", ""),
        subject=info.get("subject", ""),
        current_patchset=info.get("current_patchset"),
    )


def known_changes(change_doc: dict) -> dict[int, dict]:
    """Everything the portal knows about other changes, by number: found
    by link, ticket, the relation chain, or linked by hand."""
    cands = change_doc.get("candidates") or {}
    out: dict[int, dict] = {}
    pools = [cands.get("stacked") or [], list((cands.get("by_link") or {}).values())]
    pools += list((cands.get("by_ticket") or {}).values())
    pools += [
        [
            {"number": int(k), **v}
            for k, v in (change_doc.get("linked_changes") or {}).items()
            if k.isdigit()
        ]
    ]
    for pool in pools:
        for c in pool:
            num = c.get("number")
            if isinstance(num, int) and num not in out:
                out[num] = c
    return out


def _follow_the_followup(item: Item, known: dict[int, dict]) -> None:
    """A promise waiting on a follow-up change in review: record which,
    and let the change's own state decide once it lands or is dropped.

    Linked by hand, a merged follow-up already made the item "kept" in
    _resolve. Named by the check (verdict "in-followup"), the item is
    kept once that change merges, and open again if it is abandoned.
    """
    if item.effective_status != "in_followup":
        return
    if item.followup and item.followup.status == "NEW":
        item.followup_in_review = item.followup  # linked by hand
        return
    ai = (item.adjudication or {}).get("addressed_in") or {}
    ref = str(ai.get("ref") or "").strip().lstrip("#")
    if not ref.isdigit():
        return
    info = known.get(int(ref)) or {}
    fu = Followup(
        number=int(ref),
        status=info.get("status", ""),
        subject=info.get("subject", "") or ai.get("detail", ""),
        current_patchset=info.get("current_patchset"),
        reason="named by the check",
    )
    if fu.status == "MERGED":
        item.effective_status = "addressed"
        item.addressed_via_followup = True
        item.followup = item.followup or fu
    elif fu.status == "ABANDONED":
        item.effective_status = "open"
    else:
        item.followup_in_review = fu


def _candidates_for(
    tickets: list[str],
    own_ticket: str,
    found: dict,
    links: list[int] | None = None,
    by_link: dict | None = None,
    since: str = "",
) -> list[Followup]:
    """Follow-up changes the portal found for the tickets a thread names.

    The change's own ticket is left out here: in a patch series every
    sibling shares it, and listing them all under every promise says
    nothing. They are shown once, for the whole change, instead.
    """
    out: list[Followup] = []
    seen: set[int] = set()
    for num in links or []:
        c = (by_link or {}).get(str(num))
        if c and num not in seen:
            seen.add(num)
            out.append(
                Followup(
                    number=int(num),
                    status=c.get("status", ""),
                    subject=c.get("subject", ""),
                    current_patchset=c.get("current_patchset"),
                    reason="linked in the thread",
                )
            )
    for t in tickets:
        if t == own_ticket:
            continue
        for c in found.get(t) or []:
            num = c.get("number")
            if not num or num in seen:
                continue
            # A change that existed before the thread cannot be the
            # follow-up of something promised in it.
            created = (c.get("created") or "")[:19]
            if since and created and created < since:
                continue
            seen.add(num)
            out.append(
                Followup(
                    number=int(num),
                    status=c.get("status", ""),
                    subject=c.get("subject", ""),
                    current_patchset=c.get("current_patchset"),
                    reason=f"names {t}",
                )
            )
    return out


def _resolve(item: Item) -> None:
    ov_status = (item.override or {}).get("status")
    if ov_status in _OVERRIDE_STATUS:
        item.effective_status = _OVERRIDE_STATUS[ov_status]
        item.manual_status = True
        return
    if item.followup and item.followup.merged:
        item.effective_status = "addressed"
        item.addressed_via_followup = True
        return
    if item.followup and item.followup.status == "NEW":
        # Linked to a follow-up still in review: handled, not landed yet.
        # It turns into "kept" when that change merges, and falls back to
        # the verdict if it is abandoned.
        item.effective_status = "in_followup"
        return
    if item.origin == "manual":
        item.effective_status = "open"
        item.manual_status = True
        return
    adj = item.adjudication
    if not adj:
        item.effective_status = "pending"
        return
    if item.stale_info:
        item.effective_status = "stale"
        item.prior_verdict = adj.get("verdict")
        return
    item.effective_status = _VERDICT_STATUS.get(adj.get("verdict", ""), "unclear")


def _thread_updated(thread: dict) -> str:
    stamps = [thread.get("root", {}).get("updated", "")]
    stamps += [r.get("updated", "") for r in thread.get("replies", [])]
    return max((s for s in stamps if s), default="")


def _gerrit_thread_url(harvest: dict, thread: dict) -> str | None:
    base = harvest.get("gerrit_url")
    if not base:
        return None
    if thread.get("is_message"):
        return base  # a review message has no link of its own
    # Gerrit links straight to a comment, inline or change-level alike.
    return f"{base.rstrip('/')}/comment/{thread.get('id')}/"


def own_ticket(change_doc: dict, prefix: str) -> str:
    subject = ((change_doc.get("harvest") or {}).get("change") or {}).get("subject", "")
    m = ticket_re(prefix).match(subject or "")
    return m.group(0) if m else ""


def build_items(
    change_doc: dict,
    overrides_doc: dict,
    judge_model: str = "",
    ticket_prefix: str = "LU",
) -> tuple[list[Item], list[Item]]:
    """Return (items, non_items) for one tracked change.

    ``items`` are the threads the classifier (or an override) made a
    promise, plus manual items. ``non_items`` are the other threads a
    person or the AI reviewer took part in -- listed so a classifier miss
    can be spotted and promoted. Threads only mechanical checkers took
    part in are neither: they never reach the classifier.
    """
    number = change_doc.get("change_number", 0)
    harvest = change_doc.get("harvest") or {}
    threads = harvest.get("threads") or []
    current_revision = (harvest.get("change") or {}).get("current_revision")
    linked = change_doc.get("linked_changes") or {}
    found = (change_doc.get("candidates") or {}).get("by_ticket") or {}
    by_link = (change_doc.get("candidates") or {}).get("by_link") or {}
    classifications = change_doc.get("classifications") or {}
    adjudications = change_doc.get("adjudications") or {}
    overrides = overrides_doc.get("overrides") or {}
    manual_items = overrides_doc.get("manual_items") or {}
    mine = own_ticket(change_doc, ticket_prefix)

    ids = short_ids([t["id"] for t in threads if t.get("id")])

    items: list[Item] = []
    non_items: list[Item] = []
    for t in threads:
        tid = t.get("id")
        if not tid:
            continue
        cls = classifications.get(tid)
        ov = overrides.get(tid)
        if t.get("mechanical") and not ov:
            continue
        is_item = bool(ov) or (cls or {}).get("kind") in ITEM_KINDS
        tickets = promise_tickets(t, ticket_prefix)
        since = ((t.get("root") or {}).get("updated") or "").replace("T", " ")[:19]
        quote = (cls or {}).get("promise_quote")
        item = Item(
            change_number=number,
            tid=tid,
            item_id=f"{number}-{ids.get(tid, tid)}",
            origin="thread",
            summary=(ov or {}).get("summary") or (cls or {}).get("summary") or _first_line(t),
            severity=(ov or {}).get("severity") or (cls or {}).get("severity"),
            file_path=normalize_path(t.get("file_path")),
            line=t.get("line"),
            patch_set=t.get("patch_set"),
            updated=_thread_updated(t),
            root=t.get("root"),
            replies=t.get("replies") or [],
            code_context=t.get("code_context"),
            is_resolved=bool(t.get("is_resolved")),
            kind=(cls or {}).get("kind"),
            promise_quote=quote,
            promised_by=promised_by(t, quote),
            asked_by=(t.get("root") or {}).get("author"),
            tickets=tickets,
            keyword_hit=bool(t.get("keyword_hit")),
            classification=cls,
            adjudication=adjudications.get(tid),
            override=ov,
            followup=_followup_for(ov, None, linked),
            candidates=_candidates_for(tickets, mine, found, t.get("links"), by_link, since),
            gerrit_url=_gerrit_thread_url(harvest, t),
        )
        item.stale_info = adjudication_staleness(
            item.adjudication, t.get("text_hash", ""), current_revision, judge_model
        )
        if is_item:
            _resolve(item)
            items.append(item)
        else:
            non_items.append(item)

    known = known_changes(change_doc)
    for it in items:
        _follow_the_followup(it, known)
    _apply_groups(items, change_doc.get("groups"), overrides)

    for mid, m in sorted(manual_items.items()):
        item = Item(
            change_number=number,
            tid=mid,
            item_id=f"{number}-{mid}",
            origin="manual",
            summary=m.get("summary") or "(no summary)",
            severity=m.get("severity"),
            file_path=m.get("file_path"),
            line=m.get("line"),
            updated=m.get("created_at", ""),
            override={
                "status": m.get("status"),
                "note": m.get("note", ""),
                "followup_change": m.get("followup_change"),
            },
            followup=_followup_for(None, m, linked),
        )
        _resolve(item)
        items.append(item)

    return items, non_items


def _apply_groups(items: list[Item], groups_doc: dict | None, overrides: dict) -> None:
    """Link duplicates to one primary each.

    Groups come from the grouping call, adjusted by decisions made by
    hand: "not a duplicate" takes an item out of its group, "duplicate
    of <item>" puts it into that item's group. The primary is the member
    from the latest patchset -- the most current statement of the work.
    A duplicate shows its primary's status unless it has a decision of
    its own.
    """
    by_tid = {it.tid: it for it in items if it.origin == "thread"}
    parent: dict[str, str] = {}

    def find(t: str) -> str:
        while parent.get(t, t) != t:
            t = parent[t]
        return t

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    reasons: dict[str, str] = {}
    split = {t for t, ov in overrides.items() if (ov or {}).get("not_duplicate")}
    for g in (groups_doc or {}).get("groups") or []:
        ids = [i for i in g.get("ids") or [] if i in by_tid and i not in split]
        for other in ids[1:]:
            union(ids[0], other)
        for i in ids:
            reasons.setdefault(i, g.get("reason") or "")
    for t, ov in overrides.items():
        target = (ov or {}).get("duplicate_of")
        if target and t in by_tid and target in by_tid and t != target:
            union(target, t)
            reasons[t] = "marked by hand"

    members: dict[str, list[Item]] = {}
    for t, it in by_tid.items():
        members.setdefault(find(t), []).append(it)
    for group in members.values():
        if len(group) < 2:
            continue
        primary = max(
            group, key=lambda it: ((it.patch_set or 0), (it.updated or "").replace("T", " "))
        )
        for it in sorted(group, key=lambda it: it.patch_set or 0):
            if it is primary:
                continue
            it.duplicate_of = primary.tid
            it.group_reason = reasons.get(it.tid, "")
            primary.duplicates.append(it)
            if not it.manual_status:
                it.effective_status = primary.effective_status
                it.stale_info = primary.stale_info
                it.prior_verdict = primary.prior_verdict
        primary.group_reason = next(
            (r for r in (reasons.get(d.tid) for d in primary.duplicates) if r), ""
        )


def top_level(items: list[Item]) -> list[Item]:
    """The items to list and count: every promise once, duplicates folded
    into their primary."""
    return [it for it in items if not it.duplicate_of]


def group_input_hash(items: list[Item]) -> str:
    """What the grouping was computed over; a change means regroup."""
    key = "\n".join(sorted(f"{it.tid}\t{it.summary}" for it in items if it.origin == "thread"))
    return hashlib.sha256(key.encode()).hexdigest()


def groups_stale(change_doc: dict, items: list[Item]) -> bool:
    threads = [it for it in items if it.origin == "thread"]
    if len(threads) < 2:
        return False
    g = change_doc.get("groups") or {}
    return g.get("prompt_version") != GROUP_PROMPT_VERSION or g.get(
        "input_hash"
    ) != group_input_hash(threads)


def possible_promises(change_doc: dict, non_items: list[Item]) -> list[Item]:
    """Threads with promise-like wording that nobody has classified yet.

    The free preview for a change Claude has not looked at: a keyword
    match, clearly labelled as such. Once a thread is classified it is an
    item or a non-item and leaves this list.
    """
    classified = change_doc.get("classifications") or {}
    return [it for it in non_items if it.keyword_hit and it.tid not in classified]


def unclassified_count(change_doc: dict) -> int:
    """Threads that still need the classifier (mechanical ones never do)."""
    threads = (change_doc.get("harvest") or {}).get("threads") or []
    classified = change_doc.get("classifications") or {}
    return sum(
        1
        for t in threads
        if t.get("id")
        and not t.get("mechanical")
        and classification_is_stale(classified.get(t["id"]), t.get("text_hash", ""))
    )


def pending_and_stale_tids(
    change_doc: dict, overrides_doc: dict, judge_model: str, ticket_prefix: str = "LU"
) -> list[str]:
    """Threads the judge should (re)check: items without a fresh verdict,
    except those whose status was set by hand."""
    items, _ = build_items(change_doc, overrides_doc, judge_model, ticket_prefix)
    return [
        it.tid
        for it in top_level(items)
        if it.origin == "thread"
        and not it.manual_status
        and it.effective_status in ("pending", "stale", "unclear")
    ]


def _first_line(thread: dict) -> str:
    msg = (thread.get("root") or {}).get("message", "")
    line = msg.strip().splitlines()[0] if msg.strip() else "(empty comment)"
    return line[:160]
