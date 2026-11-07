"""Operator CLI for account access: invitations, suspension, deletion, sign-out.

Run from the application root (``/app`` in the container, ``backend/`` in a
checkout) against the database the web service uses::

    python scripts/manage_accounts.py status person@example.com
    python scripts/manage_accounts.py invite person@example.com --days 7
    python scripts/manage_accounts.py revoke-invite person@example.com
    python scripts/manage_accounts.py suspend person@example.com
    python scripts/manage_accounts.py reactivate person@example.com
    python scripts/manage_accounts.py revoke-sessions person@example.com
    python scripts/manage_accounts.py delete person@example.com --yes

Every state change takes effect on the account's very next web request: the
server re-reads this state on each request and every status change also
advances the account's session epoch, ending all of its signed sessions. Nothing
here prints a secret; accounts are identified by opaque id and pseudonymous tag.

``delete`` is an *access* tombstone. It permanently blocks sign-in and ends
sessions but does not erase the account's data or stop background jobs; that
belongs to the account-deletion and suspension-lifecycle work.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow the script to find sibling modules when run as ``scripts/<name>.py``.
_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from account_policy import canonical_email
from data_classification import pseudonymise_identifier
from database import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_DELETED,
    ACCOUNT_STATUS_SUSPENDED,
    AccountState,
    AccountTransitionError,
    create_account_invitation,
    find_account_state,
    init_db,
    list_account_invitations,
    revoke_account_invitations,
    revoke_account_sessions,
    set_account_status,
)

DEFAULT_INVITE_DAYS = 7
MAX_INVITE_DAYS = 90

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


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


def _cmd_status(email: str, args: argparse.Namespace) -> int:
    print(f"identity={pseudonymise_identifier(email)}")
    state = find_account_state(email, args.db)
    print(_describe(state) if state else "account=none")
    for invitation in list_account_invitations(email, args.db):
        print(
            "invitation "
            f"status={invitation['status']} created={invitation['created_at']} "
            f"expires={invitation['expires_at']}"
        )
    return EXIT_OK


def _cmd_invite(email: str, args: argparse.Namespace) -> int:
    if not 1 <= args.days <= MAX_INVITE_DAYS:
        print(f"--days must be between 1 and {MAX_INVITE_DAYS}.", file=sys.stderr)
        return EXIT_USAGE
    now = datetime.now(tz=timezone.utc)
    create_account_invitation(
        email,
        expires_at=now + timedelta(days=args.days),
        invited_by=args.by,
        db_path=args.db,
        now=now,
    )
    print(
        f"Invitation created for identity={pseudonymise_identifier(email)}; "
        f"valid for {args.days} day(s). Any earlier pending invitation was revoked."
    )
    return EXIT_OK


def _cmd_revoke_invite(email: str, args: argparse.Namespace) -> int:
    revoked = revoke_account_invitations(email, db_path=args.db)
    print(f"Revoked {revoked} pending invitation(s).")
    return EXIT_OK


def _change_status(email: str, args: argparse.Namespace, status: str) -> int:
    state = find_account_state(email, args.db)
    if state is None:
        print("No account exists for that address.", file=sys.stderr)
        return EXIT_FAILED
    try:
        updated = set_account_status(state["id"], status, db_path=args.db)
    except AccountTransitionError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    if updated is None:  # removed between the lookup and the update
        print("No account exists for that address.", file=sys.stderr)
        return EXIT_FAILED
    print(f"{_describe(updated)}; all earlier sessions are revoked.")
    return EXIT_OK


def _cmd_suspend(email: str, args: argparse.Namespace) -> int:
    return _change_status(email, args, ACCOUNT_STATUS_SUSPENDED)


def _cmd_reactivate(email: str, args: argparse.Namespace) -> int:
    return _change_status(email, args, ACCOUNT_STATUS_ACTIVE)


def _cmd_delete(email: str, args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "Deleting an account is permanent and cannot be undone. "
            "Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return _change_status(email, args, ACCOUNT_STATUS_DELETED)


def _cmd_revoke_sessions(email: str, args: argparse.Namespace) -> int:
    state = _require_account(email, args.db)
    if state is None:
        return EXIT_FAILED
    updated = revoke_account_sessions(state["id"], args.db)
    if updated is None:
        print("No account exists for that address.", file=sys.stderr)
        return EXIT_FAILED
    print(f"{_describe(updated)}; all earlier sessions are revoked.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage account access for the Workout Agent web service."
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("DATABASE_PATH", "workout_agent.db").strip(),
        help="SQLite database path (default: $DATABASE_PATH or workout_agent.db).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add(
        name: str,
        handler: Callable[[str, argparse.Namespace], int],
        help_text: str,
    ) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.add_argument("email", help="The person's email address.")
        sub.set_defaults(handler=handler)
        return sub

    add("status", _cmd_status, "Show an address's account state and invitations.")
    invite = add(
        "invite",
        _cmd_invite,
        "Invite an address (replaces any earlier pending invitation).",
    )
    invite.add_argument("--days", type=int, default=DEFAULT_INVITE_DAYS)
    invite.add_argument("--by", default="operator-cli", help="Free-text label.")
    add("revoke-invite", _cmd_revoke_invite, "Revoke pending invitations.")
    add("suspend", _cmd_suspend, "Suspend an account and end its sessions.")
    add("reactivate", _cmd_reactivate, "Reactivate a suspended account.")
    add("revoke-sessions", _cmd_revoke_sessions, "Sign an account out everywhere.")
    delete = add(
        "delete",
        _cmd_delete,
        "Permanently block an account (access tombstone; data is not erased).",
    )
    delete.add_argument("--yes", action="store_true", help="Confirm the deletion.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    email = canonical_email(args.email)
    if email is None:
        print("That is not an acceptable email address.", file=sys.stderr)
        return EXIT_USAGE
    try:
        init_db(args.db)
        return int(args.handler(email, args))
    except sqlite3.Error as exc:
        print(f"Database error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
