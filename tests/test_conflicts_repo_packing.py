"""The refresh keeps the `gc graph --conflicts` clone packed, keeping
every object -- graphs may be merging unreferenced commits right then."""

import subprocess
from types import SimpleNamespace

from portal import refresh

ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}  # fmt: skip


def git(repo, *args, input=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        input=input, env=ENV,
    ).stdout.strip()  # fmt: skip


def counts(repo):
    out = dict(line.split(": ", 1) for line in git(repo, "count-objects", "-v").splitlines())
    return int(out["packs"]), int(out["count"])


def make_clone(tmp_path, packs):
    """A bare clone with ``packs`` packs, each holding an unreferenced
    commit -- what gc's fetches leave behind."""
    repo = tmp_path / "lustre-release.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True, env=ENV)
    tree = git(repo, "mktree", input="")
    commits = []
    for i in range(packs):
        sha = git(repo, "commit-tree", tree, "-m", f"change {i}")
        git(repo, "pack-objects", "-q", str(repo / "objects" / "pack" / "pack"), input=sha + "\n")
        git(repo, "prune-packed")
        commits.append(sha)
    return repo, commits


def test_many_packs_are_packed_into_one_keeping_everything(tmp_path, monkeypatch):
    repo, commits = make_clone(tmp_path, 4)
    monkeypatch.setattr(refresh, "CONFLICTS_REPO_MAX_PACKS", 2)
    refresh._pack_conflicts_repo(SimpleNamespace(config={"GRAPH_CONFLICTS_REPO": str(repo)}))
    assert counts(repo)[0] == 1
    for sha in commits:
        assert git(repo, "cat-file", "-t", sha) == "commit", "nothing is pruned"


def test_a_tidy_clone_is_left_alone(tmp_path):
    repo, _ = make_clone(tmp_path, 2)
    refresh._pack_conflicts_repo(SimpleNamespace(config={"GRAPH_CONFLICTS_REPO": str(repo)}))
    assert counts(repo)[0] == 2


def test_no_clone_nothing_to_do(tmp_path):
    refresh._pack_conflicts_repo(SimpleNamespace(config={"GRAPH_CONFLICTS_REPO": None}))
    refresh._pack_conflicts_repo(
        SimpleNamespace(config={"GRAPH_CONFLICTS_REPO": str(tmp_path / "gone")})
    )
