"""The judge's workspace, cut from a real (throwaway) git repository."""

import os
import subprocess

import pytest

from portal.promises import repo


def git(cwd, *args, env=None):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **(env or {})}
    )


@pytest.fixture
def mirror(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "master")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    (work / "lustre").mkdir()
    (work / "lustre" / "a.c").write_text("int a;\n")
    git(work, "add", ".")
    git(
        work,
        "commit",
        "-q",
        "-m",
        "LU-1 base",
        env={"GIT_AUTHOR_DATE": "2026-01-01T00:00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00"},
    )
    (work / "lustre" / "a.c").write_text("int a;\nint b; /* the promised fix */\n")
    os.symlink("/etc/hostname", work / "lustre" / "evil")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "LU-19999 mdt: keep the promise")
    bare = tmp_path / "mirror.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def test_a_snapshot_has_the_files_but_no_symlinks(mirror, tmp_path):
    sha = repo.rev_parse(mirror, "refs/heads/master")
    dest = tmp_path / "snap"
    repo.snapshot(mirror, sha, dest)
    assert (dest / "lustre" / "a.c").read_text().endswith("promised fix */\n")
    assert not os.path.lexists(dest / "lustre" / "evil"), "a symlink could point the judge outside"


def test_history_shows_the_commits_since_the_comment(mirror):
    master = repo.rev_parse(mirror, "refs/heads/master")
    text = repo.history(mirror, master, "2026-06-01", ["lustre/a.c"])
    assert "LU-19999 mdt: keep the promise" in text and "LU-1 base" not in text
    assert "+int b;" in text


def test_history_says_so_when_nothing_changed(mirror):
    master = repo.rev_parse(mirror, "refs/heads/master")
    assert "no commit on master" in repo.history(mirror, master, "2099-01-01", ["lustre/a.c"])


def test_ticket_log_finds_the_follow_up(mirror):
    master = repo.rev_parse(mirror, "refs/heads/master")
    assert "keep the promise" in repo.ticket_log(mirror, master, "LU-19999")
    assert "no commit on master names LU-9" in repo.ticket_log(mirror, master, "LU-9")


def test_long_output_is_cut_and_says_so():
    assert "truncated" in repo._limited("x" * 50, 10)


def test_change_refs_follow_gerrit_sharding():
    assert repo.change_ref(62757, 110) == "refs/changes/57/62757/110"
    assert repo.change_ref(5, 1) == "refs/changes/05/5/1"


def test_files_changed_works_for_a_merged_change_too(mirror):
    """A merged change is an ancestor of master; diffing from the merge
    base would give nothing."""
    master = repo.rev_parse(mirror, "refs/heads/master")
    assert repo.files_changed(mirror, master, master) == ["lustre/a.c", "lustre/evil"]


def test_a_snapshot_works_under_eventlet(mirror, tmp_path):
    """The web service runs monkey-patched by eventlet, which makes a
    child's pipes non-blocking; a pipe from git archive into tar then
    failed with EAGAIN. Run it the way the service does, in a process of
    its own so the patching cannot leak into the other tests."""
    import sys

    script = tmp_path / "snap.py"
    script.write_text(
        "import eventlet\n"
        "eventlet.monkey_patch()\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from portal.promises import repo\n"
        "m = Path(sys.argv[1])\n"
        "repo.snapshot(m, repo.rev_parse(m, 'refs/heads/master'), Path(sys.argv[2]))\n"
    )
    dest = tmp_path / "tree"
    proc = subprocess.run(
        [sys.executable, str(script), str(mirror), str(dest)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert (dest / "lustre" / "a.c").exists()


def test_fetches_into_one_mirror_take_turns(tmp_path):
    import threading
    import time

    mirror = tmp_path / "m.git"
    mirror.mkdir()
    inside, overlap = [0], [False]

    def use():
        with repo._exclusive(mirror):
            inside[0] += 1
            overlap[0] |= inside[0] > 1
            time.sleep(0.05)
            inside[0] -= 1

    workers = [threading.Thread(target=use) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert not overlap[0]


def test_sweep_removes_workspaces_of_dead_jobs_only(tmp_path):
    work = tmp_path / "work"
    mine, dead, unowned = work / "mine", work / "dead", work / "unowned"
    for d in (mine, dead, unowned):
        d.mkdir(parents=True)
    repo.claim(mine)
    (dead / repo.OWNER_FILE).write_text("999999999")
    repo.sweep(work)
    assert mine.exists() and not dead.exists() and not unowned.exists()


def test_the_changes_diff_comes_per_file_with_only_its_lines(mirror):
    master = repo.rev_parse(mirror, "refs/heads/master")
    got = repo.diff_by_file(mirror, master, master)
    assert set(got) == {"lustre/a.c", "lustre/evil"}
    assert got["lustre/a.c"] == "+int b; /* the promised fix */"
    assert repo.changed_lines(mirror, master, ["lustre/a.c"]) == "+int b; /* the promised fix */"


def test_split_diff_keeps_deleted_files_and_dash_lines():
    text = "\n".join(
        [
            "diff --git a/x.sh b/x.sh",
            "--- a/x.sh",
            "+++ b/x.sh",
            "@@ -1 +1 @@",
            "--- a removed line that began with two dashes",
            "+new",
            "diff --git a/gone.c b/gone.c",
            "--- a/gone.c",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-int gone;",
            "",
        ]
    )
    got = repo._split_diff(text)
    assert got == {
        "x.sh": "--- a removed line that began with two dashes\n+new",
        "gone.c": "-int gone;",
    }


def test_fetch_many_fetches_what_exists_and_skips_what_does_not(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "w"
    work.mkdir()
    git(work, "init", "-q", "-b", "master")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    for n in (11, 12):
        (work / f"f{n}").write_text(str(n))
        git(work, "add", ".")
        git(work, "commit", "-q", "-m", f"change {n}")
        git(work, "update-ref", f"refs/changes/{n % 100:02d}/{n}/1", "HEAD")
    git(tmp_path, "clone", "-q", "--bare", "--mirror", str(work), str(origin))
    mirror = repo.ensure_mirror(tmp_path / "repos", str(tmp_path), "origin.git")
    got = repo.fetch_many(mirror, [(11, 1), (12, 1), (13, 1), (0, 1), (14, None)])
    assert got == {(11, 1), (12, 1)}, "the missing one costs only itself"
    assert repo.rev_parse(mirror, "refs/promises/12-1")
    calls = []
    real = repo.git
    repo.git = lambda *a, **kw: (calls.append(a[1]), real(*a, **kw))[1]
    try:
        assert repo.fetch_many(mirror, [(11, 1)]) == {(11, 1)}
    finally:
        repo.git = real
    assert "fetch" not in calls, "already in the mirror: no fetch"
