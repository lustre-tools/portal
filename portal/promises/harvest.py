"""Harvesting: Gerrit -> the harvest section of a change document.

Uses the gerrit_cli library in-process (the same package the graph tool
installs), with the credentials it reads from GERRIT_URL / GERRIT_USER /
GERRIT_PASS. Read-only against Gerrit.

Every thread is kept, resolved ones included -- a promise is often made
in a thread someone then marks resolved. Two things are worked out here
without any AI, so they are free:

* ``mechanical``: only mechanical checkers (checkpatch, the janitor)
  took part. Such a thread never reaches the classifier -- on real
  changes that is half to four fifths of all threads. The AI reviewer
  and the gatekeeper are reviewers, not checkers, and always count.
* ``keyword_hit``: promise-like wording ("follow-up", "separate patch",
  "will fix", a named ticket, ...). Only a preview for changes Claude has
  not looked at; it misses about one promise in five, so it never
  decides anything on its own.

Review messages -- the text posted with a vote -- are turned into
change-level threads of their own, since a promise sometimes lives there
("+1, the cleanups can follow after landing").
"""

from __future__ import annotations

import hashlib
import re

from .models import normalize_path, now_iso, thread_text

CONTEXT_LINES = 5

#: Accounts that only ever post mechanical findings. Matched exactly
#: (case-insensitive) -- a pattern would catch reviewers too.
DEFAULT_MECHANICAL = (
    "wc-checkpatch",
    "Lustre Gerrit Janitor",
    "Lustre RISC-V Builder",
    "Maloo",
    "jenkins",
)

PROMISE_WORDS = re.compile(
    r"follow[- ]?(up|on)|separate (patch|change|ticket|commit)"
    r"|(later|future|another|next|subsequent|new) (patch|change|ticket)"
    r"|in a (later|separate|future)|will (be )?(fix|address|handl|port|clean|do|add|look)"
    r"|(file|filed|open|opened|create|created) (a )?(new )?(ticket|bug|patch|jira)"
    r"|\bTODO\b|if (it is |this is |the patch is )?refreshed|port\b.{0,40}\bto b[_\d]"
    r"|predates this|pre-existing|out of scope|not in this patch|after (this )?land",
    re.IGNORECASE,
)

# The vote line and comment count Gerrit puts at the top of a review
# message; what is left after them is what the reviewer wrote.
_MSG_HEADER = re.compile(r"^(Patch Set \d+:[^\n]*|\(\d+ (inline )?comments?\))\s*$", re.MULTILINE)
_SYSTEM_MSG = re.compile(
    r"^(Hashtags? (added|removed)|Topic |Removed |Change has been|Build |Uploaded patch set)", re.I
)
# A tool posting under a person's account tags itself ("[Marc Bot] Note:
# this change has been cherry-picked to master-next"): automated,
# whoever the author is.
_TAGGED_BOT = re.compile(r"^\[[^\]\n]{0,40}\bbot\]", re.I)
# An automated review's summary message: it points at its own inline
# comments ("Some concerns, see inline comments"), which are threads of
# their own -- as a promise it only duplicates them.
_REVIEW_SUMMARY = re.compile(
    r"AI code review|AI review|Sashiko|automated review|experimental feature testing"
    r"|see (the )?inline|comments? inline|inline (comments|items)",
    re.I,
)
# A follow-up named by its number: "follow-up: 68339", "addressed in 67878".
_NAMED_CHANGE = re.compile(
    r"(?:follow[- ]?(?:up|on)|followup|addressed in|fixed in|done in|see change|in change|change)"
    r"[\s:#(]{1,4}(?<![-\w])(\d{4,7})\b",
    re.I,
)


def change_links(messages: list[str], gerrit_url: str, own: int | None = None) -> list[int]:
    """Gerrit changes linked in the text: ".../c/<project>/+/68697" or
    "<gerrit host>/68697". A reply like "Addressed in <link>" is the
    strongest follow-up signal a thread can carry."""
    host = re.escape(re.sub(r"^https?://", "", gerrit_url.rstrip("/")))
    pat = re.compile(rf"/c/[\w./-]+/\+/(\d+)|{host}/(?:#/c/)?(\d+)\b")
    out: list[int] = []
    for m in messages:
        found = [int(a or b) for a, b in pat.findall(m or "")]
        found += [int(n) for n in _NAMED_CHANGE.findall(m or "")]
        for num in found:
            if num != own and num not in out:
                out.append(num)
    return out


def _author_name(author: dict | None) -> str:
    return (author or {}).get("name") or "unknown"


def _comment_view(c: dict) -> dict:
    return {
        "author": _author_name(c.get("author")),
        "updated": c.get("updated", ""),
        "message": c.get("message", ""),
    }


def _is_mechanical(authors: list[str], mechanical: set[str]) -> bool:
    return bool(authors) and all(a.lower() in mechanical for a in authors)


def _has_promise_words(messages: list[str]) -> bool:
    return any(PROMISE_WORDS.search(m or "") for m in messages)


def _format_context(ctx: dict | None) -> str | None:
    if not ctx or not ctx.get("lines"):
        return None
    out = []
    start = ctx.get("start_line") or 1
    target = ctx.get("target_line")
    for i, line in enumerate(ctx["lines"], start=start):
        out.append(f"{'>>> ' if i == target else '    '}{i:4}: {line}")
    return "\n".join(out)


def _thread_view(
    t: dict, mechanical: set[str], gerrit_url: str = "", own: int | None = None
) -> dict:
    root = t.get("root_comment") or {}
    replies = list(t.get("replies") or [])
    messages = [root.get("message", "")] + [r.get("message", "") for r in replies]
    root_view = _comment_view(root)
    reply_views = [_comment_view(r) for r in replies]
    return {
        "id": root.get("id"),
        "file_path": normalize_path(root.get("file_path")),
        "line": root.get("line"),
        "patch_set": root.get("patch_set"),
        "is_resolved": bool(t.get("is_resolved")),
        "root": root_view,
        "replies": reply_views,
        "code_context": _format_context(root.get("code_context")),
        "text_hash": hashlib.sha256(
            thread_text(messages[0], messages[1:]).encode("utf-8", "replace")
        ).hexdigest(),
        "mechanical": _is_mechanical(
            [root_view["author"]] + [r["author"] for r in reply_views], mechanical
        ),
        "keyword_hit": _has_promise_words(messages),
        "links": change_links(messages, gerrit_url, own) if gerrit_url else [],
    }


def review_message_body(text: str) -> str:
    """A review message minus Gerrit's vote line and comment count."""
    return _MSG_HEADER.sub("", text or "").strip()


def _message_threads(
    messages: list[dict], mechanical: set[str], gerrit_url: str = "", own: int | None = None
) -> list[dict]:
    out = []
    for m in messages:
        author = _author_name(m.get("author"))
        body = review_message_body(m.get("message", ""))
        automated = (
            author.lower() in mechanical
            or _SYSTEM_MSG.match(body)
            or _TAGGED_BOT.match(body)
            or _REVIEW_SUMMARY.search(body[:400])
        )
        if not body or not m.get("id") or automated:
            continue
        out.append(
            {
                "id": f"msg-{m['id']}",
                "file_path": None,
                "line": None,
                "patch_set": m.get("patch_set"),
                "is_resolved": False,
                "is_message": True,
                "root": {"author": author, "updated": m.get("date", ""), "message": body},
                "replies": [],
                "code_context": None,
                "text_hash": hashlib.sha256(
                    thread_text(body, []).encode("utf-8", "replace")
                ).hexdigest(),
                "mechanical": False,
                "keyword_hit": _has_promise_words([body]),
                "links": change_links([body], gerrit_url, own) if gerrit_url else [],
            }
        )
    return out


def harvest_change(gerrit_url: str, number: int, mechanical=DEFAULT_MECHANICAL) -> dict:
    """Fetch every thread and review message of a change.

    Returns ``{"change_number": ..., "harvest": {...}}`` -- the harvest
    section of the change document.
    """
    from gerrit_cli.extractor import extract_comments

    mech = {m.lower() for m in mechanical}
    result = extract_comments(
        f"{gerrit_url.rstrip('/')}/{int(number)}",
        include_resolved=True,
        include_code_context=True,
        context_lines=CONTEXT_LINES,
    )
    data = result.to_dict()
    info = data["change_info"]
    own = info["change_number"]
    threads = [_thread_view(t, mech, gerrit_url, own) for t in data["threads"]]
    threads = [t for t in threads if t["id"]]
    threads += _message_threads(data.get("review_messages") or [], mech, gerrit_url, own)
    return {
        "change_number": info["change_number"],
        "harvest": {
            "harvested_at": now_iso(),
            "gerrit_url": info.get("url"),
            "change": {
                "subject": info.get("subject", ""),
                "project": info.get("project", ""),
                "branch": info.get("branch", ""),
                "status": info.get("status", ""),
                "owner": ((info.get("owner") or {}).get("name") or ""),
                "current_revision": info.get("current_revision", ""),
                "current_patchset": info.get("current_patchset", 0),
            },
            "threads": threads,
        },
    }


def change_project(gerrit_url: str, number: int) -> dict:
    """Project, branch and subject of a change, for the access check done
    before tracking it. Raises on any failure."""
    from gerrit_cli.client import GerritCommentsClient

    client = GerritCommentsClient(gerrit_url)
    d = client.get_change(int(number))
    return {
        "project": d.get("project", ""),
        "branch": d.get("branch", ""),
        "subject": d.get("subject", ""),
    }
