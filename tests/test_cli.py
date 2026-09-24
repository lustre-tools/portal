"""Account management from the command line.

The predecessor of this module had no tests at all, which is a poor
place to have none: it is the only way accounts are created, and it
writes the file every login reads.
"""

import json
import os
import stat

import pytest

from portal import cli
from portal.users import load_users, roles_of, save_users, verify


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "users.json")


def run(store_path, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    cli.main(["--users-file", store_path, *argv])


def set_pw(store, user, password, monkeypatch):
    """Create or update an account. The password goes over stdin because
    the CLI deliberately has no --password option."""
    run(
        store,
        "set-password",
        "--user",
        user,
        "--password-stdin",
        stdin=password + "\n",
        monkeypatch=monkeypatch,
    )


# ---------- creating and changing accounts ----------


def test_set_password_creates_an_account(store, monkeypatch, capsys):
    set_pw(store, "alice", "s3cret", monkeypatch)
    data = load_users(store)
    assert "alice" in data["users"]
    assert verify(data, "alice", "s3cret")
    assert not verify(data, "alice", "wrong")


def test_set_password_changes_an_existing_one(store, monkeypatch):
    set_pw(store, "alice", "first", monkeypatch)
    set_pw(store, "alice", "second", monkeypatch)
    data = load_users(store)
    assert verify(data, "alice", "second")
    assert not verify(data, "alice", "first")


def test_set_password_keeps_roles(store, monkeypatch):
    """Changing a password must not silently drop someone's access."""
    set_pw(store, "alice", "x", monkeypatch)
    run(store, "add-role", "--user", "alice", "--role", "admin")
    set_pw(store, "alice", "y", monkeypatch)
    assert roles_of(load_users(store), "alice") == {"admin"}


def test_empty_password_is_refused(store, monkeypatch):
    with pytest.raises(SystemExit):
        set_pw(store, "alice", "", monkeypatch)


def test_the_password_never_appears_in_argv():
    """argv is visible in ps and lands in shell history."""
    parser_src = cli.__doc__ or ""
    assert "--password-stdin" in parser_src
    # There must be no --password option at all, only --password-stdin.
    with pytest.raises(SystemExit):
        cli.main(["set-password", "--user", "x", "--password", "hunter2"])


# ---------- roles ----------


def test_add_and_remove_roles(store, monkeypatch, capsys):
    set_pw(store, "sam", "x", monkeypatch)
    run(store, "add-role", "--user", "sam", "--role", "internal")
    assert roles_of(load_users(store), "sam") == {"internal"}
    run(store, "add-role", "--user", "sam", "--role", "admin")
    assert roles_of(load_users(store), "sam") == {"admin", "internal"}
    run(store, "remove-role", "--user", "sam", "--role", "internal")
    assert roles_of(load_users(store), "sam") == {"admin"}


def test_adding_a_role_twice_is_harmless(store, monkeypatch):
    set_pw(store, "sam", "x", monkeypatch)
    run(store, "add-role", "--user", "sam", "--role", "admin")
    run(store, "add-role", "--user", "sam", "--role", "admin")
    assert roles_of(load_users(store), "sam") == {"admin"}


def test_role_for_an_unknown_user_fails(store):
    with pytest.raises(SystemExit):
        run(store, "add-role", "--user", "ghost", "--role", "admin")


def test_removing_an_absent_role_fails(store, monkeypatch):
    set_pw(store, "sam", "x", monkeypatch)
    with pytest.raises(SystemExit):
        run(store, "remove-role", "--user", "sam", "--role", "admin")


def test_an_unknown_role_is_stored_with_a_note(store, monkeypatch, capsys):
    """Operators can invent roles for /_authcheck; the app just warns."""
    set_pw(store, "sam", "x", monkeypatch)
    run(store, "add-role", "--user", "sam", "--role", "wiki")
    assert roles_of(load_users(store), "sam") == {"wiki"}
    assert "not a role this app checks" in capsys.readouterr().err


# ---------- deleting ----------


def test_delete_removes_the_account(store, monkeypatch):
    set_pw(store, "sam", "x", monkeypatch)
    run(store, "delete", "--user", "sam")
    assert "sam" not in load_users(store)["users"]


def test_deleting_an_unknown_user_fails(store):
    with pytest.raises(SystemExit):
        run(store, "delete", "--user", "ghost")


# ---------- the file itself ----------


def test_the_store_is_owner_only(store, monkeypatch):
    """It holds password hashes; the umask should not get a say."""
    set_pw(store, "sam", "x", monkeypatch)
    mode = stat.S_IMODE(os.stat(store).st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_a_write_replaces_atomically(store, monkeypatch, tmp_path):
    """A reader must never see a half-written file, so the new content
    arrives by rename rather than by truncate-and-write."""
    set_pw(store, "sam", "x", monkeypatch)
    before = os.stat(store).st_ino
    set_pw(store, "other", "y", monkeypatch)
    assert os.stat(store).st_ino != before, "file was written in place"
    # No temp files left behind.
    leftovers = [f for f in os.listdir(os.path.dirname(store)) if f.endswith(".tmp")]
    assert leftovers == []


def test_a_missing_store_reads_as_empty(store):
    data = load_users(store)
    assert data["users"] == {}


# ---------- migration ----------


def test_migrate_converts_the_legacy_layout(store, capsys):
    """The zone names are arbitrary: anything other than the base zone
    becomes the privileged role, whatever a deployment called it."""
    with open(store, "w") as f:
        json.dump(
            {
                "main": {"alice": "hash-a", "boss": "hash-b"},
                "restricted": {"sam": "hash-c"},
                "admins": ["boss"],
            },
            f,
        )

    run(store, "migrate")
    data = load_users(store)
    assert roles_of(data, "alice") == set()
    assert roles_of(data, "boss") == {"admin"}
    assert roles_of(data, "sam") == {"internal"}
    # The old file is kept, not clobbered.
    assert os.path.exists(store + ".pre-roles")


def test_migrate_dry_run_changes_nothing(store, capsys):
    original = {"main": {"alice": "hash-a"}, "admins": []}
    with open(store, "w") as f:
        json.dump(original, f)
    run(store, "migrate", "--dry-run")
    with open(store) as f:
        assert json.load(f) == original


def test_migrate_is_idempotent(store, monkeypatch, capsys):
    set_pw(store, "sam", "x", monkeypatch)
    run(store, "migrate")
    assert "already in the roles format" in capsys.readouterr().out


def test_migrate_warns_about_a_name_in_two_zones(store, capsys):
    """Each zone had its own password, so one has to be dropped. Say
    which, rather than silently picking."""
    with open(store, "w") as f:
        json.dump(
            {
                "main": {"sam": "hash-main"},
                "restricted": {"sam": "hash-other"},
                "admins": [],
            },
            f,
        )
    run(store, "migrate")
    err = capsys.readouterr().err
    assert "different passwords" in err
    data = load_users(store)
    assert data["users"]["sam"]["password_hash"] == "hash-main"
    assert roles_of(data, "sam") == {"internal"}


def test_migrate_warns_about_an_orphan_admin(store, capsys):
    with open(store, "w") as f:
        json.dump({"main": {}, "admins": ["ghost"]}, f)
    run(store, "migrate")
    assert "had no account" in capsys.readouterr().err


def test_legacy_file_is_readable_without_migrating(store):
    """Reading migrates in memory only, so an accidental downgrade
    cannot destroy the old file."""
    with open(store, "w") as f:
        json.dump({"main": {"alice": "h"}, "restricted": {}, "admins": []}, f)
    data = load_users(store)
    assert "alice" in data["users"]
    with open(store) as f:
        assert "main" in json.load(f), "reading must not rewrite the file"


# ---------- listing ----------


def test_list_shows_accounts_and_roles(store, monkeypatch, capsys):
    set_pw(store, "sam", "x", monkeypatch)
    run(store, "add-role", "--user", "sam", "--role", "admin")
    run(store, "list")
    out = capsys.readouterr().out
    assert "sam" in out and "admin" in out


def test_list_on_an_empty_store(store, capsys):
    run(store, "list")
    assert "no users" in capsys.readouterr().out


def test_save_then_load_round_trips(store):
    data = {"version": 2, "users": {"a": {"password_hash": "h", "roles": ["admin"]}}}
    save_users(store, data)
    assert load_users(store) == data


# ---------- running as root must not lock the service out ----------


def _as_root(monkeypatch, calls):
    import os

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))


def test_a_root_run_write_keeps_the_existing_owner(store, monkeypatch):
    """Root is exactly who runs portal-users on a server. Without this the
    rewritten store came out root-owned 0600, the service user could not
    read it, and every login failed."""
    import os

    set_pw(store, "sam", "x", monkeypatch)
    st = os.stat(store)
    calls = []
    _as_root(monkeypatch, calls)
    set_pw(store, "sam", "y", monkeypatch)
    assert calls, "a root-run write did not preserve ownership"
    assert all(c == (st.st_uid, st.st_gid) for c in calls)


def test_a_root_run_first_write_takes_the_directory_owner(store, monkeypatch):
    import os

    calls = []
    _as_root(monkeypatch, calls)
    set_pw(store, "sam", "x", monkeypatch)
    d = os.stat(os.path.dirname(store))
    assert (d.st_uid, d.st_gid) in calls


def test_a_non_root_write_changes_no_ownership(store, monkeypatch):
    import os

    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "fchown", lambda *a: calls.append(a))
    set_pw(store, "sam", "x", monkeypatch)
    assert calls == []
