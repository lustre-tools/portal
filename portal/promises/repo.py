"""The code the judge reads: a mirror of the public repository, and
read-only snapshots cut from it for one check.

Only the public project's ``master`` is supported: the judge compares a
change's current patchset with master. The mirror is fetched
anonymously, so no credential is ever involved.

A check never touches the mirror itself. It gets a directory of plain
files -- two trees (``change/``, ``master/``), plus history and diffs
worked out here -- so the model needs no shell at all: Read, Grep and
Glob inside that directory are all it can do. Symlinks are dropped when a
tree is unpacked; Claude Code already refuses to follow one outside its
working directory, and this makes sure there is nothing to follow.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

#: Size limits for the files written for the judge, in bytes.
HISTORY_LIMIT = 200_000
DIFF_LIMIT = 150_000

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class RepoError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "GIT_TERMINAL_PROMPT": "0", "LANG": "C.UTF-8"}


def git(mirror: Path, *args: str, timeout: int = 300, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "--git-dir", str(mirror), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(),
    )
    if check and proc.returncode != 0:
        raise RepoError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def ensure_mirror(root: Path, gerrit_url: str, project: str) -> Path:
    """The bare mirror for ``project``, created on first use."""
    mirror = Path(root) / (_SAFE.sub("_", project) + ".git")
    if not (mirror / "HEAD").exists():
        mirror.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "-q", str(mirror)], check=True, env=_env(), timeout=60
        )
    url = f"{gerrit_url.rstrip('/')}/{project}"
    git(mirror, "config", "remote.origin.url", url)
    return mirror


def change_ref(number: int, patchset: int) -> str:
    return f"refs/changes/{number % 100:02d}/{number}/{patchset}"


_FETCH_LOCK = threading.Lock()


@contextlib.contextmanager
def _exclusive(mirror: Path):
    """One fetch into a mirror at a time -- across threads and processes
    (two checks, the CLI): concurrent fetches fail on git's ref locks."""
    with _FETCH_LOCK, open(mirror / "promises-fetch.lock", "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def fetch(
    mirror: Path, number: int | None = None, patchset: int | None = None, timeout: int = 1800
) -> None:
    """Bring master (and optionally one change's patchset) up to date.
    The first call fetches the whole history and takes a while."""
    refspecs = ["+refs/heads/master:refs/heads/master"]
    if number and patchset:
        number, patchset = int(number), int(patchset)
        refspecs.append(f"+{change_ref(number, patchset)}:refs/promises/{number}-{patchset}")
    with _exclusive(mirror):
        git(mirror, "fetch", "--no-tags", "-q", "origin", *refspecs, timeout=timeout)


def rev_parse(mirror: Path, ref: str) -> str:
    return git(mirror, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def snapshot(mirror: Path, sha: str, dest: Path, timeout: int = 600) -> None:
    """Unpack the tree at ``sha`` into ``dest``, without symlinks.

    Through a file, not a pipe: under eventlet (the web service) a child's
    pipe is non-blocking, and ``tar`` reading one gives up with EAGAIN.
    """
    dest.mkdir(parents=True, exist_ok=True)
    tarball = dest.parent / f".{dest.name}.tar"
    try:
        git(mirror, "archive", "--format=tar", "-o", str(tarball), sha, timeout=timeout)
        tar = subprocess.run(
            [
                "tar",
                "-x",
                "-f",
                str(tarball),
                "-C",
                str(dest),
                "--no-same-owner",
                "--no-same-permissions",
            ],
            capture_output=True,
            timeout=timeout,
            env=_env(),
        )
        if tar.returncode != 0:
            raise RepoError(f"could not unpack {sha[:12]}: {(tar.stderr or b'').decode()[:200]}")
    finally:
        tarball.unlink(missing_ok=True)
    for dirpath, dirnames, filenames in os.walk(dest):
        for name in filenames + dirnames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                os.unlink(p)


def _limited(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated: {len(text) - limit} more bytes not shown ...]\n"


def files_changed(mirror: Path, base: str, sha: str) -> list[str]:
    """Files the change touches relative to where it branched from master."""
    merge_base = git(mirror, "merge-base", base, sha, check=False).strip()
    if not merge_base or merge_base == sha:
        # Already on master (merged): its own diff is against its parent.
        merge_base = f"{sha}^"
    out = git(mirror, "diff", "--name-only", merge_base, sha, check=False)
    return [line for line in out.splitlines() if line.strip()]


def history(mirror: Path, master: str, since: str, paths: list[str]) -> str:
    """Commits on master since ``since`` that touch ``paths``, newest
    first, with their diffs limited to those paths."""
    if not paths:
        return "(no files to follow)\n"
    out = git(
        mirror,
        "log",
        "--no-merges",
        f"--since={since}",
        "--format=%n==== commit %H%nDate:    %ad%nAuthor:  %an%nSubject: %s%n%n%b",
        "--date=short",
        "-p",
        master,
        "--",
        *paths,
        check=False,
        timeout=300,
    )
    return (
        _limited(out, HISTORY_LIMIT) or f"(no commit on master touched these files since {since})\n"
    )


def ticket_log(mirror: Path, master: str, ticket: str) -> str:
    out = git(
        mirror,
        "log",
        "--no-merges",
        f"--grep={ticket}",
        "--format=%n==== commit %H%nDate:    %ad%nSubject: %s%n%n%b",
        "--date=short",
        "--stat",
        master,
        check=False,
        timeout=300,
    )
    return _limited(out, HISTORY_LIMIT) or f"(no commit on master names {ticket})\n"


def show(mirror: Path, sha: str) -> str:
    return _limited(git(mirror, "show", "--stat", "-p", sha, check=False), DIFF_LIMIT)


def safe_name(text: str) -> str:
    return _SAFE.sub("_", text)[:80]


def remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


OWNER_FILE = ".owner"


def claim(workdir: Path) -> None:
    """Mark a workspace as belonging to this process."""
    (workdir / OWNER_FILE).write_text(str(os.getpid()))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is someone else's
    return True


def sweep(work: Path, older_than: int = 6 * 3600) -> None:
    """Remove check workspaces left behind by a job that was killed (a
    restart mid-check): those whose owning process is gone, and any older
    than a check can possibly take."""
    import time

    if not work.is_dir():
        return
    cutoff = time.time() - older_than
    for child in work.iterdir():
        try:
            owner = int((child / OWNER_FILE).read_text().strip())
        except (OSError, ValueError):
            owner = None
        try:
            stale = child.stat().st_mtime < cutoff
        except OSError:
            continue
        if stale or owner is None or not _alive(owner):
            shutil.rmtree(child, ignore_errors=True)
