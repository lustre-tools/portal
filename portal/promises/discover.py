"""Follow-ups looked for when a promise is checked -- with the code at hand.

The rescan finds what a thread points at: links, tickets, the relation
chain. A check can look further, because it has the change's own diff:

* the files a promise is about: the file commented on, files the thread
  names, and files where the change's diff contains what the thread talks
  about -- ``always_except``, a function, a test. A comment on the commit
  message has no file of its own; this is where it gets one;
* tickets the change leaves in its code -- ``always_except LU-20566 41j``,
  ``/* LU-19999: ... */`` -- which is where deferred work gets parked;
* then Gerrit: changes touching those files since, and changes on those
  tickets. Neither is cut by creation date: an open follow-up nobody has
  touched since the promise was made is still the follow-up.

What is found is ranked by whether the candidate's own diff touches what
the promise is about; the check gets the best of it, with the diffs. A
deeper check searches back to when the change was created, takes more
files, keeps weaker matches and adds the change's topic and hashtags.

A check only runs for the public project, and every search here is
limited to the change's own project and branch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import normalize_path, ticket_re


@dataclass(frozen=True)
class Depth:
    name: str
    max_files: int  # files per promise to search
    per_ticket: int  # changes kept per ticket the change parks work under
    file_results: int  # changes kept per file search
    ticket_results: int  # changes kept per ticket search
    pool: int  # candidates one check is given
    unscored: int  # file matches kept whose diff shares nothing with the promise
    since_created: bool  # search from the change's creation, not the promise
    siblings: bool  # the change's topic and hashtags too
    stacked: int  # changes from the relation chain


DEPTHS = {
    "normal": Depth("normal", 3, 8, 50, 50, 30, 3, False, False, 10),
    "deep": Depth("deep", 8, 20, 100, 100, 80, 20, True, True, 40),
}


def depth_of(name: str | None) -> Depth:
    return DEPTHS.get(name or "normal", DEPTHS["normal"])


# ---------------------------------------------------------- what it is about

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\(\))?")
# A test of a Lustre suite: "41d", "53a", as in "sanity-ec 41d",
# "always_except LU-20708 41d" and "test_41d".
_TEST_ID = re.compile(r"(?<![A-Za-z0-9.])(\d{2,4}[a-z])(?![A-Za-z0-9])")
_SIZES = {str(2**n) for n in range(11)}
_SHOUTING = {
    "ACK", "AFAICT", "AFAIK", "API", "BTW", "FIXME", "FYI", "IIRC", "IMHO", "IMO",
    "LGTM", "MERGED", "NACK", "NEW", "NOTE", "TBD", "TODO", "WIP", "XXX",
}  # fmt: skip


def _test_id(word: str) -> bool:
    m = _TEST_ID.fullmatch(word)
    # "64k", "16m": a size, not a test.
    return bool(m) and not (word[-1] in "kmg" and word[:-1] in _SIZES)


def identifiers(text: str) -> set[str]:
    """The words in a thread that look like code -- names with an
    underscore, calls, camelCase, acronyms like AIO, test numbers like
    41d. They are what a follow-up's diff would contain."""
    out: set[str] = set()
    for m in _TOKEN.finditer(text or ""):
        raw = m.group(0)
        word = raw.rstrip("()")
        if len(word) < 3:
            continue
        if (
            "_" in word.strip("_")
            or raw.endswith("()")
            or re.search(r"[a-z][A-Z]", word)
            or (word.isupper() and word not in _SHOUTING and not word.isdigit())
        ):
            out.add(word)
    for m in _TEST_ID.finditer(text or ""):
        if _test_id(m.group(1)):
            out.add(m.group(1))
    return out


def weight(word: str) -> int:
    # A name or a test is a much better match than an acronym.
    return 1 if word.isupper() else 2


def _occurs(word: str, text: str) -> bool:
    if _test_id(word):
        # "41d" in "test_41d" and "41d 53a", not in "0x41dead".
        return re.search(rf"(?<![A-Za-z0-9]){word}(?![A-Za-z0-9])", text) is not None
    return word in text


def hits(idents: set[str], text: str) -> list[str]:
    """The identifiers that occur in ``text``, best first."""
    found = [w for w in idents if _occurs(w, text or "")]
    return sorted(found, key=lambda w: (-weight(w), w))


def score(idents: set[str], text: str) -> int:
    return sum(weight(w) for w in hits(idents, text))


def strong(words: list[str]) -> bool:
    """A real match has a name or a test in it; acronyms alone do not
    count -- "OST" and "FLR" are in half of Lustre."""
    return any(weight(w) > 1 for w in words)


def item_text(item) -> str:
    """Everything said about one promise, duplicates included."""
    parts = [item.summary or "", item.promise_quote or "", item.code_context or ""]
    for it in [item, *list(item.duplicates or [])]:
        parts.append((it.root or {}).get("message", ""))
        parts += [r.get("message", "") for r in it.replies or []]
    return "\n".join(p for p in parts if p)


def _real_path(path: str | None) -> bool:
    # "COMMIT_MSG" and "PATCHSET_LEVEL" have no directory.
    return bool(path) and "/" in path


def _named(path: str, text: str) -> bool:
    parts = path.split("/")
    for k in range(len(parts) - 1):
        # The whole path, or a tail of it with a directory: "llite/file.c".
        if re.search(rf"(?<![\w.-]){re.escape('/'.join(parts[k:]))}(?![\w-])", text):
            return True
    base = parts[-1]
    stem = base.rsplit(".", 1)[0]
    for word in (base, stem if ("-" in stem or "_" in stem) else None):
        if word and len(word) >= 5 and re.search(rf"(?<![\w./-]){re.escape(word)}(?![\w-])", text):
            return True
    return False


def files_for(item, diffs: dict[str, str], idents: set[str], depth: Depth) -> list[tuple[str, str]]:
    """(path, why) for the files a promise is about, most certain first."""
    text = item_text(item)
    out: dict[str, str] = {}
    for it in [item, *list(item.duplicates or [])]:
        path = normalize_path(it.file_path)
        if _real_path(path):
            out.setdefault(path, "commented on")
    for path in diffs:
        if path not in out and _named(path, text):
            out[path] = "named in the thread"
    ranked = sorted(
        ((score(idents, diff), path) for path, diff in diffs.items() if path not in out),
        key=lambda x: (-x[0], x[1]),
    )
    for _s, path in ranked:
        if len(out) >= depth.max_files:
            break
        words = hits(idents, diffs[path])
        if strong(words):
            out[path] = "the change's diff there has " + ", ".join(words[:3])
    if depth.name == "deep":
        # Nothing to go on: the biggest parts of the change.
        for path in sorted(diffs, key=lambda p: -len(diffs[p])):
            if len(out) >= depth.max_files:
                break
            out.setdefault(path, "changed by this change")
    return list(out.items())[: max(depth.max_files, 1)]


def code_tickets(diffs: dict[str, str], own: str, prefix: str) -> dict[str, list[tuple[str, str]]]:
    """Tickets the change writes into its code, with where: {ticket:
    [(path, line)]}. The change's own ticket is left out."""
    pattern = ticket_re(prefix)
    out: dict[str, list[tuple[str, str]]] = {}
    for path, diff in diffs.items():
        for line in diff.splitlines():
            if not line.startswith("+"):
                continue
            for m in pattern.finditer(line):
                if m.group(0) != own:
                    out.setdefault(m.group(0), []).append((path, line[1:].strip()[:160]))
    return out


def tickets_for(
    item, code: dict[str, list[tuple[str, str]]], idents: set[str], files: list[str], depth: Depth
) -> dict[str, str]:
    """{ticket: why} for the code tickets that belong to this promise:
    the line naming it shares a name or a test with the thread
    (``always_except LU-20708 41d`` for "fix 41d"). That the thread names
    the ticket is not enough -- an umbrella ticket is named everywhere;
    the tickets a reply promises are searched by the rescan anyway. A
    deeper check takes every ticket in the item's files."""
    out: dict[str, str] = {}
    for ticket, places in code.items():
        for path, line in places:
            if strong(hits(idents, line)):
                out[ticket] = f"{ticket}, left in {path}: {line[:70]}"
                break
            if depth.name == "deep" and path in files:
                out.setdefault(ticket, f"{ticket}, left in {path}: {line[:70]}")
    return out


# ------------------------------------------------------------------ search


def summarize(c: dict) -> dict:
    ps = None
    rev = c.get("current_revision")
    if rev and rev in (c.get("revisions") or {}):
        ps = c["revisions"][rev].get("_number")
    return {
        "number": c.get("_number"),
        "subject": c.get("subject", ""),
        "status": c.get("status", ""),
        "project": c.get("project", ""),
        "branch": c.get("branch", ""),
        "current_patchset": ps,
        "updated": c.get("updated", ""),
        "created": c.get("created", ""),
    }


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "").replace('"', "") + '"'


class Finder:
    """Gerrit searches for one check, each run once however many promises
    ask for it. A failed search finds nothing; it does not stop the check."""

    def __init__(self, client, *, project: str, branch: str, number: int):
        self.client = client
        self.scope = f"project:{_quote(project)} branch:{_quote(branch)} -change:{int(number)}"
        self._cache: dict[tuple[str, int], list[dict]] = {}
        self.queries = 0

    def search(self, query: str, max_results: int) -> list[dict]:
        key = (query, max_results)
        if key not in self._cache:
            self.queries += 1
            try:
                found = self.client.search_all(
                    f"{self.scope} -is:abandoned {query}",
                    max_results=max_results,
                    page_size=min(100, max_results),
                    options=["CURRENT_REVISION"],
                )
            except Exception:  # noqa: BLE001 - one failed search must not stop the check
                found = []
            self._cache[key] = [summarize(c) for c in found or [] if c.get("_number")]
        return self._cache[key]

    def touching(self, path: str, since: str, max_results: int) -> list[dict]:
        after = f" after:{since[:10]}" if since else ""
        return self.search(f"path:{_quote(path)}{after}", max_results)

    def on_ticket(self, ticket: str, since: str, max_results: int) -> list[dict]:
        after = f" after:{since[:10]}" if since else ""
        return self.search(f"message:{_quote(ticket)}{after}", max_results)

    def siblings(self, topic: str, hashtags: list[str], since: str, max_results: int) -> list[dict]:
        terms = ([f"topic:{_quote(topic)}"] if topic else []) + [
            f"hashtag:{_quote(h)}" for h in hashtags or []
        ]
        if not terms:
            return []
        after = f" after:{since[:10]}" if since else ""
        return self.search(f"({' OR '.join(terms)}){after}", max_results)
