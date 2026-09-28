"""Follow-up changes the portal can find on its own -- no AI involved.

A promise often names where it will be kept: "I will create a patch in
LU-19999", "filed LU-20566 for that". And a Lustre follow-up that fixes
a landed change says so in a ``Fixes: <sha> ("<subject>")`` trailer. Both
are plain Gerrit searches:

* every Gerrit change linked in a thread ("Addressed in .../+/68697");
* every ticket a promise names ("filed LU-20566", "a patch in LU-19999")
  -> changes whose commit message names it, touched since the change was
  created (Gerrit's ``after:`` is "modified after"; it narrows the search
  on the server, so a busy ticket is not cut to its newest few);
* once the change is merged -> later changes whose message cites its commit.

Abandoned changes are left out. What a check looks at beyond this --
the files a promise is about, the tickets a change leaves in its code --
is in :mod:`.discover`.

Results go into the change document's ``candidates`` section and are
shown next to the promise; the judge is told about them too, so it
checks a short list instead of searching.

A public change is searched within the public project only, so a public
page can never list an internal change.
"""

from __future__ import annotations

from .discover import summarize as _summarize
from .models import now_iso
from .status import own_ticket, promise_tickets

#: Most tickets searched per change, and results kept per search (Gerrit's
#: pages are followed up to that).
MAX_TICKETS = 20
MAX_RESULTS = 100


def _search(client, query: str) -> list[dict]:
    try:
        found = client.search_all(
            query, max_results=MAX_RESULTS, page_size=MAX_RESULTS, options=["CURRENT_REVISION"]
        )
    except Exception:  # noqa: BLE001 - one failed search must not stop the rescan
        return []
    return [_summarize(c) for c in found or [] if c.get("_number")]


def change_facts(client, number: int) -> dict:
    """When the change was created, and its topic and hashtags -- what
    the searches here and in a check are scoped by. Empty on failure."""
    try:
        c = client.get_change(int(number))
    except Exception:  # noqa: BLE001 - the searches just go unscoped
        return {}
    return {
        "created": c.get("created", ""),
        "topic": c.get("topic", ""),
        "hashtags": list(c.get("hashtags") or []),
    }


def find_candidates(client, change_doc: dict, *, public_project: str, ticket_prefix: str) -> dict:
    """Search Gerrit for follow-ups of one change. Returns the new
    ``candidates`` section."""
    harvest = change_doc.get("harvest") or {}
    change = harvest.get("change") or {}
    number = change_doc.get("change_number")
    scope = f" project:{public_project}" if change.get("project") == public_project else ""
    facts = change_facts(client, number)
    after = f" after:{facts['created'][:10]}" if facts.get("created") else ""

    tickets: list[str] = []
    mine = own_ticket(change_doc, ticket_prefix)
    if mine:
        tickets.append(mine)
    for t in harvest.get("threads") or []:
        if t.get("mechanical"):
            continue
        for ticket in promise_tickets(t, ticket_prefix):
            if ticket not in tickets:
                tickets.append(ticket)

    by_ticket = {}
    for ticket in tickets[:MAX_TICKETS]:
        by_ticket[ticket] = _search(
            client, f'message:"{ticket}" -change:{number} -is:abandoned{scope}{after}'
        )

    links: list[int] = []
    for t in harvest.get("threads") or []:
        if not t.get("mechanical"):
            links += [n for n in t.get("links") or [] if n not in links]
    by_link = {}
    for num in links[:MAX_TICKETS]:
        try:
            info = _summarize(client.get_change(num, options=["CURRENT_REVISION"]))
        except Exception:  # noqa: BLE001 - a dead link is just not a candidate
            continue
        if scope and info.get("project") != public_project:
            continue  # a public page never shows an internal change
        by_link[str(num)] = info

    stacked = stacked_on(client, number)

    fixes = []
    sha = change.get("current_revision") or ""
    if change.get("status") == "MERGED" and len(sha) >= 12:
        fixes = _search(client, f'message:"{sha[:12]}" -change:{number} -is:abandoned{scope}')

    return {
        "found_at": now_iso(),
        "change": facts,
        "own_ticket": mine,
        "by_ticket": by_ticket,
        "by_link": by_link,
        "stacked": stacked,
        "fixes": fixes,
    }


#: Most changes kept from the relation chain.
MAX_STACKED = 40


def stacked_on(client, number: int, sha: str = "") -> list[dict]:
    """Changes stacked on top of this one in Gerrit's relation chain --
    where "fixed in the next patch" usually lands.

    Gerrit lists the chain from the top down: the changes above this one,
    then this one, then what it is based on. Following parent commits
    instead misses most of them -- a child is based on an older patchset,
    or was rebased onto the commit the change merged as -- so the order
    is what decides. Abandoned changes are left out. A relation chain
    never leaves its project, so this is as public as the change itself.
    """
    try:
        chain = client.get_related_changes(number) or []
    except Exception:  # noqa: BLE001 - no chain is not an error
        return []
    numbers = [r.get("_change_number") for r in chain]
    if number not in numbers:
        return []
    out = []
    for r in chain[: numbers.index(number)]:
        if r.get("status") == "ABANDONED":
            continue
        out.append(
            {
                "number": r.get("_change_number"),
                "subject": (r.get("commit") or {}).get("subject") or r.get("subject", ""),
                "status": r.get("status", ""),
                "current_patchset": r.get("_current_revision_number") or r.get("_revision_number"),
            }
        )
    return out[:MAX_STACKED]


def fetch_linked_changes(client, numbers: list[int]) -> dict[str, dict]:
    """Status of the follow-up changes linked to items by hand."""
    out: dict[str, dict] = {}
    for num in sorted({int(n) for n in numbers}):
        try:
            detail = client.get_change(num, options=["CURRENT_REVISION"])
        except Exception as exc:  # noqa: BLE001 - one bad link must not stop the rescan
            out[str(num)] = {
                "subject": "",
                "status": "",
                "error": str(exc)[:200],
                "fetched_at": now_iso(),
            }
            continue
        info = _summarize(detail)
        info["fetched_at"] = now_iso()
        out[str(num)] = info
    return out
