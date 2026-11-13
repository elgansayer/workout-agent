"""Single-use account recovery and verified e-mail change tokens.

The application signs people in with a provider identity. This module adds the
two account actions that still need a secret delivered out of band:

* **Recovery** lets a person who can read the account's verified e-mail address
  obtain a fresh session. It does not create an account and cannot be used for a
  suspended, deleted, unclaimed or unknown account.
* **E-mail change** lets a signed-in person move the account's contact address.
  The new address is not trusted until a token sent *to that address* is
  returned, and the previous address is told about the request and the result.

Both actions use 256-bit random tokens. Only a SHA-256 digest is stored, each
token is consumed atomically, and every token expires quickly. Request attempts
are rate limited by hashed subject and source so the store cannot be used to
enumerate accounts or to mail-bomb an address.

This module never logs a token or an address. Callers deliver the token by
e-mail; the database only ever holds the digest and the addresses already stored
on the account.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import email_delivery
from account_policy import canonical_email
from database import (
    _USER_COLUMNS,
    DEFAULT_DB_PATH,
    UserRow,
    _user_from_row,
    transaction,
)
from tenancy import LEGACY_TENANT_EMAIL

logger = logging.getLogger(__name__)

PURPOSE_RECOVERY = "recovery"
PURPOSE_EMAIL_CHANGE = "email_change"
_TOKEN_BYTES = 32

# Short by design. A person has time to open the message, but a leaked mailbox
# message is not a long-lived credential.
RECOVERY_TOKEN_TTL = timedelta(hours=1)
EMAIL_CHANGE_TOKEN_TTL = timedelta(hours=1)
# A change of contact address is only allowed from a session that was itself
# established recently. This is the "recent authentication" requirement.
RECENT_AUTHENTICATION_WINDOW = timedelta(minutes=15)

_REQUEST_WINDOW = timedelta(hours=1)
_MAX_REQUESTS_PER_SUBJECT = 3
_MAX_REQUESTS_PER_SOURCE = 10

EmailSender = Callable[[str, str, str], None]


class AccountActionError(RuntimeError):
    """Base class for account recovery/e-mail change failures."""


class InvalidTokenError(AccountActionError):
    """The token is unknown, expired, already used, or no longer applicable."""


class AccountActionRateLimited(AccountActionError):
    """Too many requests have been made for this subject or source."""


class RecentAuthenticationRequired(AccountActionError):
    """The session is too old for this security-sensitive change."""


class EmailChangeRejected(AccountActionError):
    """The requested new address cannot be used."""


class EmailDeliveryFailed(AccountActionError):
    """The token could not be delivered, so the action was not started."""


@dataclass(frozen=True, slots=True)
class EmailChangeResult:
    """The account after a completed change and the address it replaced."""

    user: UserRow
    previous_email: str


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the additive, idempotent token and request-rate-limit tables."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_action_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            purpose TEXT NOT NULL
                CHECK (purpose IN ('recovery', 'email_change')),
            target_email TEXT NOT NULL,
            previous_email TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_action_tokens_user "
        "ON account_action_tokens (user_id, purpose, used_at, expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_action_tokens_expiry "
        "ON account_action_tokens (expires_at)"
    )
    # Request attempts are recorded for every address, including unknown ones,
    # so rate limiting cannot be used as an account-existence oracle. Only
    # keyed digests are stored, never the address or the source in clear text.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_action_request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL
                CHECK (purpose IN ('recovery', 'email_change')),
            subject_hash TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_action_requests_subject "
        "ON account_action_request_log (purpose, subject_hash, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_action_requests_source "
        "ON account_action_request_log (purpose, source_hash, created_at)"
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_digest(token: str) -> str:
    return _digest(f"workout-agent/token/v1/{token}")


def _subject_digest(purpose: str, subject: str) -> str:
    return _digest(f"workout-agent/request/{purpose}/subject/{subject}")


def _source_digest(purpose: str, source: str) -> str:
    return _digest(f"workout-agent/request/{purpose}/source/{source}")


def _epoch(moment: datetime | None) -> int:
    value = moment or datetime.now(tz=timezone.utc)
    if value.tzinfo is None:
        raise ValueError("account action times must be timezone-aware")
    return int(value.timestamp())


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _load_user(conn: sqlite3.Connection, user_id: str) -> UserRow | None:
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return _user_from_row(row) if row is not None else None


def _record_request(
    *,
    db_path: str,
    purpose: str,
    subject: str,
    source: str,
    now: int,
) -> None:
    """Enforce the request limits and record this attempt in its own commit.

    The attempt is committed even when the action that follows is rejected (an
    unknown address, a suspended account, an invalid new address). Otherwise a
    caller could retry rejected actions without ever hitting the limit.
    """

    window_start = now - int(_REQUEST_WINDOW.total_seconds())
    subject_hash = _subject_digest(purpose, subject)
    source_hash = _source_digest(purpose, source)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Keep the log bounded; one day is enough to enforce an hourly window.
        conn.execute(
            "DELETE FROM account_action_request_log WHERE created_at < ?",
            (now - 24 * 60 * 60,),
        )
        subject_count = conn.execute(
            "SELECT COUNT(*) FROM account_action_request_log "
            "WHERE purpose = ? AND subject_hash = ? AND created_at > ?",
            (purpose, subject_hash, window_start),
        ).fetchone()[0]
        source_count = conn.execute(
            "SELECT COUNT(*) FROM account_action_request_log "
            "WHERE purpose = ? AND source_hash = ? AND created_at > ?",
            (purpose, source_hash, window_start),
        ).fetchone()[0]
        if subject_count >= _MAX_REQUESTS_PER_SUBJECT:
            raise AccountActionRateLimited("Too many requests for this address.")
        if source_count >= _MAX_REQUESTS_PER_SOURCE:
            raise AccountActionRateLimited("Too many requests from this source.")
        conn.execute(
            "INSERT INTO account_action_request_log "
            "(purpose, subject_hash, source_hash, created_at) VALUES (?, ?, ?, ?)",
            (purpose, subject_hash, source_hash, now),
        )


def _issue_token(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    purpose: str,
    target_email: str,
    previous_email: str | None,
    now: int,
    ttl: timedelta,
) -> str:
    # A new request supersedes any earlier unused token for the same action, so
    # an old message cannot stay live after a newer one is sent.
    conn.execute(
        "UPDATE account_action_tokens SET used_at = ? "
        "WHERE user_id = ? AND purpose = ? AND used_at IS NULL",
        (now, user_id, purpose),
    )
    conn.execute(
        "DELETE FROM account_action_tokens WHERE expires_at <= ?",
        (now,),
    )
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    conn.execute(
        "INSERT INTO account_action_tokens "
        "(token_hash, user_id, purpose, target_email, previous_email, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _token_digest(token),
            user_id,
            purpose,
            target_email,
            previous_email,
            now,
            now + int(ttl.total_seconds()),
        ),
    )
    return token


def _discard_token(db_path: str, token: str) -> None:
    with transaction(db_path) as conn:
        conn.execute(
            "DELETE FROM account_action_tokens WHERE token_hash = ?",
            (_token_digest(token),),
        )


def _send(sender: EmailSender | None, to: str, subject: str, body: str) -> None:
    (sender or email_delivery.send_email)(to, subject, body)


def _action_link(public_url: str, token: str, *, kind: str | None = None) -> str:
    base = (public_url or "").rstrip("/")
    suffix = f"&kind={quote(kind)}" if kind else ""
    return f"{base}/recover?token={quote(token)}{suffix}"


def request_recovery(
    *,
    email: str,
    source: str,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
    sender: EmailSender | None = None,
    public_url: str = "",
    allowed: bool = True,
) -> bool:
    """Issue a recovery token for *email* if it belongs to an active account.

    Returns whether a token was issued. Unknown, malformed, inactive and
    disallowed addresses follow the same path (a recorded attempt and a normal
    return) so the caller can answer identically for all of them.
    """

    moment = _epoch(now)
    canonical = canonical_email(email)
    subject = canonical or str(email).strip().lower()[:254]
    should_send = False
    token = ""

    _record_request(
        db_path=db_path,
        purpose=PURPOSE_RECOVERY,
        subject=subject,
        source=source,
        now=moment,
    )
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if canonical is None or canonical == LEGACY_TENANT_EMAIL or not allowed:
            return False
        row = conn.execute(
            "SELECT id, email, status FROM users WHERE lower(email) = ?",
            (canonical,),
        ).fetchone()
        if row is None or row[2] != "active":
            return False
        token = _issue_token(
            conn,
            user_id=str(row[0]),
            purpose=PURPOSE_RECOVERY,
            target_email=canonical,
            previous_email=None,
            now=moment,
            ttl=RECOVERY_TOKEN_TTL,
        )
        should_send = True

    if should_send:
        link = _action_link(public_url, token)
        try:
            _send(
                sender,
                canonical,
                "Recover your Workout Agent account",
                (
                    "A recovery was requested for your Workout Agent account.\n\n"
                    f"Open this single-use link within one hour:\n{link}\n\n"
                    "If you did not request this, you can ignore this message; "
                    "your account has not been changed."
                ),
            )
        except Exception as exc:
            _discard_token(db_path, token)
            logger.warning(
                "Recovery token delivery failed (%s); token discarded.",
                type(exc).__name__,
            )
            raise EmailDeliveryFailed("Recovery e-mail could not be delivered.") from exc
    return should_send


def complete_recovery(
    *,
    token: str,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> UserRow:
    """Consume a recovery token and return its active account.

    The token is marked used in the same transaction that validates it, so a
    concurrent replay can win at most once. Any inapplicable token (unknown,
    expired, used, for another purpose, for a changed address, or for an account
    that is no longer active) raises the same non-enumerating error.
    """

    if not isinstance(token, str) or not token or len(token) > 512:
        raise InvalidTokenError("Invalid or expired recovery token.")
    moment = _epoch(now)
    recovered: UserRow | None = None
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, purpose, target_email, expires_at, used_at "
            "FROM account_action_tokens WHERE token_hash = ?",
            (_token_digest(token),),
        ).fetchone()
        if row is None:
            raise InvalidTokenError("Invalid or expired recovery token.")
        user_id, purpose, target_email, expires_at, used_at = row
        if (
            purpose != PURPOSE_RECOVERY
            or used_at is not None
            or int(expires_at) <= moment
        ):
            raise InvalidTokenError("Invalid or expired recovery token.")
        consumed = conn.execute(
            "UPDATE account_action_tokens SET used_at = ? "
            "WHERE token_hash = ? AND used_at IS NULL",
            (moment, _token_digest(token)),
        )
        if consumed.rowcount != 1:
            raise InvalidTokenError("Invalid or expired recovery token.")
        user = _load_user(conn, str(user_id))
        if (
            user is not None
            and user["status"] == "active"
            and str(user["email"]).lower() == str(target_email)
        ):
            recovered = user
    # The token is consumed even when the account is no longer eligible, so a
    # failed attempt cannot be replayed after a status change or reactivation.
    if recovered is None:
        raise InvalidTokenError("Invalid or expired recovery token.")
    return recovered


def request_email_change(
    *,
    user_id: str,
    new_email: str,
    authenticated_at: datetime | None,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
    sender: EmailSender | None = None,
    public_url: str = "",
    new_email_allowed: bool = True,
) -> None:
    """Start a verified change of the account's contact e-mail address.

    Requires a session authenticated within ``RECENT_AUTHENTICATION_WINDOW``.
    A token is mailed to the *new* address and a warning to the *previous* one.
    The account is not changed until :func:`confirm_email_change`.
    """

    moment_dt = now or datetime.now(tz=timezone.utc)
    moment = _epoch(moment_dt)
    if (
        authenticated_at is None
        or authenticated_at.tzinfo is None
        or moment_dt - authenticated_at > RECENT_AUTHENTICATION_WINDOW
        or authenticated_at > moment_dt
    ):
        raise RecentAuthenticationRequired(
            "Sign in again before changing the account e-mail address."
        )

    canonical = canonical_email(new_email)
    if canonical is None or canonical == LEGACY_TENANT_EMAIL or not new_email_allowed:
        raise EmailChangeRejected("That e-mail address cannot be used.")

    token = ""
    previous_email = ""
    _record_request(
        db_path=db_path,
        purpose=PURPOSE_EMAIL_CHANGE,
        subject=user_id,
        source=user_id,
        now=moment,
    )
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = _load_user(conn, user_id)
        if user is None or user["status"] != "active":
            raise EmailChangeRejected("That e-mail address cannot be used.")
        previous_email = str(user["email"])
        if canonical == previous_email.lower():
            raise EmailChangeRejected("That is already your account e-mail address.")
        clash = conn.execute(
            "SELECT 1 FROM users WHERE lower(email) = ? AND id <> ?",
            (canonical, user_id),
        ).fetchone()
        if clash is not None:
            raise EmailChangeRejected("That e-mail address cannot be used.")
        token = _issue_token(
            conn,
            user_id=user_id,
            purpose=PURPOSE_EMAIL_CHANGE,
            target_email=canonical,
            previous_email=previous_email,
            now=moment,
            ttl=EMAIL_CHANGE_TOKEN_TTL,
        )

    link = _action_link(public_url, token, kind="email_change")
    try:
        _send(
            sender,
            canonical,
            "Confirm your new Workout Agent e-mail address",
            (
                "Confirm this address for your Workout Agent account.\n\n"
                f"Open this single-use link within one hour:\n{link}\n\n"
                "If you did not request this, do not open the link."
            ),
        )
        _send(
            sender,
            previous_email,
            "Workout Agent e-mail change requested",
            (
                "Someone requested to change the e-mail address on your "
                "Workout Agent account.\n\n"
                "The new address will only take effect after it is verified. "
                "If this was not you, sign in and review your account."
            ),
        )
    except Exception as exc:
        _discard_token(db_path, token)
        logger.warning(
            "E-mail change token delivery failed (%s); token discarded.",
            type(exc).__name__,
        )
        raise EmailDeliveryFailed("E-mail could not be delivered.") from exc


def confirm_email_change(
    *,
    token: str,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
    sender: EmailSender | None = None,
) -> EmailChangeResult:
    """Consume an e-mail change token and apply the verified address.

    The change bumps ``session_epoch`` so every session issued under the old
    address fails closed; the caller should also revoke the registry rows.
    A final warning is sent to the previous address after the commit.
    """

    if not isinstance(token, str) or not token or len(token) > 512:
        raise InvalidTokenError("Invalid or expired e-mail change token.")
    moment = _epoch(now)
    previous_email = ""
    canonical = ""
    updated: UserRow | None = None
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, purpose, target_email, previous_email, "
            "expires_at, used_at FROM account_action_tokens WHERE token_hash = ?",
            (_token_digest(token),),
        ).fetchone()
        if row is None:
            raise InvalidTokenError("Invalid or expired e-mail change token.")
        user_id, purpose, target_email, token_previous, expires_at, used_at = row
        if (
            purpose != PURPOSE_EMAIL_CHANGE
            or used_at is not None
            or int(expires_at) <= moment
        ):
            raise InvalidTokenError("Invalid or expired e-mail change token.")
        consumed = conn.execute(
            "UPDATE account_action_tokens SET used_at = ? "
            "WHERE token_hash = ? AND used_at IS NULL",
            (moment, _token_digest(token)),
        )
        if consumed.rowcount != 1:
            raise InvalidTokenError("Invalid or expired e-mail change token.")
        user = _load_user(conn, str(user_id))
        if (
            user is not None
            and user["status"] == "active"
            and str(user["email"]).lower() == str(token_previous).lower()
        ):
            previous_email = str(user["email"])
            proposed = canonical_email(str(target_email))
            if proposed and proposed != LEGACY_TENANT_EMAIL:
                clash = conn.execute(
                    "SELECT 1 FROM users WHERE lower(email) = ? AND id <> ?",
                    (proposed, user_id),
                ).fetchone()
                if clash is None:
                    try:
                        conn.execute(
                            "UPDATE users SET email = ?, updated_at = ?, "
                            "session_epoch = session_epoch + 1 WHERE id = ?",
                            (proposed, _iso(moment), user_id),
                        )
                    except sqlite3.IntegrityError:
                        updated = None
                    else:
                        canonical = proposed
                        updated = _load_user(conn, str(user_id))

    # The token is consumed even when the change no longer applies; a later
    # status/address change must not make an old link live again.
    if updated is None or not canonical:
        raise InvalidTokenError("Invalid or expired e-mail change token.")

    try:
        _send(
            sender,
            previous_email,
            "Your Workout Agent e-mail address was changed",
            (
                "The e-mail address on your Workout Agent account was changed "
                f"to {canonical}.\n\n"
                "All existing sessions were ended. If you did not make this "
                "change, contact the person who runs this deployment."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - the change is already committed
        logger.warning(
            "Previous-address e-mail change alert failed (%s).",
            type(exc).__name__,
        )
    return EmailChangeResult(user=updated, previous_email=previous_email)


__all__ = [
    "EMAIL_CHANGE_TOKEN_TTL",
    "RECENT_AUTHENTICATION_WINDOW",
    "RECOVERY_TOKEN_TTL",
    "AccountActionError",
    "AccountActionRateLimited",
    "EmailChangeRejected",
    "EmailChangeResult",
    "EmailDeliveryFailed",
    "InvalidTokenError",
    "RecentAuthenticationRequired",
    "complete_recovery",
    "confirm_email_change",
    "ensure_schema",
    "request_email_change",
    "request_recovery",
]
