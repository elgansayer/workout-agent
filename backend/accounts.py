"""Account identity and lifecycle for the production user model.

An account (``users`` row) has an immutable identifier, a lifecycle ``status`` and
one canonical external identity per provider (``user_identities``). Email is a
mutable attribute, never the key: a sign-in is matched on the provider's stable
``(provider, subject)`` pair, and an email address is only ever used to adopt a
pre-identity account once, when the provider has verified it.

Registration is part of the same decision as sign-in: ``admit_oauth_identity``
resolves the identity, creates the account when the deployment allows it and
spends the matching invitation in one write transaction, so a refused sign-in
never leaves an account, an identity or a spent invitation behind.

Only ``active`` accounts may authenticate or be scheduled. Errors deliberately
carry no email address or provider subject so they are safe to log.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from database import (
    _USER_COLUMNS,
    DEFAULT_DB_PATH,
    TERMINAL_ACCOUNT_STATUSES,
    UserRow,
    _now_iso,
    _user_from_row,
    accept_invitation,
    find_pending_invitation,
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
    """The account exists but its status does not permit the operation.

    ``user_id`` is the opaque account id (never an address), so callers can
    correlate a refusal in the log.
    """

    def __init__(self, status: str, user_id: str | None = None) -> None:
        super().__init__(f"Account is {status}.")
        self.status = status
        self.user_id = user_id


class AccountNotInvitedError(AccountError):
    """A new account was refused because the deployment needs an invitation."""


class IdentityVerificationError(AccountError):
    """The identity provider did not supply verifiable identity claims."""


class IdentityConflictError(AccountError):
    """The identity cannot be bound without risking an account takeover."""


def _require_active(user: UserRow) -> None:
    if user["status"] != AccountStatus.ACTIVE:
        raise AccountInactiveError(user["status"], user["id"])


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


@dataclass(frozen=True, slots=True)
class IdentityAdmission:
    """An admitted sign-in: the account, its session epoch and whether it is new.

    ``session_epoch`` is read in the admitting transaction so the session that
    is issued next carries exactly the epoch the account had when it was
    admitted (see ``account_policy``). ``created`` is true only for the sign-in
    that registered the account.
    """

    user: UserRow
    session_epoch: int
    created: bool


def _admission(conn: sqlite3.Connection, user_id: str, *, created: bool) -> IdentityAdmission:
    user = _load(conn, user_id)
    epoch = conn.execute(
        "SELECT session_epoch FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return IdentityAdmission(user=user, session_epoch=int(epoch[0]), created=created)


def admit_oauth_identity(
    *,
    provider: str,
    subject: str,
    email: str,
    email_verified: bool,
    display_name: str | None = None,
    require_invitation: bool = False,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> IdentityAdmission:
    """Resolve, and if the deployment permits create, the account for a sign-in.

    Resolution order, all inside one ``BEGIN IMMEDIATE`` transaction so that
    concurrent or replayed callbacks cannot create two accounts or spend one
    invitation twice, and a refusal changes nothing:

    1. ``(provider, subject)`` already bound: that account, if it is active. The
       provider's stable subject selects the account, so an address changed at
       the provider keeps the same account (the account's own address is not
       rewritten; only the identity's last-seen address is refreshed).
    2. Otherwise an account already holding the (verified) email and no identity
       for this provider adopts the identity. This is the one-time upgrade path
       for accounts created before identities existed.
    3. Otherwise a new ``active`` account and identity are created, and when
       ``require_invitation`` is set exactly one pending, unexpired invitation
       for the address is spent in the same transaction.

    A verified email that belongs to an account already bound to a *different*
    subject for the same provider is refused: the address may have been
    reassigned at the provider, and binding it would hand over the account. An
    operator who has confirmed the person can re-bind the account explicitly
    (``unlink_provider_identity``).

    Raises:
        IdentityVerificationError: missing subject/email, the reserved
            placeholder address, or an email the provider did not verify.
        IdentityConflictError: see above, or the address matches several
            accounts.
        AccountInactiveError: the matched account is not ``active``.
        AccountNotInvitedError: a new account is needed but no invitation is
            pending for the address.
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

    moment = datetime.now(tz=timezone.utc) if now is None else now
    if moment.tzinfo is None:
        raise ValueError("A timezone-aware datetime is required.")
    stamp = moment.astimezone(timezone.utc).isoformat()
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
                (email_key, stamp, provider_key, subject_key),
            )
            _record_login(conn, user["id"], stamp)
            return _admission(conn, user["id"], created=False)

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
        created = False
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
        else:
            invitation_id: str | None = None
            if require_invitation:
                invitation_id = find_pending_invitation(conn, email_key, moment)
                if invitation_id is None:
                    raise AccountNotInvitedError(
                        "No invitation is pending for that address."
                    )
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users "
                "(id, email, display_name, created_at, status_changed_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, email_key, display_name, stamp, stamp, stamp),
            )
            if invitation_id is not None:
                accept_invitation(conn, invitation_id, user_id, moment)
            created = True
        conn.execute(
            "INSERT INTO user_identities "
            "(user_id, provider, subject, email, email_verified, created_at, "
            "last_login_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (user_id, provider_key, subject_key, email_key, stamp, stamp),
        )
        _record_login(conn, user_id, stamp)
        return _admission(conn, user_id, created=created)


def resolve_oauth_user(
    *,
    provider: str,
    subject: str,
    email: str,
    email_verified: bool,
    display_name: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> UserRow:
    """Return the active account for a verified sign-in on an open deployment.

    The same resolution as :func:`admit_oauth_identity` with no invitation
    requirement. The web login does not use it (it asks the access policy, which
    knows whether the deployment is invite-only); it is the plain identity
    resolver for provisioning tools and tests.
    """

    return admit_oauth_identity(
        provider=provider,
        subject=subject,
        email=email,
        email_verified=email_verified,
        display_name=display_name,
        db_path=db_path,
    ).user


def unlink_provider_identity(
    user_id: str, provider: str, db_path: str = DEFAULT_DB_PATH
) -> bool:
    """Forget an account's identity for ``provider`` so it can be re-bound.

    This is the operator-confirmed way out of an identity conflict: a person
    whose provider account was recreated has a new stable subject, and the
    address alone is never enough to move an account to it. The account keeps
    its data and its address; its next verified sign-in for that address binds
    the new subject (the one-time adoption in :func:`admit_oauth_identity`).
    Every session the account holds ends now, because the session epoch
    advances. Terminal accounts cannot be re-bound.

    Returns whether an identity was removed.

    Raises:
        AccountNotFoundError: *user_id* matches no account.
        AccountError: the account is retired or deleted.
    """

    provider_key = provider.strip().lower()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = _load(conn, user_id)
        if user["status"] in TERMINAL_ACCOUNT_STATUSES:
            raise AccountError(f"A {user['status']} account cannot be re-bound.")
        removed = conn.execute(
            "DELETE FROM user_identities WHERE user_id = ? AND provider = ?",
            (user_id, provider_key),
        ).rowcount
        if removed:
            now = _now_iso()
            conn.execute(
                "UPDATE users SET session_epoch = session_epoch + 1, updated_at = ? "
                "WHERE id = ?",
                (now, user_id),
            )
        return bool(removed)


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
    "AccountNotInvitedError",
    "AccountStatus",
    "IdentityAdmission",
    "IdentityConflictError",
    "IdentityVerificationError",
    "admit_oauth_identity",
    "has_verified_identity",
    "require_active_user",
    "resolve_oauth_user",
    "set_account_status",
    "unlink_provider_identity",
]
