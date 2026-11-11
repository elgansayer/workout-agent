"""Account recovery, verified email changes and identity binding.

Recovery is deliberately passwordless: a verified account can request a
single-use link by email. Tokens are high-entropy, stored only as SHA-256
hashes, expire quickly and are consumed atomically so a replay cannot sign in
twice. Requests are rate limited by both client address and canonical email.

Email changes require a recently authenticated session. A single-use token is
mailed to the *new* address for proof of control, and the previous address is
notified before and after the change. Changing an address never re-keys an
account: OAuth sign-ins are bound to the provider's stable subject in
``user_identities``, so a provider-side address change or a local address change
cannot hand the account to somebody else.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from database import (
    ACCOUNT_STATUS_ACTIVE,
    DEFAULT_DB_PATH,
    _connect,
)

RECOVERY_TOKEN_TTL_SECONDS = 15 * 60
EMAIL_CHANGE_TOKEN_TTL_SECONDS = 30 * 60
RECENT_AUTH_MAX_AGE_SECONDS = 10 * 60
SESSION_AUTHENTICATED_AT_KEY = "authenticated_at"

_TOKEN_BYTES = 32
_EMAIL_MAX_LENGTH = 254
_LOCAL_PART_MAX_LENGTH = 64
_LOCAL_PART_RE = re.compile(r"[a-z0-9._%+'-]+")
_DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_DOMAIN_MAX_LENGTH = 253
_LEGACY_TENANT_EMAIL = "legacy@local"


class AccountRecoveryError(RuntimeError):
    """Base class for safe account-recovery failures."""


class InvalidRecoveryToken(AccountRecoveryError):
    """The token is unknown, malformed, expired or already used."""


class RecoveryTokenExpired(InvalidRecoveryToken):
    """The token existed but is past its short lifetime."""


class RecoveryTokenAlreadyUsed(InvalidRecoveryToken):
    """The token was already consumed."""


class InactiveAccountError(AccountRecoveryError):
    """The account is suspended or otherwise unavailable."""


class InvalidEmailAddress(AccountRecoveryError):
    """The supplied address is not an acceptable account address."""


class EmailAlreadyInUse(AccountRecoveryError):
    """The requested address already belongs to another account."""


class EmailUnchanged(AccountRecoveryError):
    """The requested address is already the account's address."""


class IdentityVerificationError(AccountRecoveryError):
    """An external identity lacks a verified subject or address."""


class IdentityConflictError(AccountRecoveryError):
    """Binding the identity could hand over another account."""


@dataclass(frozen=True, slots=True)
class RecoveryLogin:
    user_id: str
    email: str


@dataclass(frozen=True, slots=True)
class EmailChangeRequest:
    token: str
    user_id: str
    old_email: str
    new_email: str


@dataclass(frozen=True, slots=True)
class EmailChangeResult:
    user_id: str
    old_email: str
    new_email: str


@dataclass(frozen=True, slots=True)
class OAuthAccount:
    user_id: str
    email: str
    display_name: str | None
    created: bool


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _moment(value: datetime | None) -> datetime:
    if value is None:
        return _now()
    if value.tzinfo is None:
        raise ValueError("A timezone-aware datetime is required.")
    return value.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat()


def _epoch(moment: datetime) -> float:
    return moment.timestamp()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_subject(subject: str) -> str:
    return hashlib.sha256(subject.strip().lower().encode("utf-8")).hexdigest()


def _valid_domain(domain: str) -> bool:
    labels = domain.split(".")
    return (
        len(domain) <= _DOMAIN_MAX_LENGTH
        and len(labels) >= 2
        and all(_DOMAIN_LABEL_RE.fullmatch(label) for label in labels)
    )


def canonical_email(value: object) -> str | None:
    """Return a conservative canonical address, or ``None`` if unacceptable."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _EMAIL_MAX_LENGTH or not text.isascii():
        return None
    text = text.lower()
    local, separator, domain = text.partition("@")
    if not separator or "@" in domain:
        return None
    if (
        not local
        or len(local) > _LOCAL_PART_MAX_LENGTH
        or not _LOCAL_PART_RE.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        return None
    if not _valid_domain(domain):
        return None
    return text


def is_recently_authenticated(
    session: Any,
    *,
    now: datetime | None = None,
    max_age_seconds: int = RECENT_AUTH_MAX_AGE_SECONDS,
) -> bool:
    """Whether the signed session was established inside the recent window."""

    if not session:
        return False
    value = None
    for key in (SESSION_AUTHENTICATED_AT_KEY, "auth_time"):
        try:
            value = session.get(key)
        except (AttributeError, TypeError):
            return False
        if value is not None:
            break
    if value is None:
        return False
    try:
        authenticated_at = float(value)
    except (TypeError, ValueError):
        return False
    if not max_age_seconds > 0:
        return False
    current = _moment(now).timestamp()
    return 0 <= current - authenticated_at <= max_age_seconds


def mark_session_authenticated(session: Any, *, now: datetime | None = None) -> None:
    """Record the authentication instant used by recent-authentication checks."""

    session[SESSION_AUTHENTICATED_AT_KEY] = int(_moment(now).timestamp())


def consume_action_rate_limit(
    action: str,
    subject: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    limit: int,
    window_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Atomically spend one slot for ``action``/``subject``.

    Subjects are hashed before storage so an email address or client address is
    never kept in the rate-limit table. Returns ``False`` when the budget is
    exhausted.
    """

    if limit <= 0 or window_seconds <= 0:
        raise ValueError("limit and window_seconds must be positive")
    moment = _moment(now)
    cutoff = _epoch(moment) - window_seconds
    subject_hash = _hash_subject(subject)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM account_action_rate_limits "
            "WHERE action = ? AND subject_hash = ? AND created_at <= ?",
            (action, subject_hash, cutoff),
        )
        used = conn.execute(
            "SELECT COUNT(*) FROM account_action_rate_limits "
            "WHERE action = ? AND subject_hash = ?",
            (action, subject_hash),
        ).fetchone()
        if int(used[0]) >= limit:
            return False
        conn.execute(
            "INSERT INTO account_action_rate_limits "
            "(action, subject_hash, created_at) VALUES (?, ?, ?)",
            (action, subject_hash, _epoch(moment)),
        )
    return True


def _account_row(conn: Any, user_id: str) -> Any:
    return conn.execute(
        "SELECT id, email, status, email_verified FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()


def _raise_for_inactive(row: Any) -> None:
    if row is None:
        raise InactiveAccountError("Account is not available.")
    if str(row[2]) != ACCOUNT_STATUS_ACTIVE:
        raise InactiveAccountError("Account is not available.")


def request_recovery(
    email: object,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> str | None:
    """Create a recovery token for an eligible account, or return ``None``.

    Callers must return the same generic response whether this returns a token
    or not, so the endpoint cannot be used to enumerate accounts.
    """

    canonical = canonical_email(email)
    if canonical is None or canonical == _LEGACY_TENANT_EMAIL:
        return None
    moment = _moment(now)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        account = conn.execute(
            "SELECT id, status, email_verified FROM users "
            "WHERE lower(email) = ? ORDER BY created_at, id LIMIT 1",
            (canonical,),
        ).fetchone()
        if (
            account is None
            or str(account[1]) != ACCOUNT_STATUS_ACTIVE
            or not bool(account[2])
        ):
            return None
        user_id = str(account[0])
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        conn.execute(
            "DELETE FROM account_recovery_tokens "
            "WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO account_recovery_tokens "
            "(user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (
                user_id,
                _hash_token(token),
                _stamp(moment),
                _epoch(moment) + RECOVERY_TOKEN_TTL_SECONDS,
            ),
        )
        return token


def consume_recovery_token(
    token: object,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> RecoveryLogin:
    """Atomically consume a single-use recovery token and return its account."""

    if not isinstance(token, str) or not token.strip() or len(token) > 512:
        raise InvalidRecoveryToken("Invalid or expired recovery token.")
    token_hash = _hash_token(token.strip())
    moment = _moment(now)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, user_id, expires_at, used_at "
            "FROM account_recovery_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise InvalidRecoveryToken("Invalid or expired recovery token.")
        token_id, user_id = int(row[0]), str(row[1])
        if row[3] is not None:
            raise RecoveryTokenAlreadyUsed("This recovery link has already been used.")
        if float(row[2]) <= _epoch(moment):
            raise RecoveryTokenExpired("This recovery link has expired.")
        account = _account_row(conn, user_id)
        _raise_for_inactive(account)
        conn.execute(
            "UPDATE account_recovery_tokens SET used_at = ? WHERE id = ?",
            (_stamp(moment), token_id),
        )
        return RecoveryLogin(user_id=user_id, email=str(account[1]))


def create_email_change_request(
    user_id: str,
    new_email: object,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> EmailChangeRequest:
    """Create a verification request for a verified change of address."""

    canonical = canonical_email(new_email)
    if canonical is None or canonical == _LEGACY_TENANT_EMAIL:
        raise InvalidEmailAddress("Enter a valid email address.")
    moment = _moment(now)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        account = _account_row(conn, user_id)
        _raise_for_inactive(account)
        old_email = str(account[1])
        if canonical == old_email.strip().lower():
            raise EmailUnchanged("That is already your account email address.")
        other = conn.execute(
            "SELECT 1 FROM users WHERE lower(email) = ? AND id != ? LIMIT 1",
            (canonical, user_id),
        ).fetchone()
        if other is not None:
            raise EmailAlreadyInUse("That email address is already in use.")
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        conn.execute(
            "DELETE FROM email_change_requests "
            "WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO email_change_requests "
            "(user_id, old_email, new_email, token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id,
                old_email,
                canonical,
                _hash_token(token),
                _stamp(moment),
                _epoch(moment) + EMAIL_CHANGE_TOKEN_TTL_SECONDS,
            ),
        )
        return EmailChangeRequest(
            token=token,
            user_id=user_id,
            old_email=old_email,
            new_email=canonical,
        )


def revoke_email_change_requests(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Discard any outstanding change request for an account."""

    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM email_change_requests "
            "WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        )


def consume_email_change_request(
    token: object,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> EmailChangeResult:
    """Verify the new address and atomically move the account to it."""

    if not isinstance(token, str) or not token.strip() or len(token) > 512:
        raise InvalidRecoveryToken("Invalid or expired email verification token.")
    token_hash = _hash_token(token.strip())
    moment = _moment(now)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, user_id, old_email, new_email, expires_at, used_at "
            "FROM email_change_requests WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise InvalidRecoveryToken(
                "Invalid or expired email verification token."
            )
        request_id, user_id = int(row[0]), str(row[1])
        old_email, new_email = str(row[2]), str(row[3])
        if row[5] is not None:
            raise RecoveryTokenAlreadyUsed(
                "This email verification link has already been used."
            )
        if float(row[4]) <= _epoch(moment):
            raise RecoveryTokenExpired("This email verification link has expired.")
        account = _account_row(conn, user_id)
        _raise_for_inactive(account)
        other = conn.execute(
            "SELECT 1 FROM users WHERE lower(email) = ? AND id != ? LIMIT 1",
            (new_email, user_id),
        ).fetchone()
        if other is not None:
            raise EmailAlreadyInUse("That email address is already in use.")
        stamp = _stamp(moment)
        conn.execute(
            "UPDATE email_change_requests SET used_at = ? WHERE id = ?",
            (stamp, request_id),
        )
        conn.execute(
            "DELETE FROM email_change_requests "
            "WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        )
        conn.execute(
            "UPDATE users SET email = ?, email_verified = 1, "
            "email_verified_at = ?, updated_at = ? WHERE id = ?",
            (new_email, stamp, stamp, user_id),
        )
        return EmailChangeResult(
            user_id=user_id,
            old_email=old_email,
            new_email=new_email,
        )


def record_authentication(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> None:
    """Record a successful sign-in without changing verification state."""

    stamp = _stamp(_moment(now))
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (stamp, stamp, user_id),
        )


def resolve_oauth_account(
    *,
    provider: str,
    subject: object,
    email: object,
    email_verified: object,
    display_name: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> OAuthAccount:
    """Resolve a Google sign-in to an account without trusting the email alone.

    The provider's stable subject is the primary key. A verified email may adopt
    an account that predates identity binding exactly once, but an address that
    is already bound to a different subject for the same provider is refused.
    """

    provider_key = provider.strip().lower()
    subject_key = subject.strip() if isinstance(subject, str) else ""
    canonical = canonical_email(email)
    if not provider_key or not subject_key:
        raise IdentityVerificationError("The identity provider did not supply a subject.")
    if canonical is None or canonical == _LEGACY_TENANT_EMAIL:
        raise IdentityVerificationError("The identity provider did not supply an address.")
    if email_verified is not True:
        raise IdentityVerificationError(
            "The identity provider has not verified the email address."
        )

    moment = _moment(now)
    stamp = _stamp(moment)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        bound = conn.execute(
            "SELECT user_id FROM user_identities WHERE provider = ? AND subject = ?",
            (provider_key, subject_key),
        ).fetchone()
        if bound is not None:
            user_id = str(bound[0])
            account = _account_row(conn, user_id)
            _raise_for_inactive(account)
            conn.execute(
                "UPDATE user_identities SET email = ?, email_verified = 1, "
                "last_login_at = ? WHERE provider = ? AND subject = ?",
                (canonical, stamp, provider_key, subject_key),
            )
            conn.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (stamp, stamp, user_id),
            )
            return OAuthAccount(
                user_id=user_id,
                email=str(account[1]),
                display_name=display_name,
                created=False,
            )

        matches = conn.execute(
            "SELECT id, email, status FROM users WHERE lower(email) = ?",
            (canonical,),
        ).fetchall()
        if len(matches) > 1:
            raise IdentityConflictError("That address matches more than one account.")
        created = False
        if matches:
            account = matches[0]
            user_id = str(account[0])
            _raise_for_inactive(account)
            already_bound = conn.execute(
                "SELECT subject FROM user_identities "
                "WHERE user_id = ? AND provider = ?",
                (user_id, provider_key),
            ).fetchone()
            if already_bound is not None and str(already_bound[0]) != subject_key:
                raise IdentityConflictError(
                    "That address is bound to a different provider identity."
                )
            conn.execute(
                "UPDATE users SET email_verified = 1, "
                "email_verified_at = COALESCE(email_verified_at, ?), "
                "last_login_at = ?, updated_at = ? WHERE id = ?",
                (stamp, stamp, stamp, user_id),
            )
        else:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, email, display_name, created_at, status, "
                "status_changed_at, email_verified, email_verified_at, "
                "last_login_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    user_id,
                    canonical,
                    display_name,
                    stamp,
                    ACCOUNT_STATUS_ACTIVE,
                    stamp,
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            created = True

        conn.execute(
            "INSERT OR REPLACE INTO user_identities "
            "(user_id, provider, subject, email, email_verified, created_at, "
            "last_login_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (user_id, provider_key, subject_key, canonical, stamp, stamp),
        )
        return OAuthAccount(
            user_id=user_id,
            email=canonical,
            display_name=display_name,
            created=created,
        )


def is_active_account(user_id: str, db_path: str = DEFAULT_DB_PATH) -> bool:
    """Return whether an account exists and may currently be used."""

    with _connect(db_path) as conn:
        row = _account_row(conn, user_id)
    return row is not None and str(row[2]) == ACCOUNT_STATUS_ACTIVE


__all__ = [
    "RECENT_AUTH_MAX_AGE_SECONDS",
    "RECOVERY_TOKEN_TTL_SECONDS",
    "AccountRecoveryError",
    "EmailAlreadyInUse",
    "EmailChangeRequest",
    "EmailChangeResult",
    "EmailUnchanged",
    "IdentityConflictError",
    "IdentityVerificationError",
    "InactiveAccountError",
    "InvalidEmailAddress",
    "InvalidRecoveryToken",
    "OAuthAccount",
    "RecoveryLogin",
    "RecoveryTokenAlreadyUsed",
    "RecoveryTokenExpired",
    "canonical_email",
    "consume_action_rate_limit",
    "consume_email_change_request",
    "consume_recovery_token",
    "create_email_change_request",
    "is_active_account",
    "is_recently_authenticated",
    "mark_session_authenticated",
    "record_authentication",
    "request_recovery",
    "resolve_oauth_account",
    "revoke_email_change_requests",
]
