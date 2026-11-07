"""Operator tool for the production user model.

Run inside the application container (or any environment configured exactly like
it) so ``APP_ENV`` and ``DATABASE_PATH`` match production::

    python scripts/manage_accounts.py list
    python scripts/manage_accounts.py set-status --email owner@example.com --status suspended
    python scripts/manage_accounts.py plan-legacy-assignment --owner-email owner@example.com
    python scripts/manage_accounts.py assign-legacy-data --owner-email owner@example.com \\
        --confirm-plan <digest from the plan> --backup-verified

``plan-legacy-assignment`` is read-only. ``assign-legacy-data`` changes data and
only proceeds for a verified owner, for the exact plan the operator confirmed,
after the operator attests that a verified backup exists. See
``docs/USER_MODEL.md`` for the full runbook.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

# Allow ``python scripts/manage_accounts.py`` to find sibling modules.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from accounts import (
    AccountError,
    has_verified_identity,
    set_account_status,
)
from database import (
    get_all_users,
    init_db,
    transaction,
)
from legacy_ownership import (
    OwnershipError,
    OwnershipPlan,
    apply_legacy_ownership,
    plan_legacy_ownership,
)
from tenancy import normalise_email

_BLOCKER_HELP = {
    "owner_not_found": "no account has that email; the owner must sign in once first",
    "owner_not_active": "the owner's account is not active",
    "owner_has_no_verified_identity": (
        "the owner has not signed in through a verified identity since accounts "
        "were introduced; have them sign in once"
    ),
    "owner_is_legacy_placeholder": "the placeholder account cannot own its own data",
    "already_assigned_to_different_owner": (
        "the legacy data was already assigned to a different owner"
    ),
}


def _describe_blocker(blocker: str) -> str:
    code, _, detail = blocker.partition(":")
    if code == "unmapped_user_table":
        return (
            f"table '{detail}' holds per-user rows the assignment does not cover; "
            "a code change is required before data can be assigned"
        )
    return _BLOCKER_HELP.get(blocker, blocker)


def _database_path(override: str | None) -> str:
    return (
        override or os.environ.get("DATABASE_PATH", "workout_agent.db")
    ).strip()


def _print_plan(plan: OwnershipPlan) -> None:
    print("Legacy data ownership plan")
    print(f"  owner:  {plan.owner_email}")
    if plan.already_assigned:
        print("  state:  the legacy data was already assigned to this owner")
    print()
    if plan.tables:
        print(f"  {'table':<22}{'to assign':>10}{'left behind':>13}")
        for entry in plan.tables:
            print(f"  {entry.table:<22}{entry.movable:>10}{entry.left_behind:>13}")
        print()
    print(
        f"  rows to assign: {plan.movable_rows}; "
        f"left on the retired placeholder: {plan.left_behind_rows}"
    )
    if plan.blockers:
        print()
        print("Blocked:")
        for blocker in plan.blockers:
            print(f"  - {blocker}: {_describe_blocker(blocker)}")
        return
    if plan.movable_rows == 0:
        print("Nothing to assign.")
        return
    print()
    print(f"Plan digest: {plan.digest}")


def _command_list(db_path: str) -> int:
    init_db(db_path)
    print(f"{'status':<11}{'verified':<10}{'created':<22}{'email':<34}id")
    users = get_all_users(db_path, include_inactive=True)
    with transaction(db_path) as conn:
        for user in users:
            verified = "yes" if has_verified_identity(conn, user["id"]) else "no"
            print(
                f"{user['status']:<11}{verified:<10}"
                f"{user['created_at'][:19]:<22}{user['email']:<34}{user['id']}"
            )
    return 0


def _command_set_status(db_path: str, email: str, status: str) -> int:
    init_db(db_path)
    canonical = normalise_email(email)
    users = [
        user
        for user in get_all_users(db_path, include_inactive=True)
        if user["email"].lower() == canonical
    ]
    if not users:
        print("No account has that email.", file=sys.stderr)
        return 1
    try:
        updated = set_account_status(users[0]["id"], status, db_path)
    except AccountError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    print(f"Account is now {updated['status']}.")
    return 0


def _command_plan(db_path: str, owner_email: str, as_json: bool) -> int:
    init_db(db_path)
    plan = plan_legacy_ownership(db_path, owner_email=owner_email)
    if as_json:
        print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    else:
        _print_plan(plan)
    return 1 if plan.blockers else 0


def _command_assign(
    db_path: str,
    owner_email: str,
    confirm_plan: str,
    backup_verified: bool,
    operator: str,
) -> int:
    init_db(db_path)
    try:
        result = apply_legacy_ownership(
            db_path,
            owner_email=owner_email,
            confirm_digest=confirm_plan,
            applied_by=operator,
            backup_verified=backup_verified,
        )
    except OwnershipError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    if result.status == "assigned":
        total = sum(result.moved.values())
        print(f"Assigned {total} rows to the verified owner.")
        for table, count in sorted(result.moved.items()):
            print(f"  {table}: {count}")
    elif result.status == "already_assigned":
        print("The legacy data was already assigned to this owner; nothing to do.")
    else:
        print("There is no legacy data to assign.")
    if result.left_behind:
        print("Left on the retired placeholder (the owner already had a conflicting row):")
        for table, count in sorted(result.left_behind.items()):
            print(f"  {table}: {count}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and administer accounts and legacy data ownership."
    )
    parser.add_argument(
        "--database",
        help="SQLite database path (defaults to $DATABASE_PATH).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="List accounts and their status.")

    status = commands.add_parser("set-status", help="Suspend or reactivate an account.")
    status.add_argument("--email", required=True)
    status.add_argument("--status", required=True, choices=("active", "suspended"))

    plan = commands.add_parser(
        "plan-legacy-assignment",
        help="Read-only: show what assigning the legacy data would do.",
    )
    plan.add_argument("--owner-email", required=True)
    plan.add_argument("--json", action="store_true", dest="as_json")

    assign = commands.add_parser(
        "assign-legacy-data",
        help="Assign the legacy data to a verified owner.",
    )
    assign.add_argument("--owner-email", required=True)
    assign.add_argument(
        "--confirm-plan",
        required=True,
        help="Digest printed by plan-legacy-assignment.",
    )
    assign.add_argument(
        "--backup-verified",
        action="store_true",
        help="Attest that a current backup has been verified by a restore drill.",
    )
    assign.add_argument(
        "--operator",
        default=None,
        help="Name recorded in the audit trail (defaults to the OS user).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    db_path = _database_path(args.database)
    if args.command == "list":
        return _command_list(db_path)
    if args.command == "set-status":
        return _command_set_status(db_path, args.email, args.status)
    if args.command == "plan-legacy-assignment":
        return _command_plan(db_path, args.owner_email, args.as_json)
    operator = args.operator or getpass.getuser()
    return _command_assign(
        db_path,
        args.owner_email,
        args.confirm_plan,
        args.backup_verified,
        operator,
    )


if __name__ == "__main__":
    sys.exit(main())
