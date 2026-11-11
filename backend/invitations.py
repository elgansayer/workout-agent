"""Invite-only beta invitation lifecycle.

Invitations are bearer links that are single-use, expiring, revocable,
rate limited, and bound to an intended email address. Only a SHA-256 hash of
the bearer token is persisted; the raw token is returned once to the inviting
administrator. Acceptance creates or reuses exactly one account and is
idempotent when retried with the same token and email.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from database import DEFAULT_DB_PATH, UserRow, _connect

INVITATION_STATUSES = ("pending", "accepted", "expired", "revoked")
DEFAULT_TTL_HOURS = 72
MAX_TTL_HOURS = 720
MAX_POLICY_LENGTH = 64
MAX_REASON_LENGTH = 200
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_INVITATION_COLUMNS = (
    "id, token_hash, email, policy, created_by_user_id, created_at, "
    "expires_at, accepted_at, accepted_user_id, revoked_at, revoke_reason"
)


class InvitationError(Exception):
    """Base class for invitation lifecycle failures."""


class InvitationNotFound(InvitationError):
    """The requested invitation does not exist."""


class InvalidInvitation(InvitationError):
    """The invitation cannot be accepted.

    The public message is deliberately generic so callers cannot distinguish
    expired, revoked, consumed and mismatched invitations.
    """


class InvitationConflict(InvitationError):
    """The requested state transition is not allowed."""


class InvitationRateLimited(InvitationError):
    """The caller exceeded an invitation rate limit."""


@dataclass(frozen=True, slots=True)
class Invitation:
    id: str
    intended_email: str
    policy: str
    status: str
    created_by_user_id: str
    created_at: str
    expires_at: str
    accepted_at: str | None = None
    revoked_at: str | None = None
    revoke_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return administrator-safe invitation metadata.

        Account identifiers and account-existence signals are intentionally
        omitted so an invitation listing never reveals whether an email address
        already has an account.
        """

        return {
            "id": self.id,
            "intended_email": self.intended_email,
            "policy": self.policy,
            "status": self.status,
            "created_by_user_id": self.created_by_user_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "accepted_at": self.accepted_at,
            "revoked_at": self.revoked_at,
            "revoke_reason": self.revoke_reason,
        }


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalise_email(email: str) -> str:
    """Return a canonical email or raise ValueError for an invalid address."""

    normalised = email.strip().lower()
    if not normalised or len(normalised) > 320 or not _EMAIL_RE.fullmatch(normalised):
        raise ValueError("A valid intended email address is required")
    return normalised


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invitation_status(
    *,
    accepted_at: str | None,
    revoked_at: str | None,
    expires_at: str,
    now: datetime | None = None,
) -> str:
    """Derive the lifecycle status without consulting account tables."""

    if accepted_at:
        return "accepted"
    if revoked_at:
        return "revoked"
    checked_at = _utc_now() if now is None else now
    try:
        if _parse_iso(expires_at) <= checked_at:
            return "expired"
    except ValueError:
        # A malformed expiry must fail closed rather than remain usable.
        return "expired"
    return "pending"


def _row_to_invitation(row: Any, now: datetime | None = None) -> Invitation:
    return Invitation(
        id=row[0],
        intended_email=row[2],
        policy=row[3],
        status=invitation_status(
            accepted_at=row[7],
            revoked_at=row[9],
            expires_at=row[6],
            now=now,
        ),
        created_by_user_id=row[4],
        created_at=row[5],
        expires_at=row[6],
        accepted_at=row[7],
        revoked_at=row[9],
        revoke_reason=row[10],
    )


def _user_from_row(row: Any) -> UserRow:
    return UserRow(
        id=row[0],
        email=row[1],
        display_name=row[2],
        created_at=row[3],
        timezone=row[4],
        units=row[5],
    )


def create_invitation(
    intended_email: str,
    *,
    created_by_user_id: str,
    policy: str = "beta",
    ttl_hours: int = DEFAULT_TTL_HOURS,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[Invitation, str]:
    """Create a single-use invitation and return it with its bearer token."""

    email = normalise_email(intended_email)
    actor = created_by_user_id.strip()
    if not actor:
        raise ValueError("An inviting user is required")
    clean_policy = policy.strip() or "beta"
    if len(clean_policy) > MAX_POLICY_LENGTH:
        raise ValueError("Invitation policy is too long")
    if not isinstance(ttl_hours, int) or isinstance(ttl_hours, bool):
        raise TypeError("ttl_hours must be an integer")
    if ttl_hours < 1 or ttl_hours > MAX_TTL_HOURS:
        raise ValueError(f"ttl_hours must be between 1 and {MAX_TTL_HOURS}")

    now = _utc_now()
    invitation = Invitation(
        id=str(uuid.uuid4()),
        intended_email=email,
        policy=clean_policy,
        status="pending",
        created_by_user_id=actor,
        created_at=_iso(now),
        expires_at=_iso(now + timedelta(hours=ttl_hours)),
    )
    token = secrets.token_urlsafe(32)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO invitations (
                id, token_hash, email, policy, created_by_user_id, created_at,
                expires_at, accepted_at, accepted_user_id, revoked_at,
                revoke_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
            """,
            (
                invitation.id,
                _hash_token(token),
                invitation.intended_email,
                invitation.policy,
                invitation.created_by_user_id,
                invitation.created_at,
                invitation.expires_at,
            ),
        )
    return invitation, token


def get_invitation(
    invitation_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> Invitation | None:
    """Return one invitation by id, or None."""

    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_INVITATION_COLUMNS} FROM invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_invitation(row)


def list_invitations(
    *,
    status: str = "all",
    limit: int = 200,
    db_path: str = DEFAULT_DB_PATH,
) -> list[Invitation]:
    """List invitations, optionally filtered by derived lifecycle status."""

    requested = status.strip().lower()
    if requested != "all" and requested not in INVITATION_STATUSES:
        raise ValueError(
            "status must be one of: all, " + ", ".join(INVITATION_STATUSES)
        )
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_INVITATION_COLUMNS} FROM invitations "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (1000,),
        ).fetchall()
    now = _utc_now()
    invitations = [_row_to_invitation(row, now) for row in rows]
    if requested != "all":
        invitations = [item for item in invitations if item.status == requested]
    return invitations[:limit]


def revoke_invitation(
    invitation_id: str,
    *,
    actor_user_id: str,
    reason: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> Invitation:
    """Revoke a pending invitation. Accepted invitations cannot be revoked."""

    actor = actor_user_id.strip()
    if not actor:
        raise ValueError("An acting administrator is required")
    clean_reason = (reason or "").strip()[:MAX_REASON_LENGTH] or None
    now = _utc_now()
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_INVITATION_COLUMNS} FROM invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
        if row is None:
            raise InvitationNotFound("Invitation not found")
        invitation = _row_to_invitation(row, now)
        if invitation.status == "accepted":
            raise InvitationConflict("Accepted invitations cannot be revoked")
        if invitation.status != "revoked":
            updated = conn.execute(
                "UPDATE invitations SET revoked_at = ?, revoke_reason = ? "
                "WHERE id = ? AND accepted_at IS NULL AND revoked_at IS NULL",
                (_iso(now), clean_reason, invitation_id),
            )
            row = conn.execute(
                f"SELECT {_INVITATION_COLUMNS} FROM invitations WHERE id = ?",
                (invitation_id,),
            ).fetchone()
            if row is None:
                raise InvitationNotFound("Invitation not found")
            invitation = _row_to_invitation(row, now)
            if updated.rowcount != 1 and invitation.status != "revoked":
                if invitation.status == "accepted":
                    raise InvitationConflict(
                        "Accepted invitations cannot be revoked"
                    )
                raise InvitationConflict("Invitation could not be revoked")
    return invitation


def accept_invitation(
    token: str,
    *,
    email: str,
    display_name: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> UserRow:
    """Accept an invitation, creating or reusing exactly one account.

    The invitation and the account creation/claim happen in one SQLite
    transaction. Retrying with the same token and email returns the same user
    without creating a duplicate.
    """

    if not token or len(token) > 512:
        raise InvalidInvitation("Invitation is invalid or unavailable")
    login_email = normalise_email(email)
    token_hash = _hash_token(token)
    now = _utc_now()
    now_iso = _iso(now)

    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_INVITATION_COLUMNS} FROM invitations WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise InvalidInvitation("Invitation is invalid or unavailable")

        invitation = _row_to_invitation(row, now)
        if invitation.intended_email != login_email:
            raise InvalidInvitation("Invitation is invalid or unavailable")
        if invitation.status in {"revoked", "expired"}:
            raise InvalidInvitation("Invitation is invalid or unavailable")

        if invitation.status == "accepted":
            accepted_user_id = row[8]
            if not isinstance(accepted_user_id, str) or not accepted_user_id:
                raise InvalidInvitation("Invitation is invalid or unavailable")
            accepted = conn.execute(
                "SELECT id, email, display_name, created_at, timezone, units "
                "FROM users WHERE id = ?",
                (accepted_user_id,),
            ).fetchone()
            if accepted is None or normalise_email(accepted[1]) != login_email:
                raise InvalidInvitation("Invitation is invalid or unavailable")
            return _user_from_row(accepted)

        user_row = conn.execute(
            "SELECT id, email, display_name, created_at, timezone, units "
            "FROM users WHERE email = ? COLLATE NOCASE",
            (login_email,),
        ).fetchone()
        if user_row is None:
            user_id = str(uuid.uuid4())
            try:
                conn.execute(
                    """
                    INSERT INTO users (id, email, display_name, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, login_email, display_name, now_iso),
                )
            except sqlite3.IntegrityError:
                # A concurrent retry created the same account first.
                pass
            user_row = conn.execute(
                "SELECT id, email, display_name, created_at, timezone, units "
                "FROM users WHERE email = ? COLLATE NOCASE",
                (login_email,),
            ).fetchone()
        if user_row is None:
            raise InvalidInvitation("Invitation is invalid or unavailable")
        user = _user_from_row(user_row)

        claim = conn.execute(
            "UPDATE invitations SET accepted_at = ?, accepted_user_id = ? "
            "WHERE id = ? AND accepted_at IS NULL AND revoked_at IS NULL "
            "AND expires_at > ?",
            (now_iso, user["id"], invitation.id, now_iso),
        )
        if claim.rowcount != 1:
            # A concurrent retry may have claimed it first. Only allow the same
            # intended account to receive the idempotent success response.
            retry = conn.execute(
                "SELECT accepted_at, accepted_user_id FROM invitations "
                "WHERE id = ?",
                (invitation.id,),
            ).fetchone()
            if (
                retry is None
                or retry[0] is None
                or retry[1] != user["id"]
            ):
                raise InvalidInvitation("Invitation is invalid or unavailable")
    return user


def consume_rate_limit(
    key: str,
    *,
    limit: int,
    window_seconds: int,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Record an action for *key* and return False when the limit is reached."""

    clean_key = key.strip()
    if not clean_key or len(clean_key) > 200:
        raise ValueError("A bounded rate-limit key is required")
    if limit < 1 or window_seconds < 1:
        raise ValueError("Rate-limit limit and window must be positive")
    now = time.time()
    cutoff = now - window_seconds
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM invitation_rate_limits WHERE timestamp <= ?",
            (cutoff,),
        )
        row = conn.execute(
            "SELECT COUNT(*) FROM invitation_rate_limits WHERE key = ?",
            (clean_key,),
        ).fetchone()
        if row is not None and int(row[0]) >= limit:
            return False
        conn.execute(
            "INSERT INTO invitation_rate_limits (key, timestamp) VALUES (?, ?)",
            (clean_key, now),
        )
    return True


def enforce_rate_limit(
    key: str,
    *,
    limit: int,
    window_seconds: int,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Raise InvitationRateLimited when *key* has no remaining budget."""

    if not consume_rate_limit(
        key,
        limit=limit,
        window_seconds=window_seconds,
        db_path=db_path,
    ):
        raise InvitationRateLimited("Invitation rate limit exceeded")


__all__ = [
    "DEFAULT_TTL_HOURS",
    "INVITATION_STATUSES",
    "InvalidInvitation",
    "Invitation",
    "InvitationConflict",
    "InvitationError",
    "InvitationNotFound",
    "InvitationRateLimited",
    "accept_invitation",
    "consume_rate_limit",
    "create_invitation",
    "enforce_rate_limit",
    "get_invitation",
    "invitation_status",
    "list_invitations",
    "normalise_email",
    "revoke_invitation",
]
