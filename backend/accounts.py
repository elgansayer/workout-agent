"""Account identity and lifecycle for the production user model.

An account (``users`` row) has an immutable identifier, a lifecycle ``status`` and
one canonical external identity per provider (``user_identities``). Email is a
mutable attribute, never the key: a sign-in is matched on the provider's stable
``(provider, subject)`` pair, and an email address is only ever used to adopt a
pre-identity account once, when the provider has verified it.

Only ``active`` accounts may authenticate or be scheduled. Errors deliberately
carry no email address or provider subject so they are safe to log.
"""

from __future__ import annotations

import sqlite3
import uuid
from enum import StrEnum

from database import (
    _USER_COLUMNS,
    DEFAULT_DB_PATH,
    TERMINAL_ACCOUNT_STATUSES,
    UserRow,
    _now_iso,
    _user_from_row,
    get_user_by_id,
    transaction,
)
from tenancy import LEGACY_TENANT_EMAIL, normalise_email


class AccountStatus(StrEnum):
    """Lifecycle of an account (mirrors ``database.ACCOUNT_STATUSES``)."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    #: Placeholder holding pre-account data until it is assigned to a verified
    #: owner. Never authenticates and is never scheduled.
    UNCLAIMED = "unclaimed"
    #: The placeholder after its data has been assigned. Terminal.
    RETIRED = "retired"
    #: Reserved for the account-deletion workflow. Terminal.
    DELETED = "deleted"


#: Statuses an operator may toggle between. The others are owned by system
#: flows (schema upgrade, ownership assignment, account deletion).
OPERATOR_STATUSES = frozenset({AccountStatus.ACTIVE, AccountStatus.SUSPENDED})


class AccountError(RuntimeError):
    """Base class for account-model failures."""


class AccountNotFoundError(AccountError):
    """The referenced account does not exist."""


class AccountInactiveError(AccountError):
    """The account exists but its status does not permit the operation."""

    def __init__(self, status: str) -> None:
        super().__init__(f"Account is {status}.")
        self.status = status


class IdentityVerificationError(AccountError):
    """The identity provider did not supply verifiable identity claims."""


class IdentityConflictError(AccountError):
    """The identity cannot be bound without risking an account takeover."""


def _require_active(user: UserRow) -> None:
    if user["status"] != AccountStatus.ACTIVE:
        raise AccountInactiveError(user["status"])


def _load(conn: sqlite3.Connection, user_id: str) -> UserRow:
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise AccountNotFoundError("No such account.")
    return _user_from_row(row)


def _record_login(conn: sqlite3.Connection, user_id: str, now: str) -> None:
    conn.execute(
        "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
        (now, now, user_id),
    )


def resolve_oauth_user(
    *,
    provider: str,
    subject: str,
    email: str,
    email_verified: bool,
    display_name: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> UserRow:
    """Return the active account for a verified external sign-in.

    Resolution order, all inside one write transaction:

    1. ``(provider, subject)`` already bound: that account, if it is active.
    2. Otherwise an account already holding the (verified) email and no identity
       for this provider adopts the identity. This is the one-time upgrade path
       for accounts created before identities existed.
    3. Otherwise a new ``active`` account and identity are created.

    A verified email that belongs to an account already bound to a *different*
    subject for the same provider is refused: the address may have been
    reassigned at the provider, and binding it would hand over the account.

    Raises:
        IdentityVerificationError: missing subject/email, the reserved
            placeholder address, or an email the provider did not verify.
        IdentityConflictError: see above.
        AccountInactiveError: the matched account is not ``active``.
    """

    provider_key = provider.strip().lower()
    subject_key = subject.strip()
    email_key = normalise_email(email)
    if not provider_key or not subject_key:
        raise IdentityVerificationError(
            "The identity provider did not supply a stable subject."
        )
    if "@" not in email_key:
        raise IdentityVerificationError(
            "The identity provider did not supply an email address."
        )
    if email_key == LEGACY_TENANT_EMAIL:
        raise IdentityVerificationError("That email address is reserved.")
    if email_verified is not True:
        raise IdentityVerificationError(
            "The identity provider has not verified the email address."
        )

    now = _now_iso()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        bound = conn.execute(
            "SELECT user_id FROM user_identities WHERE provider = ? AND subject = ?",
            (provider_key, subject_key),
        ).fetchone()
        if bound is not None:
            user = _load(conn, bound[0])
            _require_active(user)
            conn.execute(
                "UPDATE user_identities SET email = ?, email_verified = 1, "
                "last_login_at = ? WHERE provider = ? AND subject = ?",
                (email_key, now, provider_key, subject_key),
            )
            conn.execute(
                "UPDATE users SET email_verified = 1, "
                "email_verified_at = COALESCE(email_verified_at, ?) "
                "WHERE id = ?",
                (now, user["id"]),
            )
            _record_login(conn, user["id"], now)
            return _load(conn, user["id"])

        matches = conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE lower(email) = ?",
            (email_key,),
        ).fetchall()
        if len(matches) > 1:
            # Only possible in a database that predates the case-insensitive
            # unique index. Picking either row could hand over the wrong account.
            raise IdentityConflictError(
                "That email address matches more than one account."
            )
        if matches:
            user = _user_from_row(matches[0])
            _require_active(user)
            already_bound = conn.execute(
                "SELECT 1 FROM user_identities WHERE user_id = ? AND provider = ?",
                (user["id"], provider_key),
            ).fetchone()
            if already_bound is not None:
                raise IdentityConflictError(
                    "That email address belongs to an account bound to a "
                    "different identity."
                )
            user_id = user["id"]
            conn.execute(
                "UPDATE users SET email_verified = 1, "
                "email_verified_at = COALESCE(email_verified_at, ?) "
                "WHERE id = ?",
                (now, user_id),
            )
        else:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users "
                "(id, email, display_name, created_at, status_changed_at, "
                "email_verified, email_verified_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (user_id, email_key, display_name, now, now, now, now),
            )
        conn.execute(
            "INSERT INTO user_identities "
            "(user_id, provider, subject, email, email_verified, created_at, "
            "last_login_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (user_id, provider_key, subject_key, email_key, now, now),
        )
        _record_login(conn, user_id, now)
        return _load(conn, user_id)


def require_active_user(user_id: str, db_path: str = DEFAULT_DB_PATH) -> UserRow:
    """Return the account for *user_id* or raise if it may not act.

    The check every authenticated entry point (request session, scheduled run,
    CLI) applies before touching user-owned data.

    Raises:
        AccountNotFoundError: *user_id* is empty or matches no account.
        AccountInactiveError: the account exists but is not ``active``.
    """

    if not isinstance(user_id, str) or not user_id.strip():
        raise AccountNotFoundError("A user id is required.")
    user = get_user_by_id(user_id, db_path)
    if user is None:
        raise AccountNotFoundError("No such account.")
    _require_active(user)
    return user


def has_verified_identity(conn: sqlite3.Connection, user_id: str) -> bool:
    """Return whether *user_id* has authenticated through a verified identity."""

    return (
        conn.execute(
            "SELECT 1 FROM user_identities "
            "WHERE user_id = ? AND email_verified = 1 LIMIT 1",
            (user_id,),
        ).fetchone()
        is not None
    )


def set_account_status(
    user_id: str,
    status: AccountStatus | str,
    db_path: str = DEFAULT_DB_PATH,
) -> UserRow:
    """Suspend or reactivate an account (the operator-controlled transitions).

    ``unclaimed``, ``retired`` and ``deleted`` are owned by system flows and are
    refused here. Terminal accounts never change status, and the legacy
    placeholder is managed exclusively by the ownership migration, so it can
    never be activated (or otherwise altered) from here.
    """

    try:
        target = AccountStatus(status)
    except ValueError as exc:
        raise AccountError(f"Unknown account status {status!r}.") from exc
    if target not in OPERATOR_STATUSES:
        raise AccountError(f"Status {target.value!r} cannot be set directly.")

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = _load(conn, user_id)
        if user["status"] == target:
            return user
        if user["status"] in TERMINAL_ACCOUNT_STATUSES:
            raise AccountError(f"A {user['status']} account cannot change status.")
        if user["email"] == LEGACY_TENANT_EMAIL:
            raise AccountError(
                "The legacy placeholder account is managed by the ownership "
                "migration."
            )
        now = _now_iso()
        conn.execute(
            "UPDATE users SET status = ?, status_changed_at = ?, updated_at = ?, "
            "session_epoch = session_epoch + 1 WHERE id = ?",
            (target.value, now, now, user_id),
        )
        return _load(conn, user_id)


__all__ = [
    "OPERATOR_STATUSES",
    "AccountError",
    "AccountInactiveError",
    "AccountNotFoundError",
    "AccountStatus",
    "IdentityConflictError",
    "IdentityVerificationError",
    "has_verified_identity",
    "require_active_user",
    "resolve_oauth_user",
    "set_account_status",
]
