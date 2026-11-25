"""Trial cherry-picks: does a change still apply to the branch today?

wc-checkpatch votes -1 when a new patch set cannot be cherry-picked, but
it does not come back when the branch moves on, and a vote can be taken
off by hand. So for the changes that pass every other check -- the ones a
needed rebase would hold back from landing -- the run tries it itself:
the change's series is cherry-picked onto today's branch in order, from
its first open change up to the change, with ``git merge-tree`` (no
checkout). That is how the community wiki's Patch Status decides its C
column; on 2026-11-25 the two agreed on 176 of 178 changes. When a change
below fails, the change records which one (the Comment column names it).

The repository is the run's own (DIR/repo.git). With a local clone of the
project configured (GRAPH_CONFLICTS_REPO) it borrows that clone's objects
and never writes into it, so the first fetch is small. One git fetch per
run brings every patch set needed; later runs only fetch what changed.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional

from portal.patch_status import classify as rules

FETCH_TIMEOUT = 900
GIT_TIMEOUT = 60


class TrialError(Exception):
    """The picks could not be tried; the votes stand in."""


def candidates(changes: list[dict]) -> list[dict]:
    """The changes to try: not test-only, CI green, two reviews, no
    negative vote -- whatever else they wait for."""
    out = []
    for ch in changes:
        if rules.is_test_only(ch):
            continue
        v, r = rules.verified_state(ch), rules.review_state(ch)
        if v["pass"] and r["pass"] and not v["has_any_negative"] and not r["has_any_negative"]:
            out.append(ch)
    return out


def series(change: dict, by_number: dict[int, dict]) -> list[tuple[int, int]]:
    """(change, patch set) from the first open change below this one up to
    this one, in landing order. Merged ones are on the branch already. A
    change below counts with its current patch set -- the one that lands --
    not the older one this change may still sit on."""
    related = change.get("_related") or []
    n = change["_number"]
    idx = next((i for i, r in enumerate(related) if r.get("_change_number") == n), None)
    below = []
    for r in [] if idx is None else related[idx + 1:]:
        m = r.get("_change_number")
        if r.get("status") != "NEW" or not m:
            continue
        ps = (by_number.get(m) or {}).get("current_revision_number") or r.get("_revision_number")
        if ps:
            below.append((m, ps))
    return list(reversed(below)) + [(n, change.get("current_revision_number"))]


class Repo:
    def __init__(self, path: str, borrow_from: Optional[str] = None):
        self.path = path
        self.borrow_from = borrow_from

    def git(self, *args: str, input: Optional[str] = None, timeout: int = GIT_TIMEOUT,
            check: bool = True) -> subprocess.CompletedProcess:
        # Anonymous: no credential helper, never a prompt.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
        return subprocess.run(
            ["git", "-c", "credential.helper=", "--git-dir", self.path, *args],
            input=input, capture_output=True, text=True, timeout=timeout, env=env, check=check,
        )

    def ensure(self) -> None:
        if not os.path.isdir(os.path.join(self.path, "objects")):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", self.path], check=True,
                           capture_output=True, timeout=GIT_TIMEOUT)
        alternates = os.path.join(self.path, "objects", "info", "alternates")
        borrowed = os.path.join(self.borrow_from, "objects") if self.borrow_from else None
        if borrowed and os.path.isdir(borrowed):
            os.makedirs(os.path.dirname(alternates), exist_ok=True)
            with open(alternates, "w", encoding="utf-8") as f:
                f.write(borrowed + "\n")
        elif os.path.exists(alternates):
            # Borrowing from a clone that is gone would break every pick.
            os.remove(alternates)

    def fetch(self, url: str, branch: str, patch_sets: set[tuple[int, int]]) -> None:
        specs = [f"+refs/heads/{branch}:refs/heads/{branch}"] + [
            f"+refs/changes/{n % 100:02d}/{n}/{ps}:refs/ps/{n}/{ps}" for n, ps in sorted(patch_sets)
        ]
        try:
            # --stdin keeps hundreds of refspecs off the command line.
            proc = self.git("fetch", "--quiet", "--no-tags", "--stdin", url,
                            input="\n".join(specs) + "\n", timeout=FETCH_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            raise TrialError("fetching the patch sets timed out") from None
        if proc.returncode != 0:
            raise TrialError("fetching the patch sets failed: "
                             + (proc.stderr.strip().splitlines() or ["?"])[-1][:200])
        # Drop the refs of patch sets no longer needed, so their objects can go.
        keep = {f"refs/ps/{n}/{ps}" for n, ps in patch_sets}
        stale = [r for r in self.git("for-each-ref", "--format=%(refname)", "refs/ps/").stdout.split()
                 if r not in keep]
        if stale:
            self.git("update-ref", "--stdin", input="".join(f"delete {r}\n" for r in stale))

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", "--verify", ref + "^{commit}").stdout.strip()

    def pick(self, onto: str, sha: str) -> Optional[str]:
        """Cherry-pick sha onto the commit onto: the new commit, or None on
        a conflict."""
        proc = self.git("merge-tree", "--write-tree", f"--merge-base={sha}^", onto, sha, check=False)
        if proc.returncode == 1:
            return None
        if proc.returncode != 0:
            raise TrialError(f"merge-tree failed: {proc.stderr.strip()[:200]}")
        tree = proc.stdout.split("\n", 1)[0].strip()
        return self.git("commit-tree", tree, "-p", onto, "-m", "trial pick").stdout.strip()


def try_picks(
    changes: list[dict],
    *,
    directory: str,
    gerrit_url: str,
    project: str,
    branch: str,
    borrow_from: Optional[str] = None,
    progress: Optional[Callable[[str, int, Optional[int]], None]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> int:
    """Try the candidates; set `mergeable` from the result, mark them
    `_picked` (classify then takes it as it is) and, when a change below
    is what fails, `_merge_blocked_by`. Returns how many were tried;
    raises TrialError when they could not be, and the votes then stand."""
    progress = progress or (lambda *_: None)
    log = log or (lambda _: None)
    picks = candidates(changes)
    if not picks:
        return 0
    by_number = {c["_number"]: c for c in changes}
    chains = {ch["_number"]: series(ch, by_number) for ch in picks}
    repo = Repo(os.path.join(directory, "repo.git"), borrow_from)
    try:
        repo.ensure()
        progress("fetching patch sets", 0, None)
        repo.fetch(f"{gerrit_url.rstrip('/')}/{project}", branch,
                   {step for chain in chains.values() for step in chain})
        base = repo.rev(f"refs/heads/{branch}")
        # A series' prefix is shared by every change above it: pick each
        # step once. done[(n, ps)] = the commit after it, or the change
        # that failed.
        done: dict[tuple[int, int], tuple[Optional[str], Optional[int]]] = {}
        conflicts = 0
        for i, ch in enumerate(picks, 1):
            head, failed = base, None
            for step in chains[ch["_number"]]:
                if step not in done:
                    new = repo.pick(head, repo.rev(f"refs/ps/{step[0]}/{step[1]}"))
                    done[step] = (new, None) if new else (None, step[0])
                head, failed = done[step]
                if failed:
                    break
            ch["mergeable"] = failed is None
            ch["_picked"] = True
            ch["_merge_blocked_by"] = failed if failed and failed != ch["_number"] else None
            conflicts += failed is not None
            progress("trial picks", i, len(picks))
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrialError(f"git: {exc.__class__.__name__}") from None
    log(f"trial picks: {len(picks)} tried, {conflicts} do not apply to {branch}")
    return len(picks)
