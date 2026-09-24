"""The user store: one account table, roles attached to each account.

Accounts live in a JSON file (``PORTAL_USERS_FILE``) that is never in
version control::

    {
      "version": 2,
      "users": {
        "alice": {"password_hash": "scrypt:...", "roles": []},
        "rob":   {"password_hash": "scrypt:...", "roles": ["admin"]},
        "sam":   {"password_hash": "scrypt:...", "roles": ["internal"]}
      }
    }

Two roles matter to the app:

``admin``
    May schedule automatic graph refreshes, and implicitly has
    ``internal``.
``internal``
    May see and act on graphs outside ``PORTAL_PUBLIC_PROJECT``, and is
    the default role gating the optional private area.

Anything else is carried through untouched, so a deployment can invent
its own roles and gate its own services on them via ``/_authcheck``.
"""

import fcntl
import json
import os
import tempfile

from werkzeug.security import check_password_hash, generate_password_hash

from portal.fsutil import owner_to_keep

#: Roles the application itself understands.
ROLE_ADMIN = "admin"
ROLE_INTERNAL = "internal"

CURRENT_VERSION = 2

# Compared against when the username does not exist, so a login attempt
# costs the same whether or not the account is real. Without it, response
# time reveals which usernames exist.
_DUMMY_HASH = generate_password_hash("dummy-never-matches")


def _empty():
    return {"version": CURRENT_VERSION, "users": {}}


#: The base zone in the legacy layout: accounts there start with no roles.
LEGACY_BASE_ZONE = "main"


def migrate_legacy(data, privileged_role=ROLE_INTERNAL):
    """Convert the old zone-based file to the roles layout.

    The old shape was ``{"<zone>": {user: hash}, ..., "admins": [user]}``
    -- independent credential sets per zone, plus a flat admin roster
    that only applied to the base zone. Accounts in the base zone become
    plain users (plus ``admin`` if listed); accounts in any other zone
    become users holding ``privileged_role``.

    Returns ``(migrated, warnings)``.
    """
    warnings = []
    users = {}
    admins = set(data.get("admins") or [])

    zones = {
        name: table for name, table in data.items() if name != "admins" and isinstance(table, dict)
    }

    for username, password_hash in (zones.get(LEGACY_BASE_ZONE) or {}).items():
        roles = [ROLE_ADMIN] if username in admins else []
        users[username] = {"password_hash": password_hash, "roles": roles}

    for zone, table in zones.items():
        if zone == LEGACY_BASE_ZONE:
            continue
        for username, password_hash in table.items():
            if username in users:
                # The same name in two zones. Each zone had its own
                # password, so if the hashes differ one must be dropped;
                # say which, rather than silently picking.
                if users[username]["password_hash"] != password_hash:
                    warnings.append(
                        f"user {username!r} existed in both {LEGACY_BASE_ZONE!r} "
                        f"and {zone!r} with different passwords; kept the "
                        f"{LEGACY_BASE_ZONE!r} one"
                    )
                if privileged_role not in users[username]["roles"]:
                    users[username]["roles"].append(privileged_role)
            else:
                users[username] = {
                    "password_hash": password_hash,
                    "roles": [privileged_role],
                }

    for username in sorted(admins - set(users)):
        warnings.append(f"{username!r} was listed as an admin but had no account; dropped")

    return {"version": CURRENT_VERSION, "users": users}, warnings


def load_users(path):
    """Read the store, migrating the legacy layout on the fly.

    Migration here is read-only: the file is rewritten only when
    something calls :func:`save_users`, so an accidental downgrade can't
    destroy the old file.
    """
    if not path or not os.path.exists(path):
        return _empty()
    with open(path) as f:
        data = json.load(f)

    if "users" in data:
        data.setdefault("version", CURRENT_VERSION)
        for entry in data["users"].values():
            entry.setdefault("roles", [])
        return data

    migrated, _ = migrate_legacy(data)
    return migrated


def save_users(path, data):
    """Write the store atomically, owner-readable only.

    Credentials, so: same flock + temp-file + rename discipline as the
    graph index, and mode 0600 rather than whatever the umask happens to
    be. A reader must never see a half-written file.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    owner = owner_to_keep(path)
    lock_path = path + ".lock"
    with open(lock_path, "w") as lock:
        if owner:
            os.fchown(lock.fileno(), *owner)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                os.fchmod(fd, 0o600)
                if owner:
                    os.fchown(fd, *owner)
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def verify(data, username, password):
    """Return the account dict on a correct password, else ``None``.

    Always runs one hash comparison, whether or not the account exists,
    so timing does not disclose which usernames are real.
    """
    users = data.get("users", {})
    entry = users.get(username)
    stored = entry["password_hash"] if entry else _DUMMY_HASH
    ok = check_password_hash(stored, password)
    if entry and ok:
        return entry
    return None


def roles_of(data, username):
    entry = data.get("users", {}).get(username)
    if not entry:
        return set()
    return set(entry.get("roles") or [])


def set_password(data, username, password):
    users = data.setdefault("users", {})
    entry = users.setdefault(username, {"roles": []})
    entry["password_hash"] = generate_password_hash(password)
    return data


def set_roles(data, username, roles):
    users = data.setdefault("users", {})
    if username not in users:
        raise KeyError(username)
    users[username]["roles"] = sorted(set(roles))
    return data


def delete_user(data, username):
    users = data.setdefault("users", {})
    if username not in users:
        raise KeyError(username)
    del users[username]
    return data
