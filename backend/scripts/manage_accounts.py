"""Operator CLI for accounts, access policy and legacy data ownership.

Run inside the application container (or any environment configured exactly like
it) so ``APP_ENV`` and ``DATABASE_PATH`` match production::

    python scripts/manage_accounts.py list
    python scripts/manage_accounts.py status person@example.com
    python scripts/manage_accounts.py invite person@example.com --days 7
    python scripts/manage_accounts.py revoke-invite person@example.com
    python scripts/manage_accounts.py set-status --email person@example.com --status suspended
    python scripts/manage_accounts.py set-role --email owner@example.com --role owner
    python scripts/manage_accounts.py suspend person@example.com
    python scripts/manage_accounts.py reactivate person@example.com
    python scripts/manage_accounts.py revoke-sessions person@example.com
    python scripts/manage_accounts.py delete person@example.com --yes
    python scripts/manage_accounts.py plan-legacy-assignment --owner-email owner@example.com
    python scripts/manage_accounts.py assign-legacy-data --owner-email owner@example.com \\
        --confirm-plan <digest from the plan> --backup-verified

``plan-legacy-assignment`` is read-only. ``assign-legacy-data`` changes data and
only proceeds for a verified owner, for the exact plan the operator confirmed,
after the operator attests that a verified backup exists. See
``docs/USER_MODEL.md`` and ``docs/ACCOUNT_ACCESS_POLICY.md`` for the runbooks.

Every access state change takes effect on the account's very next web request:
the server re-reads this state on each request and every status change also
advances the account's session epoch, ending all of its signed sessions. Nothing
here prints a secret; accounts are identified by opaque id and pseudonymous tag.

``delete`` is an *access* tombstone. It permanently blocks sign-in and ends
sessions but does not erase the account's data or stop background jobs; that
belongs to the account-deletion and suspension-lifecycle work. ``set-role``
assigns an administrative role (`owner`, `admin`, `support`, `user`) and ends the
account's earlier sessions; new accounts are `user` with no administrative
permission. See ``docs/ROLE_PERMISSIONS.md``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow ``python scripts/manage_accounts.py`` to find sibling modules.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from account_policy import canonical_email
from accounts import (
    AccountError,
    AccountRole,
    has_verified_identity,
)
from accounts import (
    set_account_role as set_operator_role,
)
from accounts import (
    set_account_status as set_operator_status,
)
from data_classification import pseudonymise_identifier
from database import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_DELETED,
    ACCOUNT_STATUS_SUSPENDED,
    AccountState,
    AccountTransitionError,
    create_account_invitation,
    find_account_state,
    get_all_users,
    init_db,
    list_account_invitations,
    revoke_account_invitations,
    revoke_account_sessions,
    transaction,
)
from database import (
    set_account_status as set_policy_status,
)
from legacy_ownership import (
    OwnershipError,
    OwnershipPlan,
    apply_legacy_ownership,
    plan_legacy_ownership,
)
from tenancy import LEGACY_TENANT_EMAIL, normalise_email

DEFAULT_INVITE_DAYS = 7
MAX_INVITE_DAYS = 90

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

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


def _describe(state: AccountState) -> str:
    return (
        f"account={state['id']} status={state['status']} "
        f"session_epoch={state['session_epoch']}"
    )


def _require_account(email: str, db_path: str) -> AccountState | None:
    state = find_account_state(email, db_path)
    if state is None:
        print("No account exists for that address.", file=sys.stderr)
    return state


def _change_policy_status(email: str, db_path: str, status: str) -> int:
    state = find_account_state(email, db_path)
    if state is None:
        print("No account exists for that address.", file=sys.stderr)
        return EXIT_FAILED
    try:
        updated = set_policy_status(state["id"], status, db_path=db_path)
    except (AccountTransitionError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    if updated is None:  # removed between the lookup and the update
        print("No account exists for that address.", file=sys.stderr)
        return EXIT_FAILED
    print(f"{_describe(updated)}; all earlier sessions are revoked.")
    return EXIT_OK


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
    print(f"{'status':<11}{'verified':<10}{'created':<22}{'email':<34}id role")
    users = get_all_users(db_path, include_inactive=True)
    with transaction(db_path) as conn:
        for user in users:
            verified = "yes" if has_verified_identity(conn, user["id"]) else "no"
            print(
                f"{user['status']:<11}{verified:<10}"
                f"{user['created_at'][:19]:<22}{user['email']:<34}"
                f"{user['id']} {user['role']}"
            )
    return EXIT_OK


def _command_status(db_path: str, email: str) -> int:
    print(f"identity={pseudonymise_identifier(email)}")
    state = find_account_state(email, db_path)
    print(_describe(state) if state else "account=none")
    for invitation in list_account_invitations(email, db_path):
        print(
            "invitation "
            f"status={invitation['status']} created={invitation['created_at']} "
            f"expires={invitation['expires_at']}"
        )
    return EXIT_OK


def _command_invite(db_path: str, email: str, days: int, by: str) -> int:
    if not 1 <= days <= MAX_INVITE_DAYS:
        print(f"--days must be between 1 and {MAX_INVITE_DAYS}.", file=sys.stderr)
        return EXIT_USAGE
    now = datetime.now(tz=timezone.utc)
    create_account_invitation(
        email,
        expires_at=now + timedelta(days=days),
        invited_by=by,
        db_path=db_path,
        now=now,
    )
    print(
        f"Invitation created for identity={pseudonymise_identifier(email)}; "
        f"valid for {days} day(s). Any earlier pending invitation was revoked."
    )
    return EXIT_OK


def _command_revoke_invite(db_path: str, email: str) -> int:
    revoked = revoke_account_invitations(email, db_path=db_path)
    print(f"Revoked {revoked} pending invitation(s).")
    return EXIT_OK


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
        return EXIT_FAILED
    try:
        updated = set_operator_status(users[0]["id"], status, db_path)
    except AccountError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Account is now {updated['status']}.")
    return EXIT_OK


def _command_set_role(db_path: str, email: str, role: str) -> int:
    init_db(db_path)
    canonical = normalise_email(email)
    users = [
        user
        for user in get_all_users(db_path, include_inactive=True)
        if user["email"].lower() == canonical
    ]
    if not users:
        print("No account has that email.", file=sys.stderr)
        return EXIT_FAILED
    try:
        updated = set_operator_role(users[0]["id"], AccountRole(role), db_path)
    except (AccountError, ValueError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Account role is now {updated['role']}; all earlier sessions are revoked.")
    return EXIT_OK


def _command_suspend(db_path: str, email: str) -> int:
    return _change_policy_status(email, db_path, ACCOUNT_STATUS_SUSPENDED)


def _command_reactivate(db_path: str, email: str) -> int:
    return _change_policy_status(email, db_path, ACCOUNT_STATUS_ACTIVE)


def _command_delete(db_path: str, email: str, confirmed: bool) -> int:
    if not confirmed:
        print(
            "Deleting an account is permanent and cannot be undone. "
            "Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return _change_policy_status(email, db_path, ACCOUNT_STATUS_DELETED)


def _command_revoke_sessions(db_path: str, email: str) -> int:
    state = _require_account(email, db_path)
    if state is None:
        return EXIT_FAILED
    updated = revoke_account_sessions(state["id"], db_path)
    if updated is None:
        print("No account exists for that address.", file=sys.stderr)
        return EXIT_FAILED
    print(f"{_describe(updated)}; all earlier sessions are revoked.")
    return EXIT_OK


def _command_plan(db_path: str, owner_email: str, as_json: bool) -> int:
    init_db(db_path)
    plan = plan_legacy_ownership(db_path, owner_email=owner_email)
    if as_json:
        print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    else:
        _print_plan(plan)
    return EXIT_FAILED if plan.blockers else EXIT_OK


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
        return EXIT_FAILED
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
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and administer accounts, access policy and legacy ownership."
    )
    parser.add_argument(
        "--db",
        "--database",
        dest="db",
        default=None,
        help="SQLite database path (default: $DATABASE_PATH or workout_agent.db).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="List accounts and their status.")

    status = commands.add_parser(
        "status", help="Show an address's account state and invitations."
    )
    status.add_argument("email", help="The person's email address.")

    invite = commands.add_parser(
        "invite",
        help="Invite an address (replaces any earlier pending invitation).",
    )
    invite.add_argument("email", help="The person's email address.")
    invite.add_argument("--days", type=int, default=DEFAULT_INVITE_DAYS)
    invite.add_argument("--by", default="operator-cli", help="Free-text label.")

    revoke_invite = commands.add_parser(
        "revoke-invite", help="Revoke pending invitations."
    )
    revoke_invite.add_argument("email", help="The person's email address.")

    set_status = commands.add_parser(
        "set-status", help="Suspend or reactivate an account."
    )
    set_status.add_argument("--email", required=True)
    set_status.add_argument("--status", required=True, choices=("active", "suspended"))

    set_role = commands.add_parser(
        "set-role", help="Assign an administrative role and end earlier sessions."
    )
    set_role.add_argument("--email", required=True)
    set_role.add_argument(
        "--role",
        required=True,
        choices=tuple(role.value for role in AccountRole),
    )

    suspend = commands.add_parser(
        "suspend", help="Suspend an account and end its sessions."
    )
    suspend.add_argument("email", help="The person's email address.")

    reactivate = commands.add_parser(
        "reactivate", help="Reactivate a suspended account."
    )
    reactivate.add_argument("email", help="The person's email address.")

    revoke_sessions = commands.add_parser(
        "revoke-sessions", help="Sign an account out everywhere."
    )
    revoke_sessions.add_argument("email", help="The person's email address.")

    delete = commands.add_parser(
        "delete",
        help="Permanently block an account (access tombstone; data is not erased).",
    )
    delete.add_argument("email", help="The person's email address.")
    delete.add_argument("--yes", action="store_true", help="Confirm the deletion.")

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


def _dispatch(args: argparse.Namespace, db_path: str) -> int:
    if args.command == "list":
        return _command_list(db_path)
    if args.command == "set-status":
        return _command_set_status(db_path, args.email, args.status)
    if args.command == "set-role":
        return _command_set_role(db_path, args.email, args.role)
    if args.command == "plan-legacy-assignment":
        return _command_plan(db_path, args.owner_email, args.as_json)
    if args.command == "assign-legacy-data":
        operator = args.operator or getpass.getuser()
        return _command_assign(
            db_path,
            args.owner_email,
            args.confirm_plan,
            args.backup_verified,
            operator,
        )

    canonical = canonical_email(args.email)
    if canonical is None or canonical == LEGACY_TENANT_EMAIL:
        print("That is not an acceptable email address.", file=sys.stderr)
        return EXIT_USAGE

    if args.command == "status":
        return _command_status(db_path, canonical)
    if args.command == "invite":
        return _command_invite(db_path, canonical, args.days, args.by)
    if args.command == "revoke-invite":
        return _command_revoke_invite(db_path, canonical)
    if args.command == "suspend":
        return _command_suspend(db_path, canonical)
    if args.command == "reactivate":
        return _command_reactivate(db_path, canonical)
    if args.command == "revoke-sessions":
        return _command_revoke_sessions(db_path, canonical)
    if args.command == "delete":
        return _command_delete(db_path, canonical, args.yes)
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = _database_path(args.db)
    try:
        # Validate email-shaped commands before creating or opening the database.
        if args.command in {
            "status",
            "invite",
            "revoke-invite",
            "suspend",
            "reactivate",
            "revoke-sessions",
            "delete",
        }:
            canonical = canonical_email(args.email)
            if canonical is None or canonical == LEGACY_TENANT_EMAIL:
                print("That is not an acceptable email address.", file=sys.stderr)
                return EXIT_USAGE
        init_db(db_path)
        return int(_dispatch(args, db_path))
    except sqlite3.Error as exc:
        print(f"Database error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
