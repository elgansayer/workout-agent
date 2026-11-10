"""Append-only, tenant-scoped consent and permission decision ledger.

Every grant or withdrawal is stored as its own immutable row. The current state
for a purpose is therefore the most recent decision, not a mutable flag on a
single record. Rows are chained with a SHA-256 digest and database triggers
reject updates and deletes, so accidental edits, reordered rows or removed
history are detectable when the ledger is verified.

The ledger is deliberately free of feature-specific knowledge: health
connectors, notifications, analytics and AI processing all record a purpose,
the policy version the user agreed to, the granted scopes, the acting identity
and the decision time. Callers must derive ``user_id`` from the authenticated
session and must not trust a client-supplied owner identifier.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
TABLE = "consent_ledger_events"
GENESIS_HASH = "0" * 64
CURRENT_CONSENT_POLICY_VERSION = "1.0.0"

_MAX_IDENTIFIER_LENGTH = 128
_MAX_POLICY_VERSION_LENGTH = 64
_MAX_PROVIDER_LENGTH = 64
_MAX_SCOPE_LENGTH = 200
_MAX_SCOPES = 64


class ConsentPurpose(StrEnum):
    HEALTH_CONNECTOR = "health_connector"
    NOTIFICATIONS = "notifications"
    ANALYTICS = "analytics"
    AI_PROCESSING = "ai_processing"


class ConsentDecision(StrEnum):
    GRANTED = "granted"
    REVOKED = "revoked"


class ConsentActorType(StrEnum):
    USER = "user"
    OPERATOR = "operator"
    SYSTEM = "system"


class ConsentRetention(StrEnum):
    REVOKE_ONLY = "revoke_only"
    REVOKE_AND_PURGE = "revoke_and_purge"


class ConsentRequiredError(PermissionError):
    """Raised when a feature is attempted without a usable consent decision."""


@dataclass(frozen=True, slots=True)
class ConsentDecisionRequest:
    """One decision to append to the ledger."""

    user_id: str | int
    purpose: ConsentPurpose
    policy_version: str
    actor_id: str
    scopes: Iterable[str] = ()
    decision: ConsentDecision = ConsentDecision.GRANTED
    actor_type: ConsentActorType = ConsentActorType.USER
    provider: str | None = None
    retention: ConsentRetention = ConsentRetention.REVOKE_ONLY
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, ConsentPurpose):
            raise TypeError("consent purpose must be a ConsentPurpose")
        if not isinstance(self.decision, ConsentDecision):
            raise TypeError("consent decision must be a ConsentDecision")
        if not isinstance(self.actor_type, ConsentActorType):
            raise TypeError("consent actor type must be a ConsentActorType")
        if not isinstance(self.retention, ConsentRetention):
            raise TypeError("consent retention must be a ConsentRetention")
        if self.occurred_at is not None and self.occurred_at.tzinfo is None:
            raise ValueError("consent decision time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """A persisted consent decision."""

    seq: int
    schema_version: int
    consent_id: str
    user_id: str
    purpose: str
    provider: str | None
    policy_version: str
    scopes: frozenset[str]
    actor_type: str
    actor_id: str
    decision: str
    retention: str
    occurred_at: str
    prev_hash: str
    record_hash: str

    @property
    def active(self) -> bool:
        return self.decision == ConsentDecision.GRANTED.value

    @property
    def revocation_state(self) -> str:
        return "active" if self.active else "revoked"

    @property
    def revoked_at(self) -> str | None:
        return None if self.active else self.occurred_at

    def covers(self, required_scopes: Iterable[str]) -> bool:
        return frozenset(_normalise_scopes(required_scopes)).issubset(self.scopes)


@dataclass(frozen=True, slots=True)
class ConsentChainVerification:
    ok: bool
    checked: int
    head_seq: int | None
    head_hash: str | None


_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        consent_id TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL REFERENCES users(id),
        purpose TEXT NOT NULL,
        provider TEXT,
        policy_version TEXT NOT NULL,
        scopes_json TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        decision TEXT NOT NULL,
        retention TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        record_hash TEXT NOT NULL UNIQUE
    )
    """,
    (
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_user "
        f"ON {TABLE} (user_id, purpose, provider, seq)"
    ),
    (
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_purpose "
        f"ON {TABLE} (purpose, provider, occurred_at)"
    ),
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_no_update
    BEFORE UPDATE ON {TABLE}
    BEGIN
        SELECT RAISE(ABORT, 'consent ledger events are append-only');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_no_delete
    BEFORE DELETE ON {TABLE}
    BEGIN
        SELECT RAISE(ABORT, 'consent ledger events are append-only');
    END
    """,
)

_COLUMN_NAMES = (
    "seq",
    "schema_version",
    "consent_id",
    "user_id",
    "purpose",
    "provider",
    "policy_version",
    "scopes_json",
    "actor_type",
    "actor_id",
    "decision",
    "retention",
    "occurred_at",
    "prev_hash",
    "record_hash",
)
_COLUMNS = ", ".join(_COLUMN_NAMES)
_HASHED_COLUMNS = tuple(name for name in _COLUMN_NAMES if name != "record_hash")


def ensure_consent_schema(conn: sqlite3.Connection) -> None:
    """Create the append-only ledger table and its tenant-aware indexes."""

    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def _open(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _normalise_user_id(user_id: str | int) -> str:
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)):
        raise TypeError("consent requires a tenant user_id")
    if isinstance(user_id, int):
        text = str(user_id)
    else:
        text = user_id.strip()
    if not text or len(text) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError("consent requires a tenant user_id")
    return text


def _normalise_provider(
    provider: str | None,
    purpose: ConsentPurpose,
) -> str | None:
    text = provider.strip().lower() if isinstance(provider, str) else ""
    if len(text) > _MAX_PROVIDER_LENGTH:
        raise ValueError("consent provider is too long")
    if purpose is ConsentPurpose.HEALTH_CONNECTOR:
        if not text:
            raise ValueError("health consent requires a provider")
        return text
    return text or None


def _normalise_policy_version(policy_version: str) -> str:
    text = policy_version.strip() if isinstance(policy_version, str) else ""
    if not text or len(text) > _MAX_POLICY_VERSION_LENGTH:
        raise ValueError("consent requires a policy version")
    return text


def _normalise_scopes(scopes: Iterable[str] | None) -> tuple[str, ...]:
    if scopes is None:
        return ()
    if isinstance(scopes, str):
        scopes = (scopes,)
    normalised: set[str] = set()
    for scope in scopes:
        if not isinstance(scope, str):
            raise TypeError("consent scopes must be strings")
        text = scope.strip().lower()
        if not text:
            continue
        if len(text) > _MAX_SCOPE_LENGTH:
            raise ValueError("consent scope is too long")
        normalised.add(text)
    if len(normalised) > _MAX_SCOPES:
        raise ValueError("too many consent scopes")
    return tuple(sorted(normalised))


def _normalise_actor(actor_id: str, actor_type: ConsentActorType) -> str:
    text = actor_id.strip() if isinstance(actor_id, str) else ""
    if actor_type is ConsentActorType.SYSTEM:
        return text or "system"
    if not text or len(text) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError("consent requires an actor identity")
    return text


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def record_hash_for(prev_hash: str, fields: Mapping[str, Any]) -> str:
    """Return the chain digest for the hashed columns of one row."""

    payload = canonical_json({name: fields[name] for name in _HASHED_COLUMNS})
    return hashlib.sha256(
        f"{prev_hash}\n{payload}".encode("ascii"),
    ).hexdigest()


def _record_from_fields(fields: Mapping[str, Any]) -> ConsentRecord:
    return ConsentRecord(
        seq=int(fields["seq"]),
        schema_version=int(fields["schema_version"]),
        consent_id=str(fields["consent_id"]),
        user_id=str(fields["user_id"]),
        purpose=str(fields["purpose"]),
        provider=fields["provider"],
        policy_version=str(fields["policy_version"]),
        scopes=frozenset(json.loads(str(fields["scopes_json"]))),
        actor_type=str(fields["actor_type"]),
        actor_id=str(fields["actor_id"]),
        decision=str(fields["decision"]),
        retention=str(fields["retention"]),
        occurred_at=str(fields["occurred_at"]),
        prev_hash=str(fields["prev_hash"]),
        record_hash=str(fields["record_hash"]),
    )


def append_decision(
    db_path: str,
    request: ConsentDecisionRequest,
) -> ConsentRecord:
    """Append one decision and return the persisted row.

    Storage errors are raised: a caller must never report a grant that was not
    actually recorded.
    """

    user_id = _normalise_user_id(request.user_id)
    provider = _normalise_provider(request.provider, request.purpose)
    policy_version = _normalise_policy_version(request.policy_version)
    actor_id = _normalise_actor(request.actor_id, request.actor_type)
    scopes = _normalise_scopes(request.scopes)
    if (
        request.purpose is ConsentPurpose.HEALTH_CONNECTOR
        and request.decision is ConsentDecision.GRANTED
        and not scopes
    ):
        raise ValueError("health consent requires at least one scope")

    occurred = (request.occurred_at or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    )
    fields: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "consent_id": uuid.uuid4().hex,
        "user_id": user_id,
        "purpose": request.purpose.value,
        "provider": provider,
        "policy_version": policy_version,
        "scopes_json": canonical_json(list(scopes)),
        "actor_type": request.actor_type.value,
        "actor_id": actor_id,
        "decision": request.decision.value,
        "retention": request.retention.value,
        "occurred_at": _timestamp(occurred),
    }

    conn = _open(db_path)
    try:
        ensure_consent_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            head = conn.execute(
                f"SELECT seq, record_hash FROM {TABLE} ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            sequence = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?", (TABLE,)
            ).fetchone()
            last_seq, prev_hash = (
                (int(head[0]), str(head[1])) if head else (0, GENESIS_HASH)
            )
            # AUTOINCREMENT remembers the highest sequence ever issued, so a
            # removed tail row leaves a permanent, detectable gap.
            fields["seq"] = max(last_seq, int(sequence[0]) if sequence else 0) + 1
            fields["prev_hash"] = prev_hash
            fields["record_hash"] = record_hash_for(prev_hash, fields)

            conn.execute(
                f"INSERT INTO {TABLE} ({_COLUMNS}) "
                f"VALUES ({', '.join('?' * len(_COLUMN_NAMES))})",
                tuple(fields[name] for name in _COLUMN_NAMES),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return _record_from_fields(fields)


def grant_consent(
    db_path: str,
    *,
    user_id: str | int,
    purpose: ConsentPurpose,
    policy_version: str,
    actor_id: str,
    scopes: Iterable[str] = (),
    provider: str | None = None,
    actor_type: ConsentActorType = ConsentActorType.USER,
    occurred_at: datetime | None = None,
) -> ConsentRecord:
    """Record an explicit grant for one purpose."""

    return append_decision(
        db_path,
        ConsentDecisionRequest(
            user_id=user_id,
            purpose=purpose,
            policy_version=policy_version,
            actor_id=actor_id,
            scopes=scopes,
            decision=ConsentDecision.GRANTED,
            actor_type=actor_type,
            provider=provider,
            occurred_at=occurred_at,
        ),
    )


def revoke_consent(
    db_path: str,
    *,
    user_id: str | int,
    purpose: ConsentPurpose,
    actor_id: str,
    policy_version: str = CURRENT_CONSENT_POLICY_VERSION,
    scopes: Iterable[str] = (),
    provider: str | None = None,
    actor_type: ConsentActorType = ConsentActorType.USER,
    retention: ConsentRetention = ConsentRetention.REVOKE_ONLY,
    occurred_at: datetime | None = None,
) -> ConsentRecord:
    """Record a withdrawal.

    The previous grant remains in the ledger as immutable history. The new row
    makes the current state ``revoked`` so feature checks fail closed.
    """

    return append_decision(
        db_path,
        ConsentDecisionRequest(
            user_id=user_id,
            purpose=purpose,
            policy_version=policy_version,
            actor_id=actor_id,
            scopes=scopes,
            decision=ConsentDecision.REVOKED,
            actor_type=actor_type,
            provider=provider,
            retention=retention,
            occurred_at=occurred_at,
        ),
    )


def _current_row(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    purpose: ConsentPurpose,
    provider: str | None,
) -> ConsentRecord | None:
    if provider is None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} "
            "WHERE user_id = ? AND purpose = ? "
            "ORDER BY seq DESC LIMIT 1",
            (user_id, purpose.value),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} "
            "WHERE user_id = ? AND purpose = ? AND provider = ? "
            "ORDER BY seq DESC LIMIT 1",
            (user_id, purpose.value, provider),
        ).fetchone()
    if row is None:
        return None
    return _record_from_fields(dict(zip(_COLUMN_NAMES, row, strict=True)))


def current_consent(
    db_path: str,
    *,
    user_id: str | int,
    purpose: ConsentPurpose,
    provider: str | None = None,
) -> ConsentRecord | None:
    """Return the latest decision for one tenant purpose, if any."""

    normalised_user = _normalise_user_id(user_id)
    normalised_provider = _normalise_provider(provider, purpose)
    conn = _open(db_path)
    try:
        ensure_consent_schema(conn)
        return _current_row(
            conn,
            user_id=normalised_user,
            purpose=purpose,
            provider=normalised_provider,
        )
    finally:
        conn.close()


def current_consents(
    db_path: str,
    *,
    user_id: str | int,
    purpose: ConsentPurpose | None = None,
) -> tuple[ConsentRecord, ...]:
    """Return the latest decision per purpose/provider for one tenant."""

    normalised_user = _normalise_user_id(user_id)
    conn = _open(db_path)
    try:
        ensure_consent_schema(conn)
        if purpose is None:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM {TABLE} WHERE user_id = ? "
                "ORDER BY seq DESC",
                (normalised_user,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM {TABLE} WHERE user_id = ? AND purpose = ? "
                "ORDER BY seq DESC",
                (normalised_user, purpose.value),
            ).fetchall()
    finally:
        conn.close()

    latest: dict[tuple[str, str | None], ConsentRecord] = {}
    for row in rows:
        record = _record_from_fields(dict(zip(_COLUMN_NAMES, row, strict=True)))
        latest.setdefault((record.purpose, record.provider), record)
    return tuple(
        sorted(latest.values(), key=lambda item: (item.purpose, item.provider or ""))
    )


def has_active_consent(
    db_path: str,
    *,
    user_id: str | int,
    purpose: ConsentPurpose,
    provider: str | None = None,
    required_scopes: Iterable[str] = (),
    policy_version: str | None = None,
) -> bool:
    """Return whether the tenant currently has a usable consent decision."""

    record = current_consent(
        db_path,
        user_id=user_id,
        purpose=purpose,
        provider=provider,
    )
    if record is None or not record.active:
        return False
    if policy_version is not None and record.policy_version != policy_version:
        return False
    return record.covers(required_scopes)


def require_active_consent(
    db_path: str,
    *,
    user_id: str | int,
    purpose: ConsentPurpose,
    provider: str | None = None,
    required_scopes: Iterable[str] = (),
    policy_version: str | None = None,
) -> ConsentRecord:
    """Return the active decision or raise :class:`ConsentRequiredError`."""

    record = current_consent(
        db_path,
        user_id=user_id,
        purpose=purpose,
        provider=provider,
    )
    if record is None:
        raise ConsentRequiredError(
            f"no consent has been recorded for {purpose.value}"
        )
    if not record.active:
        raise ConsentRequiredError(
            f"consent for {purpose.value} has been withdrawn"
        )
    if policy_version is not None and record.policy_version != policy_version:
        raise ConsentRequiredError(
            f"consent for {purpose.value} does not cover the current policy"
        )
    if not record.covers(required_scopes):
        raise ConsentRequiredError(
            f"consent for {purpose.value} does not include the required scopes"
        )
    return record


def verify_chain(db_path: str) -> ConsentChainVerification:
    """Walk the ledger and verify that every digest links to its predecessor."""

    conn = _open(db_path)
    try:
        ensure_consent_schema(conn)
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} ORDER BY seq ASC"
        ).fetchall()
    finally:
        conn.close()

    prev_hash = GENESIS_HASH
    head_seq: int | None = None
    head_hash: str | None = None
    for row in rows:
        fields = dict(zip(_COLUMN_NAMES, row, strict=True))
        if fields["prev_hash"] != prev_hash:
            return ConsentChainVerification(
                False, len(rows), head_seq, head_hash
            )
        expected = record_hash_for(prev_hash, fields)
        if fields["record_hash"] != expected:
            return ConsentChainVerification(
                False, len(rows), head_seq, head_hash
            )
        prev_hash = str(fields["record_hash"])
        head_seq = int(fields["seq"])
        head_hash = prev_hash
    return ConsentChainVerification(True, len(rows), head_seq, head_hash)
