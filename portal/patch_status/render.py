"""The Patch Status page body, as HTML.

Rendered once per run and stored; the web page wraps it in the site's
layout and adds the stylesheet for the viewer's design and the script
(static/patch_status/). Every selector those use sits under `.ps`.
"""

from __future__ import annotations

import html
import re
from typing import Optional
from urllib.parse import quote

from portal.patch_status.classify import (
    BUCKET_ORDER,
    IN_LINE_FOR_LANDING,
    READY_BUCKETS,
    READY_TO_LAND,
    is_blocker,
    is_most_wanted,
    owner_self_review_score,
    wanted_rank,
)
from portal.patch_status.comment import comment_for
from portal.patch_status.orgs import Orgs

COLUMNS = [
    "Change", "Chain", "Issue", "Component", "Summary",
    "V", "R", "C", "S", "D",
    "Owner", "Org", "Uploaded", "Updated", "Type", "Topic/Hashtag", "Comment",
]

LEGEND = [
    ("V", "Verified: both jenkins and Maloo must be +1, with no -1"),
    ("R", "Code-Review: at least two +1s from people other than the owner, and no -1 or -2"),
    ("C", "Clean cherry-pick: ✗ when the patch does not apply to the branch. For patches that pass every other check this is a trial cherry-pick onto today's branch; for the rest, wc-checkpatch's -1 vote. Stacked patches take the result of the first open patch of their series"),
    ("S", "Style: wc-checkpatch warnings; advisory, does not block landing"),
    ("D", "Dependees: every patch below in the series is merged or ready itself"),
    ("Chain", "position in the series; lower numbers land first; blank means no series"),
    ("Comment", "what the patch still needs"),
    ("B", "release blocker: a *_review_blocker hashtag; listed first in every bucket"),
    ("P", "release priority: a *_review_priority hashtag and not a blocker; listed after the blockers"),
    (IN_LINE_FOR_LANDING, "ready, and picked up in {landing}. Red rows are in {landing} although the patch is not ready"),
    (READY_TO_LAND, "ready, not in {landing} yet"),
    ("Needs review", "Jenkins and Maloo are both +1, Code-Review is short"),
    ("Needs verify", "Jenkins or Maloo has not voted +1 yet"),
    ("Fails verify or review", "any -1 from CI or a reviewer"),
    ("Dependee not ready", "ready itself, but a patch below it in the series is not"),
]

_CENTERED = {"Chain", "V", "R", "C", "S", "D"}

# Every "#<digits>" the Comment column says is a change of this branch.
_CHANGE_REF_RE = re.compile(r"#(\d+)")


def _esc(text) -> str:
    if text is None:
        return ""
    return html.escape(str(text).replace("\r", " ").replace("\n", " "), quote=True)


def bucket_anchor(bucket: str) -> str:
    return "bucket-" + bucket.lower().replace(" ", "-").replace("/", "-")


def _date(text: str) -> str:
    return (text or "").split(" ", 1)[0]


def _date_key(text: str) -> int:
    digits = [c for c in (text or "").split(".", 1)[0] if c.isdigit()]
    return int("".join(digits)) if digits else 0


def sort_for_bucket(rows: list[dict]) -> list[dict]:
    """Release blockers first, then priorities, then the rest; a series
    stays together (bottom first) and ranks by its newest update; series
    before standalone changes."""
    groups: dict = {}
    for ch in rows:
        groups.setdefault(ch.get("_series_id") or ch.get("_number"), []).append(ch)
    for members in groups.values():
        members.sort(key=lambda c: (c.get("series_position") or (0, 0))[0])

    def key(item):
        sid, members = item
        return (
            min(wanted_rank(c) for c in members),
            0 if isinstance(sid, tuple) else 1,
            -max(_date_key(c.get("updated") or "") for c in members),
        )

    return [ch for _, members in sorted(groups.items(), key=key) for ch in members]


def _ticket_rest(change: dict) -> str:
    subject = change.get("subject") or ""
    ticket = change.get("_jira_ticket")
    if ticket and subject.startswith(ticket):
        subject = subject[len(ticket):].lstrip(" -:")
    return subject


def component_text(change: dict) -> str:
    head = _ticket_rest(change).split(":", 1)[0].strip()
    if not head or (" " in head and len(head) > 30):
        return ""
    return head


def summary_text(change: dict) -> str:
    rest = _ticket_rest(change)
    parts = rest.split(":", 1)
    summary = parts[1].strip() if len(parts) > 1 else rest.strip()
    return summary[:87] + "…" if len(summary) > 90 else summary


def owner_text(change: dict) -> str:
    owner = change.get("owner") or {}
    return (owner.get("name") or owner.get("username") or "").strip()


def chain_text(change: dict) -> str:
    sp = change.get("series_position")
    return f"{sp[0]}/{sp[1]}" if sp else ""


class Page:
    def __init__(self, gerrit_url: str, jira_url: str, ticket_prefix: str = "LU",
                 orgs: Optional[Orgs] = None, commit_url: str = ""):
        self.gerrit = gerrit_url.rstrip("/")
        self.commit_url = commit_url
        self.jira = jira_url.rstrip("/")
        self.ticket_prefix = ticket_prefix
        self.orgs = orgs
        self.columns = [c for c in COLUMNS if c != "Org" or orgs is not None]

    # ---- cells ----

    def _change_url(self, change: dict, number) -> str:
        return f"{self.gerrit}/c/{change.get('project') or ''}/+/{number}"

    def _badge(self, change: dict) -> str:
        if is_blocker(change):
            return '<span class="badge blocker" title="release blocker">B</span> '
        if is_most_wanted(change):
            return '<span class="badge priority" title="release priority">P</span> '
        return ""

    def change_link(self, change: dict) -> str:
        n = change.get("_number")
        ps = change.get("current_revision_number") or "?"
        url = self._change_url(change, n)
        return f'{self._badge(change)}<a href="{_esc(url)}">{_esc(n)}/{_esc(ps)}</a>'

    def issue(self, change: dict) -> str:
        """Only the project's own Jira is public; anything else stays text."""
        ticket = change.get("_jira_ticket") or ""
        if ticket.startswith(self.ticket_prefix + "-"):
            return f'<a href="{_esc(self.jira)}/browse/{_esc(ticket)}">{_esc(ticket)}</a>'
        return _esc(ticket)

    def tags(self, change: dict) -> str:
        """Topic and hashtags, each a Gerrit search, the release ones marked
        like the badges."""
        items = []
        topic = (change.get("topic") or "").strip()
        if topic:
            items.append(("topic", topic, f"topic:{topic}", ""))
        for h in change.get("hashtags") or []:
            one = {**change, "hashtags": [h]}
            cls = "blocker" if is_blocker(one) else "priority" if is_most_wanted(one) else ""
            items.append(("hashtag", h, h, cls))
        return ", ".join(
            f'<a class="{cls}" href="{_esc(self.gerrit)}/q/{op}:{quote(value, safe="")}">{_esc(label)}</a>'
            for op, value, label, cls in items
        )

    def comment(self, change: dict) -> str:
        """The Comment text with '#<number>' as links: the change named is
        usually not the row's own, so a link saves a search."""
        text = comment_for(change)
        out, pos = [], 0
        for m in _CHANGE_REF_RE.finditer(text):
            out.append(_esc(text[pos:m.start()]))
            url = self._change_url(change, m.group(1))
            out.append(f'<a href="{_esc(url)}">#{_esc(m.group(1))}</a>')
            pos = m.end()
        out.append(_esc(text[pos:]))
        return "".join(out)

    def head_link(self, project: str, sha: str) -> str:
        code = f'<code title="{_esc(sha)}">{_esc(sha[:12])}</code>'
        if not self.commit_url:
            return code
        url = self.commit_url.format(project=quote(project, safe="/"), sha=quote(sha, safe=""))
        return f'<a href="{_esc(url)}">{code}</a>'

    @staticmethod
    def vote(score: int) -> str:
        if not score:
            return ""
        return f'<b class="{"good" if score > 0 else "bad"}">{score:+d}</b>'

    @staticmethod
    def mark(score: int, *, advisory: bool = False) -> str:
        if score >= 1:
            return '<b class="good">✓</b>'
        if score <= -1:
            return '<b class="warn">!</b>' if advisory else '<b class="bad">✗</b>'
        return ""

    def owner(self, change: dict) -> str:
        score = owner_self_review_score(change)
        return _esc(owner_text(change)) + (f" {self.vote(score)}" if score else "")

    def org(self, change: dict) -> str:
        owner = change.get("owner") or {}
        return self.orgs.lookup(owner.get("email") or "", owner.get("name") or "")

    @staticmethod
    def patch_set_date(change: dict) -> str:
        rev = (change.get("revisions") or {}).get(change.get("current_revision")) or {}
        return _date(rev.get("created") or change.get("updated") or "")

    @staticmethod
    def col_class(column: str) -> str:
        return "mid" if column in _CENTERED else "c-" + column.split("/")[0].lower()

    # ---- rows and tables ----

    def row(self, change: dict, blocked: bool = False) -> str:
        v = change["votes"]
        summary = _esc(summary_text(change))
        if blocked:
            summary += f' <b class="bad">(blocked — in: {_esc(change.get("bucket"))})</b>'
        cells = {
            "Change": self.change_link(change),
            "Chain": _esc(chain_text(change)),
            "Issue": self.issue(change),
            "Component": _esc(component_text(change)),
            "Summary": summary,
            "V": self.vote(v["v"]),
            "R": self.vote(v["r"]),
            "C": self.mark(v["c"]),
            "S": self.mark(v["s"], advisory=True),
            "D": self.mark(v["d"]),
            "Owner": self.owner(change),
            "Org": _esc(self.org(change)) if self.orgs else "",
            "Uploaded": _esc(_date(change.get("created"))),
            "Updated": _esc(_date(change.get("updated"))),
            "Type": _esc((change.get("_jira") or {}).get("type") or ""),
            "Topic/Hashtag": self.tags(change),
            "Comment": self.comment(change),
        }
        cls = "blocked" if blocked else ("is-blocker", "is-priority", "")[wanted_rank(change)]
        tds = "".join(f'<td class="{self.col_class(c)}">{cells[c]}</td>' for c in self.columns)
        return f'<tr class="{cls}" data-ps="{_esc(self.patch_set_date(change))}">{tds}</tr>'

    def table(self, rows: str) -> str:
        head = "".join(
            f'<th class="{self.col_class(c)}"><button type="button">{_esc(c)}</button></th>'
            for c in self.columns
        )
        return (
            '<div class="scroll"><table class="list"><thead><tr>' + head
            + "</tr></thead><tbody>" + rows + "</tbody></table></div>"
        )

    def wanted_list(self, changes: list[dict], rank: int, title: str) -> str:
        wanted = [ch for ch in changes if wanted_rank(ch) == rank]
        if not wanted:
            return ""
        items = "".join(
            f"<li>{self.change_link(ch)} {_esc(ch.get('subject'))} "
            f"<i>— {_esc(ch['bucket'])}: {self.comment(ch)}</i></li>"
            for ch in sorted(wanted, key=lambda ch: (BUCKET_ORDER.index(ch["bucket"]), ch["_number"]))
        )
        return f"<details><summary>{title} ({len(wanted)})</summary><ul>{items}</ul></details>"

    # ---- page ----

    def render(self, fetch_result: dict, classified: list[dict]) -> str:
        """The body: summary table, legend, release lists, search, buckets."""
        from portal.patch_status.rules import landing_branch

        branch = fetch_result.get("branch", "?")
        landing = landing_branch(branch)
        project = fetch_result.get("project", "?")
        head: Optional[str] = fetch_result.get("head")
        total = len(classified)

        out = [
            f'<p class="meta">project <code>{_esc(project)}</code> · branch <code>{_esc(branch)}</code>'
            f" · open changes: {total}"
            + (f" · HEAD {self.head_link(project, head)}" if head else "")
            + (f" · needs-rebase check: {_esc(fetch_result['rebase_check'])}" if fetch_result.get("rebase_check") else "")
            + "</p>",
        ]

        by_bucket = {b: [c for c in classified if c.get("bucket") == b] for b in BUCKET_ORDER}
        blocked = [
            ch for ch in classified
            if ch["votes"]["_review"].get("queued") and ch.get("bucket") not in READY_BUCKETS
        ]
        rows = "".join(
            f'<tr data-bucket="{bucket_anchor(b)}"><td><a href="#{bucket_anchor(b)}">{_esc(b)}</a></td>'
            f'<td class="num n">{len(m)}</td><td class="num">{100.0 * len(m) / total if total else 0:.1f}%</td>'
            + "".join(
                f'<td class="num">{sum(wanted_rank(ch) == r for ch in m) or ""}</td>' for r in (0, 1)
            ) + "</tr>"
            for b, m in by_bucket.items()
        )
        out.append(
            '<table class="sum"><thead><tr><th>Bucket</th><th>Count</th><th>% of total</th>'
            '<th><span class="badge blocker" title="release blockers">B</span></th>'
            '<th><span class="badge priority" title="release priority">P</span></th>'
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
        out.append(
            "<details><summary>Legend</summary><dl>"
            + "".join(
                f"<dt>{_esc(k)}</dt><dd>{_esc(t.format(landing=landing))}</dd>" for k, t in LEGEND
            )
            + "</dl></details>"
        )
        out.append(self.wanted_list(classified, 0, "Release blockers"))
        out.append(self.wanted_list(classified, 1, "Release priority"))
        out.append(
            '<form class="filter" role="search">'
            '<div class="box"><input type="search" placeholder="Search: owner, component, ticket, hashtag, comment, summary…"'
            ' aria-label="Search" role="combobox" aria-autocomplete="list" aria-expanded="false"'
            ' aria-controls="ps-suggest" autocomplete="off" spellcheck="false">'
            '<ul id="ps-suggest" role="listbox" hidden></ul></div>'
            '<label>patch set uploaded <select>'
            '<option value="">any time</option><option value="30">in the last 30 days</option>'
            '<option value="90">in the last 90 days</option><option value="365">in the last year</option>'
            '</select></label><span class="hits" aria-live="polite"></span></form>'
        )

        for b, members in by_bucket.items():
            extras = blocked if b == IN_LINE_FOR_LANDING else []
            note = f" + {len(extras)} blocked" if extras else ""
            out.append(
                f'<section id="{bucket_anchor(b)}"><h2>{_esc(b)} '
                f'(<span class="n">{len(members)}</span>{note})</h2>'
            )
            if members or extras:
                body = "".join(self.row(ch) for ch in sort_for_bucket(members))
                body += "".join(
                    self.row(ch, blocked=True) for ch in sorted(extras, key=lambda c: c["_number"])
                )
                out.append(self.table(body))
            else:
                out.append("<p><i>No changes in this bucket.</i></p>")
            out.append("</section>")

        return '<div class="ps">' + "\n".join(o for o in out if o) + "</div>"
