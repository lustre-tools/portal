"""Markdown export: hand one change's promises to a person or a Claude
session ("address item 64620-a08dac95"). Pure rendering over Item views."""

from __future__ import annotations

from .models import (
    BUCKET_ADDRESSED,
    BUCKET_ATTENTION,
    BUCKET_FOLLOWUP,
    BUCKET_IGNORED,
    BUCKET_OPEN,
    Item,
    sort_items,
)


def _quote(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit] + " […]"
    return "\n".join("> " + line for line in text.splitlines() or [""])


def _location(it: Item) -> str:
    return f"{it.file_path}:{it.line or '-'}" if it.file_path else "(change-level)"


def _one_liner(it: Item) -> str:
    parts = [f"- **{it.item_id}**", _location(it), "—", it.summary]
    ai = (it.adjudication or {}).get("addressed_in")
    if it.addressed_via_followup and it.followup:
        parts.append(f"(kept in follow-up {it.followup.number}, merged)")
    elif it.in_open_followup and it.followup:
        parts.append(f"(in flight: follow-up {it.followup.number}, still in review)")
    elif it.manual_status:
        parts.append(f"(set by hand{': ' + chr(34) + it.note + chr(34) if it.note else ''})")
    elif ai:
        parts.append(f"(kept in {ai.get('kind')} {ai.get('ref') or ''} — {ai.get('detail') or ''})")
    elif it.effective_status == "invalid":
        parts.append("(judged not needed)")
    return " ".join(p for p in parts if p)


def _full_block(it: Item) -> list[str]:
    out = [
        f"### {it.item_id} — {_location(it)}"
        + (f" [severity: {it.severity}]" if it.severity else "")
    ]
    if it.effective_status == "stale":
        out.append(
            f"*Status: check outdated — {it.stale_info}; earlier verdict: {it.prior_verdict}.*"
        )
    elif it.effective_status in ("pending", "unclear"):
        out.append(f"*Status: {it.status_label.lower()}.*")
    if it.resolved_but_open:
        out.append("*The thread is marked resolved, but the item is still open.*")
    out.append(f"**Summary:** {it.summary}")
    if it.origin == "manual":
        out.append("**Origin:** added by hand (no Gerrit thread)")
    else:
        origin = f"comment on PS{it.patch_set}" + (", thread resolved" if it.is_resolved else "")
        if it.promise_quote:
            who = f" by {it.promised_by}" if it.promised_by else ""
            origin += f'; promise{who}: "{it.promise_quote}"'
        out.append(f"**Origin:** {origin}")
        lines = []
        if it.root:
            lines.append(
                f"[{it.root.get('author')}, {it.root.get('updated', '')[:10]}] {it.root.get('message', '')}"
            )
        for r in it.replies:
            lines.append(f"[{r.get('author')}, {r.get('updated', '')[:10]}] {r.get('message', '')}")
        out.append("**Thread:**")
        out.append(_quote("\n".join(lines)))
        if it.code_context:
            out.append("**Code context:**")
            out.append("```\n" + it.code_context + "\n```")
    adj = it.adjudication
    if adj and not adj.get("raw_error"):
        out.append(
            f"**Check (AI, {adj.get('judged_at', '')[:10]}, verdict {adj.get('verdict')}):** {adj.get('evidence') or '-'}"
        )
        if it.related:
            out.append(
                "**Related work:** "
                + ", ".join(f"{f.number} ({f.status or '?'}): {f.subject}" for f in it.related)
            )
        if adj.get("suggested_action"):
            out.append(f"**Suggested action:** {adj['suggested_action']}")
    if it.duplicates:
        out.append(
            "**Also raised in:** "
            + ", ".join(
                f"{d.item_id} ({d.file_path or 'change-level'}, PS{d.patch_set})"
                for d in it.duplicates
            )
        )
    if it.followup_in_review:
        f = it.followup_in_review
        out.append(f"**In flight:** change {f.number} ({f.status or 'in review'}): {f.subject}")
    if it.candidates:
        found = ", ".join(f"{c.number} ({c.status or '?'}, {c.reason})" for c in it.candidates)
        out.append(f"**Possible follow-ups:** {found}")
    if it.followup and not it.addressed_via_followup:
        out.append(
            f"**Linked follow-up:** change {it.followup.number} ({it.followup.status or 'status unknown'})"
        )
    if it.note:
        out.append(f"**Note:** {it.note}")
    if it.gerrit_url:
        out.append(f"**Gerrit:** {it.gerrit_url}")
    return out


def render_markdown(change_doc: dict, items: list[Item]) -> str:
    number = change_doc.get("change_number")
    hv = change_doc.get("harvest") or {}
    change = hv.get("change") or {}
    by_bucket: dict[str, list[Item]] = {}
    for it in sort_items(items):
        by_bucket.setdefault(it.bucket, []).append(it)

    out = [
        f"# Promises — change {number}: {change.get('subject', '')}",
        f"Branch {change.get('branch', '')} · PS{change.get('current_patchset', '?')} · "
        f"{change.get('status', '')} · {hv.get('gerrit_url', '')}",
        f"Harvested {hv.get('harvested_at', '')[:10]}",
    ]
    judged = [it.adjudication for it in items if it.adjudication]
    if judged:
        latest = max(judged, key=lambda a: a.get("judged_at", ""))
        out.append(
            f"Checked at PS{latest.get('judged_patchset')} ({latest.get('judged_revision', '')[:12]}), "
            f"master {latest.get('judged_master', '')[:12]}"
        )
    out.append(
        f'Refer to items by id, e.g. "address item {items[0].item_id}".' if items else "No items."
    )

    for bucket, title in (
        (BUCKET_OPEN, "Open"),
        (BUCKET_FOLLOWUP, "In flight"),
        (BUCKET_ATTENTION, "To check"),
    ):
        group = by_bucket.get(bucket, [])
        if group or bucket == BUCKET_OPEN:
            out.append(f"\n## {title} ({len(group)})\n")
            for it in group:
                out.extend(_full_block(it))
                out.append("")
    for bucket, title in ((BUCKET_ADDRESSED, "Kept"), (BUCKET_IGNORED, "Set aside")):
        group = by_bucket.get(bucket, [])
        if group:
            out.append(f"\n## {title} ({len(group)})\n")
            out.extend(_one_liner(it) for it in group)
    return "\n".join(out) + "\n"
