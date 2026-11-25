"""Fetch the open changes of one branch for Patch Status.

Everything is read without logging in, so the page cannot show anything
that is not public already: Gerrit's REST API and Jira's search, with
``requests`` and nothing else. About 2400 requests for Lustre master,
eight at a time.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

import requests

from portal.patch_status import rules

PAGE_SIZE = 200
WORKERS = 8
# A few failed series lookups are noise; more mean the data is wrong.
MAX_RELATED_FAILURES = 0.02
MAX_STAGED = 400

SEARCH_OPTIONS = [
    "DETAILED_LABELS",
    "DETAILED_ACCOUNTS",
    "CURRENT_REVISION",
    "CURRENT_COMMIT",
    "MESSAGES",
    "SUBMITTABLE",
    "LABELS",
]

_CHANGE_ID_RE = re.compile(r"^Change-Id: (I[0-9a-f]{40})\s*$", re.MULTILINE)

_KEEP = (
    "_number", "subject", "project", "branch", "status", "change_id", "owner",
    "labels", "hashtags", "topic", "created", "updated", "current_revision",
    "work_in_progress", "unresolved_comment_count",
)

Progress = Callable[[str, int, Optional[int]], None]
Log = Callable[[str], None]


class FetchError(Exception):
    """The data is incomplete; nothing may be shown from it."""


class AnonymousGerrit:
    def __init__(self, base: str, workers: int = WORKERS):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=workers))
        # A ~/.netrc entry would silently turn this into a logged-in
        # session, and the page would show what that account can see.
        self.session.trust_env = False

    def get(self, path: str, params: Any = None) -> Any:
        error = ""
        for attempt in range(4):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                r = self.session.get(self.base + path, params=params, timeout=120)
            except requests.RequestException as e:
                error = str(e)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                error = f"HTTP {r.status_code}"
                continue
            if r.status_code != 200:
                raise FetchError(f"{path}: HTTP {r.status_code}")
            try:
                # Gerrit prefixes its JSON with a )]}' line.
                return json.loads(r.text.split("\n", 1)[1])
            except (IndexError, ValueError):
                error = "not a Gerrit JSON reply"
        raise FetchError(f"{path}: {error}")


def _project_path(project: str) -> str:
    return f"/projects/{project.replace('/', '%2F')}"


def extract_ticket(subject: str, prefix: str = "LU") -> Optional[str]:
    """The first ticket of the project's Jira in a commit subject."""
    m = re.search(rf"\b({re.escape(prefix)}-\d+)\b", subject or "")
    return m.group(1) if m else None


def current_revision_number(change: dict) -> Optional[int]:
    rev = (change.get("revisions") or {}).get(change.get("current_revision")) or {}
    n = rev.get("_number")
    return n if isinstance(n, int) else None


def trim_related(related: Optional[list[dict]]) -> Optional[list[dict]]:
    if related is None:
        return None
    return [
        {
            "_change_number": item.get("_change_number"),
            "status": item.get("status"),
            "_revision_number": item.get("_revision_number"),
        }
        for item in related
    ]


def is_rebase_vote(change: dict) -> bool:
    """wc-checkpatch holds a Code-Review -1 exactly on changes that cannot
    be cherry-picked; the label drops it on the next patch set."""
    entries = ((change.get("labels") or {}).get("Code-Review") or {}).get("all") or []
    return any(
        e.get("name") == rules.CHECKPATCH and (e.get("value") or 0) < 0
        for e in entries
    )


def trim(change: dict) -> dict:
    """Keep what classify and the page read: the raw records are about
    190 MB for master, nearly all of it bot messages."""
    out = {k: change[k] for k in _KEEP if k in change}
    sha = change.get("current_revision")
    rev = (change.get("revisions") or {}).get(sha) or {}
    out["revisions"] = {sha: {
        "_number": rev.get("_number"),
        "created": rev.get("created"),
        "commit": {"message": (rev.get("commit") or {}).get("message") or ""},
    }}
    out["labels"] = {
        name: {"all": [e for e in (body.get("all") or []) if e.get("value")]}
        for name, body in (change.get("labels") or {}).items()
    }
    out["messages"] = [
        m for m in change.get("messages") or []
        if (m.get("author") or {}).get("name") in (rules.CHECKPATCH, rules.MALOO)
        and m.get("_revision_number") == rev.get("_number")
        # or it would hide the style result posted before it
        and "cannot be cherry-picked" not in (m.get("message") or "")
    ]
    return out


def search_open(gerrit: AnonymousGerrit, project: str, branch: str, limit: Optional[int],
                progress: Progress) -> list[dict]:
    out: dict[int, dict] = {}
    start = 0
    while True:
        page = gerrit.get("/changes/", params=[
            ("q", f"project:{project} branch:{branch} status:open -is:private"),
            ("n", PAGE_SIZE), ("S", start),
            *(("o", o) for o in SEARCH_OPTIONS),
        ])
        for ch in page:
            if ch.get("is_private"):
                continue
            # A change updated while paging shows up on two pages.
            out[ch["_number"]] = trim(ch)
        progress("changes", len(out), None)
        if not page or not page[-1].get("_more_changes"):
            break
        if limit and len(out) >= limit:
            break
        start += len(page)
    changes = list(out.values())
    return changes[:limit] if limit else changes


def _dependees_ready(number: int, related: Optional[list[dict]]) -> Optional[bool]:
    """Everything below this change in its series merged? None: unknown."""
    if related is None:
        return None
    if not related:
        return True
    self_idx = next((i for i, r in enumerate(related) if r.get("_change_number") == number), None)
    if self_idx is None:
        return None
    return all(item.get("status") == "MERGED" for item in related[self_idx + 1:])


def add_related(gerrit: AnonymousGerrit, changes: list[dict], workers: int,
                progress: Progress, log: Log) -> None:
    def fetch(ch: dict) -> list[dict]:
        resp = gerrit.get(f"/changes/{ch['_number']}/revisions/current/related")
        return resp.get("changes") or []

    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, ch): ch for ch in changes}
        for done, fut in enumerate(as_completed(futures), 1):
            ch = futures[fut]
            try:
                related: Optional[list[dict]] = fut.result()
            except FetchError as e:
                log(f"related: {e}")
                related = None
                failed += 1
            ch["_related"] = trim_related(related)
            ch["dependees_ready"] = _dependees_ready(ch["_number"], related)
            progress("series", done, len(changes))
    if failed > len(changes) * MAX_RELATED_FAILURES:
        raise FetchError(f"series lookups failed for {failed} of {len(changes)} changes")


def fetch_staged(gerrit: AnonymousGerrit, project: str, branch: str) -> set[str]:
    """Change-Ids of the changes on the landing branch.

    The branch starts with commits that are not Gerrit changes and so have
    no Change-Id; below them is the branch itself.
    """
    out: set[str] = set()
    path = _project_path(project)
    sha = gerrit.get(f"{path}/branches/{branch}")["revision"]
    for _ in range(MAX_STAGED):
        commit = gerrit.get(f"{path}/commits/{sha}")
        ids = _CHANGE_ID_RE.findall(commit["message"])
        if not ids:
            return out
        out.add(ids[-1])
        sha = commit["parents"][0]["commit"]
    raise FetchError(f"{branch}: no base commit in {MAX_STAGED} commits")


def mark_staged(changes: list[dict], staged: Future, landing: str, log: Log) -> None:
    try:
        ids = staged.result()
    except (FetchError, KeyError, IndexError) as e:
        log(f"{landing}: {e}; using its hashtag instead")
        return
    for ch in changes:
        ch["_staged"] = ch.get("change_id") in ids
    log(f"{landing}: {sum(ch['_staged'] for ch in changes)} open changes")


def fetch_ticket_types(jira_url: str, tickets: set[str],
                       progress: Progress, log: Log) -> dict[str, dict]:
    """Issue types, 100 per search. The column stays blank when Jira is
    down."""
    keys = sorted(tickets)
    out: dict[str, dict] = {}
    session = requests.Session()
    session.trust_env = False
    batches = range(0, len(keys), 100)
    for done, i in enumerate(batches, 1):
        try:
            r = session.get(f"{jira_url.rstrip('/')}/rest/api/2/search", timeout=60, params={
                "jql": f"key in ({','.join(keys[i:i + 100])})",
                "fields": "issuetype", "maxResults": 100,
                # unknown keys are skipped instead of failing the batch
                "validateQuery": "false",
            })
            r.raise_for_status()
            for issue in r.json().get("issues") or []:
                out[issue["key"]] = {"type": issue["fields"]["issuetype"]["name"]}
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            log(f"Jira batch failed: {e}")
        progress("Jira", done, len(batches))
    log(f"Jira: {len(out)} of {len(keys)} tickets")
    return out


def fetch(
    *,
    gerrit_url: str,
    jira_url: str,
    project: str,
    branch: str,
    ticket_prefix: str = "LU",
    limit: Optional[int] = None,
    skip_jira: bool = False,
    workers: int = WORKERS,
    progress: Optional[Progress] = None,
    log: Optional[Log] = None,
) -> dict:
    """Every open change of project/branch, ready for classify()."""
    progress = progress or (lambda *_: None)
    log = log or (lambda _: None)
    gerrit = AnonymousGerrit(gerrit_url, workers=workers)
    landing = rules.landing_branch(branch)
    log(f"searching {project} branch:{branch} status:open (anonymous)")
    head = gerrit.get(f"{_project_path(project)}/branches/{branch}")
    with ThreadPoolExecutor(max_workers=1) as pool:
        staged = pool.submit(fetch_staged, AnonymousGerrit(gerrit_url), project, landing)
        changes = search_open(gerrit, project, branch, limit, progress)
        if not changes:
            raise FetchError("Gerrit returned no open changes")
        add_related(gerrit, changes, workers, progress, log)
        mark_staged(changes, staged, landing, log)

    for ch in changes:
        ch["current_revision_number"] = current_revision_number(ch)
        ch["_jira_ticket"] = extract_ticket(ch.get("subject") or "", ticket_prefix)
        ch["mergeable"] = not is_rebase_vote(ch)
    types = {} if skip_jira else fetch_ticket_types(
        jira_url, {ch["_jira_ticket"] for ch in changes if ch["_jira_ticket"]},
        progress, log)
    for ch in changes:
        ch["_jira"] = types.get(ch["_jira_ticket"], {})

    return {
        "branch": branch,
        "project": project,
        "head": head.get("revision"),
        "fetched_at": time.time(),
        "changes": changes,
    }
