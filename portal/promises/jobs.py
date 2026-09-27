"""The pipelines, and the registry that runs them in the background.

The pipelines are plain synchronous functions -- the CLI calls them
directly; the web app runs them through :class:`JobRegistry`, one job per
change at a time. Three of them:

* :func:`run_rescan` -- harvest, follow-up search, linked changes. Free.
* :func:`run_classify` -- find the promises (Claude, batched, no tools).
* :func:`run_judge` -- check them against the code (Claude, per item).

A Claude stage stops starting calls when the usage limit is reached and
keeps everything done so far; a failure never overwrites a result that
was usable.
"""

from __future__ import annotations

import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import claude, followups, harvest, judge, repo, status
from .models import now_iso
from .store import Store


@dataclass
class Settings:
    """Everything a pipeline needs, taken from the app config once."""

    root: Path  # the promises directory
    gerrit_url: str
    public_project: str
    ticket_prefix: str
    mechanical: tuple = harvest.DEFAULT_MECHANICAL
    classify_batch: int = 60
    claude: judge.ClaudeSettings = field(default=None)

    @classmethod
    def from_config(cls, config) -> Settings:
        root = Path(config["PROMISES_DIR"])
        return cls(
            root=root,
            gerrit_url=config["GERRIT_URL"],
            public_project=config["PUBLIC_PROJECT"],
            ticket_prefix=config["TICKET_PREFIX"],
            mechanical=tuple(config["PROMISES_MECHANICAL"]),
            classify_batch=config["PROMISES_CLASSIFY_BATCH"],
            claude=judge.ClaudeSettings(
                binary=claude.find_binary(config.get("CLAUDE_BINARY")),
                home=str(root / "claude-home"),
                neutral_dir=str(root / "claude-cwd"),
                classify_model=config["CLAUDE_CLASSIFY_MODEL"],
                judge_model=config["CLAUDE_JUDGE_MODEL"],
                effort=config["CLAUDE_EFFORT"],
                classify_budget_usd=config["CLAUDE_CLASSIFY_BUDGET_USD"],
                judge_budget_usd=config["CLAUDE_JUDGE_BUDGET_USD"],
                classify_timeout=config["CLAUDE_CLASSIFY_TIMEOUT"],
                judge_timeout=config["CLAUDE_JUDGE_TIMEOUT"],
                parallel=config["CLAUDE_PARALLEL"],
                min_free_mb=config["CLAUDE_MIN_FREE_MB"],
            ),
        )

    def gerrit(self):
        from gerrit_cli.client import GerritCommentsClient

        return GerritCommentsClient(self.gerrit_url)


def judge_unsupported(settings: Settings, change_doc: dict) -> str | None:
    """Why a change's promises cannot be checked against code, or None.

    Only the public project's master is supported: the judge compares the
    change with master, and other branches -- and anything internal -- are
    out of scope for now.
    """
    change = (change_doc.get("harvest") or {}).get("change") or {}
    if change.get("project") != settings.public_project:
        return f"checking is only supported for {settings.public_project}"
    if change.get("branch") != "master":
        return "checking is only supported for changes on master"
    return None


# ------------------------------------------------------------ rescan


def run_rescan(
    settings: Settings,
    store: Store,
    number: int,
    progress=None,
    added_by: str | None = None,
    cancel=None,
) -> dict:
    """Harvest, look for follow-ups, refresh linked changes. No AI."""
    report = progress or (lambda *_: None)
    report("harvest")
    got = harvest.harvest_change(settings.gerrit_url, number, settings.mechanical)

    def apply(doc: dict) -> None:
        doc["change_number"] = got["change_number"]
        doc["added_at"] = doc.get("added_at") or now_iso()
        if added_by and not doc.get("added_by"):
            doc["added_by"] = added_by
        doc["harvest"] = got["harvest"]

    doc = store.mutate_change(number, apply)

    client = settings.gerrit()
    report("follow-ups", "searching tickets")
    found = followups.find_candidates(
        client, doc, public_project=settings.public_project, ticket_prefix=settings.ticket_prefix
    )
    doc = store.mutate_change(number, lambda d: d.update(candidates=found), create=False)

    overrides = store.load_overrides(number)
    linked = [
        v["followup_change"]
        for v in list(overrides.get("overrides", {}).values())
        + list(overrides.get("manual_items", {}).values())
        if v.get("followup_change")
    ]
    if linked:
        report("links", f"{len(set(linked))} linked")
        info = followups.fetch_linked_changes(client, linked)
        doc = store.mutate_change(number, lambda d: d.update(linked_changes=info), create=False)
    return doc


def run_add(
    settings: Settings,
    store: Store,
    number: int,
    progress=None,
    added_by: str | None = None,
    cancel=None,
) -> dict:
    """Track a change: restore it from the archive if it was tracked
    before, then rescan."""
    if not store.has_change(number):
        store.unarchive_change(number)
    return run_rescan(settings, store, number, progress=progress, added_by=added_by)


# ---------------------------------------------------------- classify


def _need_claude(settings: Settings) -> None:
    if not settings.claude or not settings.claude.available:
        raise RuntimeError("Claude is not configured on this server")


def _parallel(settings: Settings, work: list, fn, cancel: threading.Event) -> None:
    """Run ``fn(x)`` for every x, up to the configured number at a time
    (the claude gate further limits them to what memory allows).

    The first QuotaReached stops everything: ``cancel`` is set so nothing
    new starts and running calls are killed, then it is raised. A
    Cancelled from a call is the expected result of that and is dropped.
    """
    stop, failed = [], []

    def one(x):
        if cancel.is_set():
            return
        try:
            fn(x)
        except claude.Cancelled:
            pass
        except claude.QuotaReached as exc:
            stop.append(exc)
            cancel.set()
        except claude.ClaudeError as exc:
            # One failed call: the others are still worth their result.
            failed.append(exc)
        except Exception as exc:  # noqa: BLE001 - a bug: stop spending, then report it
            stop.append(exc)
            cancel.set()

    workers = max(1, min(settings.claude.parallel, len(work)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(one, x) for x in work]:
            future.result()
    if stop:
        raise stop[0]
    if failed:
        raise claude.ClaudeError(
            f"{len(failed)} of {len(work)} failed; the rest is kept. First: {failed[0]}"
        )


def run_classify(settings: Settings, store: Store, number: int, progress=None, cancel=None) -> dict:
    """Find the promises among the threads not classified yet. Batches run
    in parallel and each is saved as soon as it is done."""
    _need_claude(settings)
    report = progress or (lambda *_: None)
    cancel = cancel or threading.Event()
    Path(settings.claude.neutral_dir).mkdir(parents=True, exist_ok=True)
    doc = store.load_change(number)
    threads = (doc.get("harvest") or {}).get("threads") or []
    if not threads:
        raise RuntimeError("nothing harvested yet: rescan first")
    todo = [
        t
        for t in threads
        if not t.get("mechanical")
        and status.classification_is_stale(
            (doc.get("classifications") or {}).get(t["id"]), t.get("text_hash", "")
        )
    ]
    subject = ((doc.get("harvest") or {}).get("change") or {}).get("subject", "")
    batch = max(1, settings.classify_batch)
    chunks = [todo[i : i + batch] for i in range(0, len(todo), batch)]
    done = [0]
    lock = threading.Lock()

    def one(chunk):
        new = judge.classify_threads(
            settings.claude,
            subject,
            chunk,
            on_call=lambda res: store.record_spend(number, "classify", res.usage()),
            cancel=cancel,
        )
        store.mutate_change(
            number, lambda d: d.setdefault("classifications", {}).update(new), create=False
        )
        with lock:
            done[0] += len(chunk)
            report("find promises", f"{done[0]}/{len(todo)} threads")

    report("find promises", f"0/{len(todo)} threads")
    _parallel(settings, chunks, one, cancel)
    if cancel.is_set():
        raise claude.Cancelled("cancelled; what was done is kept")
    return run_group(settings, store, number, progress=report, cancel=cancel)


def run_group(settings: Settings, store: Store, number: int, progress=None, cancel=None) -> dict:
    """Group duplicate promises -- one call over all of them -- when the
    set of promises changed since the last grouping."""
    from .models import GROUP_PROMPT_VERSION, now_iso

    report = progress or (lambda *_: None)
    doc = store.load_change(number)
    items, _ = status.build_items(
        doc, store.load_overrides(number), settings.claude.judge_model, settings.ticket_prefix
    )
    threads = [it for it in items if it.origin == "thread"]
    if not status.groups_stale(doc, items):
        return doc
    report("grouping duplicates", f"{len(threads)} promises")
    groups = judge.group_duplicates(
        settings.claude,
        doc,
        threads,
        on_call=lambda res: store.record_spend(number, "group", res.usage()),
        cancel=cancel,
    )
    entry = {
        "prompt_version": GROUP_PROMPT_VERSION,
        "model": settings.claude.classify_model,
        "made_at": now_iso(),
        "input_hash": status.group_input_hash(threads),
        "groups": groups,
    }
    return store.mutate_change(number, lambda d: d.update(groups=entry), create=False)


# ------------------------------------------------------------- judge

_SK = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")


def _scrub(value, workdir: Path, roots: tuple):
    """Keep server paths and anything token-shaped out of stored text."""
    if isinstance(value, dict):
        return {k: _scrub(v, workdir, roots) for k, v in value.items()}
    if not isinstance(value, str):
        return value
    text = value.replace(str(workdir) + "/", "").replace(str(workdir), ".")
    for root in roots:
        if root:
            text = text.replace(str(root), "<data>")
    return _SK.sub("[redacted]", text)


def _prepare_workspace(settings: Settings, doc: dict, workdir: Path, report) -> tuple[Path, dict]:
    """Mirror, fetch, and unpack both trees. Returns (mirror, pins)."""
    change = (doc.get("harvest") or {}).get("change") or {}
    number = doc["change_number"]
    ps = change.get("current_patchset") or 0
    mirror = repo.ensure_mirror(
        settings.root / "repos", settings.gerrit_url, settings.public_project
    )
    report("code", "fetching (the first time takes several minutes)")
    repo.fetch(mirror, number, ps)
    change_sha = repo.rev_parse(mirror, f"refs/promises/{number}-{ps}")
    master_sha = repo.rev_parse(mirror, "refs/heads/master")
    report("code", "unpacking")
    repo.snapshot(mirror, change_sha, workdir / "change")
    repo.snapshot(mirror, master_sha, workdir / "master")
    for sub in ("history", "tickets", "candidates"):
        (workdir / sub).mkdir(exist_ok=True)
    return mirror, {"change_sha": change_sha, "master_sha": master_sha}


#: Most possible follow-ups one check is told about (and given diffs of).
MAX_JUDGE_CANDIDATES = 30


def judge_candidates(
    item, doc: dict, ticket_prefix: str, limit: int = MAX_JUDGE_CANDIDATES
) -> list:
    """Every change the check should consider as a follow-up, most likely
    first:

    1. the item's own: linked in its thread, or on a ticket it names;
    2. changes stacked on top of this one -- "fixed in the next patch";
    3. changes on a ticket promised anywhere in this change -- the thread
       asking for the work is often not the one naming where it goes;
    4. other changes on this change's own ticket, updated since.

    Only 1 is listed under the item on the page. The check gets them all,
    with their diffs, and says which one -- if any -- does what was
    promised.
    """
    from .models import Followup

    cands = doc.get("candidates") or {}
    own = cands.get("own_ticket") or ""
    since = ((item.root or {}).get("updated") or item.updated or "").replace("T", " ")[:19]
    out = list(item.candidates)
    seen = {c.number for c in out} | {doc.get("change_number")}

    def add(c: dict, reason: str) -> None:
        num = c.get("number")
        if not isinstance(num, int) or num in seen or c.get("status") == "ABANDONED":
            return
        seen.add(num)
        out.append(
            Followup(
                number=num,
                status=c.get("status", ""),
                subject=c.get("subject", ""),
                current_patchset=c.get("current_patchset"),
                reason=reason,
            )
        )

    for c in cands.get("stacked") or []:
        add(c, "stacked on this change")
    promised = []
    for t in (doc.get("harvest") or {}).get("threads") or []:
        if not t.get("mechanical"):
            promised += [
                x
                for x in status.promise_tickets(t, ticket_prefix)
                if x not in promised and x != own
            ]
    for ticket in promised:
        for c in (cands.get("by_ticket") or {}).get(ticket, []):
            if (c.get("created") or since)[:19] >= since:
                add(c, f"promised in {ticket}")
    for c in (cands.get("by_ticket") or {}).get(own, []):
        if (c.get("updated") or "")[:19] >= since:
            add(c, f"same ticket {own}")
    return out[:limit]


def _item_files(
    mirror: Path, workdir: Path, item, pins: dict, changed: list[str], candidates: list
) -> dict:
    """Write what one item's check reads; return the relative names.
    Runs before the parallel phase: fetches into one mirror must not race."""
    since = ((item.root or {}).get("updated") or item.updated or "")[:10] or "1 year ago"
    paths = [item.file_path] if item.file_path and "/" in (item.file_path or "") else changed
    hist = f"history/{repo.safe_name(item.tid)}.txt"
    (workdir / hist).write_text(repo.history(mirror, pins["master_sha"], since, paths))
    files = {"history": hist, "tickets": [], "candidates": []}
    for ticket in item.tickets[:5]:
        rel = f"tickets/{repo.safe_name(ticket)}.txt"
        if not (workdir / rel).exists():
            (workdir / rel).write_text(repo.ticket_log(mirror, pins["master_sha"], ticket))
        files["tickets"].append(rel)
    for c in candidates:
        rel = f"candidates/{c.number}.diff"
        if not (workdir / rel).exists() and c.current_patchset:
            try:
                repo.fetch(mirror, c.number, c.current_patchset, timeout=600)
                sha = repo.rev_parse(mirror, f"refs/promises/{c.number}-{c.current_patchset}")
                (workdir / rel).write_text(
                    f"Change {c.number} ({c.status}): {c.subject}\n\n" + repo.show(mirror, sha)
                )
            except repo.RepoError:
                continue
        if (workdir / rel).exists():
            files["candidates"].append(rel)
    return files


def run_judge(
    settings: Settings,
    store: Store,
    number: int,
    scope: str = "pending",
    tid: str | None = None,
    progress=None,
    cancel=None,
) -> dict:
    """Check the items of one change against the code, several at a time.

    scope "pending": items without a fresh verdict. "open": also re-check
    the ones currently judged open. ``tid``: just that one item.
    """
    _need_claude(settings)
    report = progress or (lambda *_: None)
    cancel = cancel or threading.Event()
    doc = store.load_change(number)
    if not (doc.get("harvest") or {}).get("threads"):
        raise RuntimeError("nothing harvested yet: rescan first")
    why = judge_unsupported(settings, doc)
    if why:
        raise RuntimeError(why)

    overrides = store.load_overrides(number)
    cs = settings.claude
    items, _ = status.build_items(doc, overrides, cs.judge_model, settings.ticket_prefix)
    by_tid = {it.tid: it for it in items if it.origin == "thread"}
    if tid:
        tids = [tid] if tid in by_tid else []
    else:
        tids = status.pending_and_stale_tids(doc, overrides, cs.judge_model, settings.ticket_prefix)
        if scope == "open":
            tids += [
                t
                for t, it in by_tid.items()
                if it.effective_status == "open" and not it.manual_status and t not in tids
            ]
    if not tids:
        return doc

    repo.sweep(settings.root / "work")
    workdir = settings.root / "work" / f"{number}-{int(time.time())}"
    workdir.mkdir(parents=True, exist_ok=True)
    repo.claim(workdir)
    threads = {t["id"]: t for t in (doc.get("harvest") or {}).get("threads") or []}
    previous = doc.get("adjudications") or {}
    try:
        mirror, pins = _prepare_workspace(settings, doc, workdir, report)
        changed = repo.files_changed(mirror, pins["master_sha"], pins["change_sha"])
        plan = []
        for i, one in enumerate(tids, 1):
            if cancel.is_set():
                raise claude.Cancelled("cancelled; what was done is kept")
            report("preparing", f"{i}/{len(tids)}")
            item = by_tid[one]
            candidates = judge_candidates(item, doc, settings.ticket_prefix)
            plan.append(
                (item, candidates, _item_files(mirror, workdir, item, pins, changed, candidates))
            )

        done = [0]
        lock = threading.Lock()

        def check(entry):
            item, candidates, files = entry
            # Re-read the decisions: an item settled by hand while the batch
            # runs must not cost a call.
            ov = store.load_overrides(number).get("overrides", {}).get(item.tid) or {}
            if not tid and ov.get("status") in status.DECISIONS:
                return
            result = judge.adjudicate(
                cs,
                doc,
                item,
                threads.get(item.tid, {}).get("text_hash", ""),
                pins,
                workdir,
                files,
                on_call=lambda res: store.record_spend(number, "judge", res.usage()),
                cancel=cancel,
                candidates=candidates,
            )
            result = _scrub(result, workdir, (settings.root,))
            old = previous.get(item.tid)
            if not (result.get("raw_error") and old and not old.get("raw_error")):
                # Never replace a usable verdict with a failure.
                store.mutate_change(
                    number,
                    lambda d: d.setdefault("adjudications", {}).update({item.tid: result}),
                    create=False,
                )
            with lock:
                done[0] += 1
                report(
                    "check promises", f"{done[0]}/{len(plan)} done, {claude.GATE.running} running"
                )

        report("check promises", f"0/{len(plan)} done")
        _parallel(settings, plan, check, cancel)
        if cancel.is_set():
            raise claude.Cancelled("cancelled; what was done is kept")
    finally:
        repo.remove(workdir)
    return store.load_change(number)


# ---------------------------------------------------------- registry


class JobRegistry:
    """Background jobs in this process, at most one per change. A job can be
    cancelled: its pipeline is handed an Event that stops new calls and
    kills the running ones."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[int, dict] = {}
        self._cancels: dict[int, threading.Event] = {}

    def start(self, number: int, name: str, fn) -> bool:
        """Run ``fn(progress, cancel)`` in the background; False if a job for
        this change is already running."""
        with self._lock:
            job = self._jobs.get(number)
            if job and job["state"] == "running":
                return False
            job = {
                "state": "running",
                "name": name,
                "phase": "starting",
                "detail": "",
                "started_at": now_iso(),
                "finished_at": None,
                "error": None,
                "cancelling": False,
            }
            cancel = threading.Event()
            self._jobs[number] = job
            self._cancels[number] = cancel

        def progress(phase: str, detail: str = "") -> None:
            job["phase"], job["detail"] = phase, detail

        def worker() -> None:
            try:
                fn(progress, cancel)
                job["state"] = "cancelled" if cancel.is_set() else "done"
            except claude.Cancelled:
                job["state"] = "cancelled"
            except claude.QuotaReached as exc:
                job["state"] = "error"
                job["error"] = f"{exc}; what was done so far is kept"
            except Exception as exc:  # noqa: BLE001 - shown on the page
                job["state"] = "cancelled" if cancel.is_set() else "error"
                job["error"] = None if cancel.is_set() else str(exc)[:300]
                if not cancel.is_set():
                    traceback.print_exc()
            finally:
                job["finished_at"] = now_iso()

        threading.Thread(target=worker, name=f"promises-{name}-{number}", daemon=True).start()
        return True

    def cancel(self, number: int) -> bool:
        """Ask a running job to stop. False if nothing is running."""
        with self._lock:
            job = self._jobs.get(number)
            if not job or job["state"] != "running":
                return False
            job["cancelling"] = True
            job["phase"] = "cancelling"
            self._cancels[number].set()
            return True

    def running(self, number: int) -> bool:
        with self._lock:
            job = self._jobs.get(number)
            return bool(job and job["state"] == "running")

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {str(n): dict(j) for n, j in self._jobs.items()}
