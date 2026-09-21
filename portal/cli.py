"""Account management for the portal.

    portal-users list
    portal-users set-password --user alice          # prompts
    portal-users set-password --user alice --password-stdin < secret
    portal-users add-role --user alice --role admin
    portal-users remove-role --user alice --role internal
    portal-users delete --user alice
    portal-users migrate                            # legacy two-zone file -> roles

Passwords are never taken as a command-line argument: argv is visible in
``ps`` and lands in shell history.
"""

import argparse
import getpass
import os
import sys

from portal.config import build_config
from portal.users import (
    ROLE_ADMIN,
    ROLE_INTERNAL,
    delete_user,
    load_users,
    migrate_legacy,
    roles_of,
    save_users,
    set_password,
    set_roles,
)

KNOWN_ROLES = (ROLE_ADMIN, ROLE_INTERNAL)


def _users_file(args):
    if args.users_file:
        return os.path.abspath(args.users_file)
    return build_config(testing=True)["USERS_FILE"]


def _read_password(args):
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            sys.exit("error: no password on stdin")
        return password
    password = getpass.getpass("Password: ")
    if not password:
        sys.exit("error: empty password")
    if password != getpass.getpass("Repeat: "):
        sys.exit("error: passwords do not match")
    return password


def cmd_set_password(args, path):
    data = load_users(path)
    existed = args.user in data.get("users", {})
    set_password(data, args.user, _read_password(args))
    save_users(path, data)
    print(f"{'Updated' if existed else 'Created'} {args.user!r} in {path}")


def cmd_add_role(args, path):
    data = load_users(path)
    if args.user not in data.get("users", {}):
        sys.exit(f"error: no such user {args.user!r}; set a password first")
    if args.role not in KNOWN_ROLES:
        print(
            f"note: {args.role!r} is not a role this app checks "
            f"({', '.join(KNOWN_ROLES)}); it will still be stored and is "
            f"usable with /_authcheck?role={args.role}",
            file=sys.stderr,
        )
    set_roles(data, args.user, roles_of(data, args.user) | {args.role})
    save_users(path, data)
    print(f"{args.user!r} now has: {', '.join(sorted(roles_of(data, args.user))) or '(none)'}")


def cmd_remove_role(args, path):
    data = load_users(path)
    if args.user not in data.get("users", {}):
        sys.exit(f"error: no such user {args.user!r}")
    roles = roles_of(data, args.user)
    if args.role not in roles:
        sys.exit(f"error: {args.user!r} does not have role {args.role!r}")
    set_roles(data, args.user, roles - {args.role})
    save_users(path, data)
    print(f"{args.user!r} now has: {', '.join(sorted(roles_of(data, args.user))) or '(none)'}")


def cmd_delete(args, path):
    data = load_users(path)
    try:
        delete_user(data, args.user)
    except KeyError:
        sys.exit(f"error: no such user {args.user!r}")
    save_users(path, data)
    print(f"Deleted {args.user!r}")


def cmd_list(args, path):
    data = load_users(path)
    users = data.get("users", {})
    if not users:
        print(f"(no users in {path})")
        return
    width = max(len(u) for u in users)
    for username in sorted(users):
        roles = ", ".join(sorted(roles_of(data, username))) or "-"
        print(f"  {username:<{width}}  {roles}")


def cmd_migrate(args, path):
    if not os.path.exists(path):
        sys.exit(f"error: {path} does not exist")
    import json

    with open(path) as f:
        raw = json.load(f)
    if "users" in raw:
        print(f"{path} is already in the roles format; nothing to do.")
        return
    migrated, warnings = migrate_legacy(raw)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.dry_run:
        print(json.dumps(migrated, indent=2))
        print("\n(dry run; nothing written)", file=sys.stderr)
        return
    backup = path + ".pre-roles"
    os.replace(path, backup)
    save_users(path, migrated)
    print(f"Migrated {len(migrated['users'])} account(s). Old file kept at {backup}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="portal-users", description="Manage portal accounts and roles"
    )
    parser.add_argument(
        "--users-file",
        help="path to users.json (default: PORTAL_USERS_FILE, else <data dir>/users.json)",
    )
    sub = parser.add_subparsers(dest="command")

    sp = sub.add_parser("set-password", help="create an account or change its password")
    sp.add_argument("--user", required=True)
    sp.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )
    sp.set_defaults(func=cmd_set_password)

    ap = sub.add_parser("add-role", help="grant a role")
    ap.add_argument("--user", required=True)
    ap.add_argument("--role", required=True)
    ap.set_defaults(func=cmd_add_role)

    rp = sub.add_parser("remove-role", help="revoke a role")
    rp.add_argument("--user", required=True)
    rp.add_argument("--role", required=True)
    rp.set_defaults(func=cmd_remove_role)

    dp = sub.add_parser("delete", help="delete an account")
    dp.add_argument("--user", required=True)
    dp.set_defaults(func=cmd_delete)

    lp = sub.add_parser("list", help="list accounts and their roles")
    lp.set_defaults(func=cmd_list)

    mp = sub.add_parser("migrate", help="convert a legacy two-zone users.json")
    mp.add_argument("--dry-run", action="store_true")
    mp.set_defaults(func=cmd_migrate)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args, _users_file(args))


if __name__ == "__main__":
    main()
