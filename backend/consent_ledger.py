"""Append-only ledger of purpose-specific consent and permission decisions.

Every data-dependent feature checks this ledger before it processes or delivers
data. A decision is never updated in place: granting and withdrawing append a
new row, and the current state is the most recent row for the tenant, purpose
and scope. Each row records the policy version, purpose, granted scopes, the
decision time, the acting identity and the revocation state.

Rows are tenant scoped and the table is append-only through database triggers.
A withdrawn health-data consent is paired with the documented retention or
deletion workflow by the caller (see ``docs/CONSENT_LEDGER.md``).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

TABLE = "consent_decisions"
POLICY_VERSION = "2026.1"

#: ``init_db`` hands the schema helper a cursor; request code uses a connection.
SqlExecutor = sqlite3.Connection | sqlite3.Cursor

ALL_SCOPES = "*"


class ConsentPurpose(StrEnum):
    """Closed vocabulary of purposes that may process or deliver user data."""

    HEALTH_DATA = "health_data"
    AI_PROCESSING = "ai_processing"
    NOTIFICATIONS = "notifications"
    ANALYTICS = "analytics"


class ConsentDecision(StrEnum):
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


class ActorType(StrEnum):
    USER = "user"
    OPERATOR = "operator"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ConsentPolicy:
    purpose: ConsentPurpose
    version: str
    label: str
    explanation: str
    scopes: tuple[str, ...]


POLICIES: dict[ConsentPurpose, ConsentPolicy] = {
    ConsentPurpose.HEALTH_DATA: ConsentPolicy(
        purpose=ConsentPurpose.HEALTH_DATA,
        version=POLICY_VERSION,
        label="Health data",
        explanation=(
            "Import and process health, recovery and body measurements from your "
            "connected providers. Withdrawing stops future imports and can delete "
            "the data those providers already sent."
        ),
        scopes=(
            "sleep",
            "heart_rate",
            "hrv",
            "activity",
            "body",
            "spo2",
            "write_workouts",
        ),
    ),
    ConsentPurpose.AI_PROCESSING: ConsentPolicy(
        purpose=ConsentPurpose.AI_PROCESSING,
        version=POLICY_VERSION,
        label="AI coaching",
        explanation=(
            "Send the minimum training and recovery context needed for the "
            "coaching request to your configured AI provider. Withdrawing stops "
            "new AI requests; stored coaching history is not deleted."
        ),
        scopes=("generate_coaching", "share_health_context"),
    ),
    ConsentPurpose.NOTIFICATIONS: ConsentPolicy(
        purpose=ConsentPurpose.NOTIFICATIONS,
        version=POLICY_VERSION,
        label="Notifications",
        explanation=(
            "Save and deliver workout, recovery and coaching notifications. "
            "Withdrawing removes push subscriptions and stops new delivery."
        ),
        scopes=("in_app", "push", "telegram"),
    ),
    ConsentPurpose.ANALYTICS: ConsentPolicy(
        purpose=ConsentPurpose.ANALYTICS,
        version=POLICY_VERSION,
        label="Product analytics",
        explanation=(
            "Use aggregated product-usage measurements to improve the service. "
            "This deployment currently sends no analytics data to a third party."
        ),
        scopes=("product_usage",),
    ),
}

#: Features that are enabled by an existing connection are treated as an
#: explicit prior choice and are migrated once into the ledger. This preserves
#: the user's earlier opt-in without silently granting a feature they never had.
_AI_PROVIDERS = ("gemini", "claude", "openai", "deepseek")


class ConsentError(ValueError):
    """A consent request is malformed or refers to an unknown purpose."""


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """One stored decision, before or after withdrawal."""

    id: int
    record_id: str
    user_id: str
    purpose: ConsentPurpose
    scope: str
    policy_version: str
    scopes: tuple[str, ...]
    decision: ConsentDecision
    actor_type: ActorType
    actor_id: str | None
    decided_at: str
    revoked_at: str | None
    source: str

    @property
    def active(self) -> bool:
        return self.decision is ConsentDecision.GRANTED and self.revoked_at is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "purpose": self.purpose.value,
            "scope": self.scope,
            "policy_version": self.policy_version,
            "scopes": list(self.scopes),
            "decision": self.decision.value,
            "active": self.active,
            "actor_type": self.actor_type.value,
            "actor_id": self.actor_id,
            "decided_at": self.decided_at,
            "revoked_at": self.revoked_at,
            "source": self.source,
        }


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS consent_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL REFERENCES users(id),
        purpose TEXT NOT NULL CHECK (
            purpose IN ('health_data', 'ai_processing', 'notifications', 'analytics')
        ),
        scope TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        scopes_json TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('granted', 'withdrawn')),
        actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'operator', 'system')),
        actor_id TEXT,
        decided_at TEXT NOT NULL,
        revoked_at TEXT,
        source TEXT NOT NULL
    )
    """,
    (
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_user "
        f"ON {TABLE} (user_id, purpose, scope, decided_at DESC, id DESC)"
    ),
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_append_only_update
    BEFORE UPDATE ON {TABLE}
    WHEN NEW.user_id IS NOT NULL AND NEW.user_id != ''
    BEGIN
        SELECT RAISE(ABORT, 'consent decisions are append-only');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_append_only_delete
    BEFORE DELETE ON {TABLE}
    BEGIN
        SELECT RAISE(ABORT, 'consent decisions are append-only');
    END
    """,
    # The ledger is not adopted by the legacy ownership migration: a decision is
    # attributable to the account that made it. These guards still make an
    # ownerless write impossible through any code path.
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_user_required_insert
    BEFORE INSERT ON {TABLE}
    WHEN NEW.user_id IS NULL OR NEW.user_id = ''
    BEGIN
        SELECT RAISE(ABORT, '{TABLE}.user_id is required');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_user_required_update
    BEFORE UPDATE OF user_id ON {TABLE}
    WHEN NEW.user_id IS NULL OR NEW.user_id = ''
    BEGIN
        SELECT RAISE(ABORT, '{TABLE}.user_id is required');
    END
    """,
)

_COLUMNS = (
    "id, record_id, user_id, purpose, scope, policy_version, scopes_json, "
    "decision, actor_type, actor_id, decided_at, revoked_at, source"
)


def ensure_schema(cursor: sqlite3.Cursor) -> None:
    """Create the ledger, tenant-first index and append-only triggers."""

    for statement in _SCHEMA_STATEMENTS:
        cursor.execute(statement)


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    row = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def policy_for(purpose: ConsentPurpose | str) -> ConsentPolicy:
    try:
        return POLICIES[ConsentPurpose(purpose)]
    except (ValueError, KeyError) as exc:
        raise ConsentError(f"unknown consent purpose: {purpose!r}") from exc


def _normalise_actor(actor_type: ActorType | str, actor_id: str | None) -> tuple[ActorType, str | None]:
    try:
        resolved = ActorType(actor_type)
    except ValueError as exc:
        raise ConsentError("unknown consent actor type") from exc
    if resolved is ActorType.SYSTEM:
        if actor_id:
            raise ConsentError("system consent decisions cannot carry an actor id")
        return resolved, None
    if not actor_id or not actor_id.strip():
        raise ConsentError("user and operator consent decisions require an actor id")
    return resolved, actor_id.strip()


def _clean_scope(scope: str | None) -> str:
    text = (scope or ALL_SCOPES).strip()
    if not text:
        raise ConsentError("consent scope cannot be blank")
    if len(text) > 64:
        raise ConsentError("consent scope is too long")
    return text


def _clean_scopes(policy: ConsentPolicy, scopes: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(str(scope).strip() for scope in (scopes or policy.scopes)))
    if not selected or any(not scope for scope in selected):
        raise ConsentError("a consent decision requires at least one scope")
    unknown = sorted(set(selected) - set(policy.scopes))
    if unknown:
        raise ConsentError(f"unsupported consent scopes: {', '.join(unknown)}")
    return selected


def _timestamp(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _record_from_row(row: tuple[Any, ...]) -> ConsentRecord:
    return ConsentRecord(
        id=int(row[0]),
        record_id=str(row[1]),
        user_id=str(row[2]),
        purpose=ConsentPurpose(row[3]),
        scope=str(row[4]),
        policy_version=str(row[5]),
        scopes=tuple(json.loads(row[6])),
        decision=ConsentDecision(row[7]),
        actor_type=ActorType(row[8]),
        actor_id=row[9],
        decided_at=str(row[10]),
        revoked_at=row[11],
        source=str(row[12]),
    )


def current_decision(
    conn: SqlExecutor,
    user_id: str,
    purpose: ConsentPurpose | str,
    scope: str = ALL_SCOPES,
) -> ConsentRecord | None:
    """Return the newest exact or wildcard decision for one tenant and purpose."""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ConsentError("consent decisions require an authenticated user")
    policy = policy_for(purpose)
    target_scope = _clean_scope(scope)
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM {TABLE} "
        "WHERE user_id = ? AND purpose = ? AND (scope = ? OR scope = ?) "
        "ORDER BY decided_at DESC, id DESC LIMIT 1",
        (user_id.strip(), policy.purpose.value, target_scope, ALL_SCOPES),
    ).fetchone()
    return _record_from_row(row) if row else None


def _append(
    conn: SqlExecutor,
    *,
    user_id: str,
    purpose: ConsentPurpose | str,
    scope: str,
    scopes: tuple[str, ...],
    decision: ConsentDecision,
    actor_type: ActorType,
    actor_id: str | None,
    source: str,
    decided_at: str,
    revoked_at: str | None,
) -> ConsentRecord:
    policy = policy_for(purpose)
    record_id = uuid.uuid4().hex
    conn.execute(
        f"INSERT INTO {TABLE} "
        "(record_id, user_id, purpose, scope, policy_version, scopes_json, "
        "decision, actor_type, actor_id, decided_at, revoked_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            user_id,
            policy.purpose.value,
            scope,
            policy.version,
            json.dumps(list(scopes), separators=(",", ":")),
            decision.value,
            actor_type.value,
            actor_id,
            decided_at,
            revoked_at,
            source,
        ),
    )
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM {TABLE} WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    assert row is not None
    return _record_from_row(row)


def record_decision(
    conn: SqlExecutor,
    *,
    user_id: str,
    purpose: ConsentPurpose | str,
    decision: ConsentDecision | str,
    actor_type: ActorType | str = ActorType.USER,
    actor_id: str | None = None,
    scope: str | None = None,
    scopes: tuple[str, ...] | list[str] | None = None,
    source: str = "web",
    decided_at: datetime | None = None,
) -> ConsentRecord:
    """Append one grant or withdrawal. The caller owns the transaction."""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ConsentError("consent decisions require an authenticated user")
    policy = policy_for(purpose)
    try:
        choice = ConsentDecision(decision)
    except ValueError as exc:
        raise ConsentError("unknown consent decision") from exc
    resolved_actor, resolved_actor_id = _normalise_actor(actor_type, actor_id)
    cleaned_scope = _clean_scope(scope)

    if choice is ConsentDecision.WITHDRAWN:
        previous = current_decision(conn, user_id, policy.purpose, cleaned_scope)
        selected = tuple(scopes) if scopes is not None else (previous.scopes if previous else ())
        if not selected:
            selected = policy.scopes
        revoked_at = _timestamp(decided_at)
        granted_scopes = _clean_scopes(policy, selected)
        return _append(
            conn,
            user_id=user_id.strip(),
            purpose=policy.purpose,
            scope=cleaned_scope,
            scopes=granted_scopes,
            decision=choice,
            actor_type=resolved_actor,
            actor_id=resolved_actor_id,
            source=str(source).strip()[:64] or "web",
            decided_at=revoked_at,
            revoked_at=revoked_at,
        )

    granted_scopes = _clean_scopes(policy, scopes)
    return _append(
        conn,
        user_id=user_id.strip(),
        purpose=policy.purpose,
        scope=cleaned_scope,
        scopes=granted_scopes,
        decision=choice,
        actor_type=resolved_actor,
        actor_id=resolved_actor_id,
        source=str(source).strip()[:64] or "web",
        decided_at=_timestamp(decided_at),
        revoked_at=None,
    )


def has_consent(
    conn: SqlExecutor,
    user_id: str,
    purpose: ConsentPurpose | str,
    *,
    scope: str = ALL_SCOPES,
    required_scopes: tuple[str, ...] = (),
) -> bool:
    """Whether current, version-matched consent covers every required scope."""

    policy = policy_for(purpose)
    record = current_decision(conn, user_id, policy.purpose, scope)
    if record is None or not record.active:
        return False
    # A stored decision made under an older policy must be asked again.
    if record.policy_version != policy.version:
        return False
    return set(required_scopes).issubset(record.scopes)


def has_consent_at(
    db_path: str,
    user_id: str,
    purpose: ConsentPurpose | str,
    *,
    scope: str = ALL_SCOPES,
    required_scopes: tuple[str, ...] = (),
) -> bool:
    """Read consent from the configured database, failing closed on errors."""

    from database import _connect

    try:
        with _connect(db_path) as conn:
            return has_consent(
                conn,
                user_id,
                purpose,
                scope=scope,
                required_scopes=required_scopes,
            )
    except (sqlite3.Error, OSError, ValueError):
        return False


def grant_consent_at(
    db_path: str,
    user_id: str,
    purpose: ConsentPurpose | str,
    *,
    scopes: tuple[str, ...] | list[str] | None = None,
    actor_id: str | None = None,
    actor_type: ActorType | str = ActorType.USER,
    source: str = "web",
) -> ConsentRecord:
    from database import _connect

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return record_decision(
            conn,
            user_id=user_id,
            purpose=purpose,
            decision=ConsentDecision.GRANTED,
            actor_type=actor_type,
            actor_id=actor_id,
            scopes=scopes,
            source=source,
        )


def withdraw_consent_at(
    db_path: str,
    user_id: str,
    purpose: ConsentPurpose | str,
    *,
    scopes: tuple[str, ...] | list[str] | None = None,
    actor_id: str | None = None,
    actor_type: ActorType | str = ActorType.USER,
    source: str = "web",
) -> ConsentRecord:
    from database import _connect

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return record_decision(
            conn,
            user_id=user_id,
            purpose=purpose,
            decision=ConsentDecision.WITHDRAWN,
            actor_type=actor_type,
            actor_id=actor_id,
            scopes=scopes,
            source=source,
        )


def _policy_view(policy: ConsentPolicy, conn: SqlExecutor, user_id: str) -> dict[str, Any]:
    record = current_decision(conn, user_id, policy.purpose)
    return {
        "purpose": policy.purpose.value,
        "label": policy.label,
        "explanation": policy.explanation,
        "scopes": list(policy.scopes),
        "policy_version": policy.version,
        "granted": has_consent(conn, user_id, policy.purpose),
        "current": record.as_dict() if record else None,
        "needs_reconsent": bool(
            record is not None
            and record.active
            and record.policy_version != policy.version
        ),
    }


def snapshot(conn: SqlExecutor, user_id: str) -> dict[str, Any]:
    """Return every purpose's current state for the settings/API layer."""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ConsentError("consent state requires an authenticated user")
    return {
        "policy_version": POLICY_VERSION,
        "purposes": [
            _policy_view(policy, conn, user_id)
            for policy in POLICIES.values()
        ],
    }


def snapshot_at(db_path: str, user_id: str) -> dict[str, Any]:
    from database import _connect

    with _connect(db_path) as conn:
        return snapshot(conn, user_id)


def backfill_enabled_features(cursor: sqlite3.Cursor) -> None:
    """Migrate existing explicit opt-ins into the ledger once.

    A connected health provider, a saved AI provider key or a push subscription
    was already an explicit user action. Recording that prior choice as a
    system-sourced grant keeps those features working after enforcement starts;
    the row is still distinguishable from a fresh in-product decision.
    """

    if not _table_exists(cursor, TABLE):
        return

    def _grant(user_id: str, purpose: ConsentPurpose, source: str) -> None:
        if not user_id or current_decision(cursor, user_id, purpose) is not None:
            return
        policy = policy_for(purpose)
        _append(
            cursor,
            user_id=user_id,
            purpose=purpose,
            scope=ALL_SCOPES,
            scopes=policy.scopes,
            decision=ConsentDecision.GRANTED,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            source=source,
            decided_at=_timestamp(),
            revoked_at=None,
        )

    if _table_exists(cursor, "push_subscriptions"):
        for (user_id,) in cursor.execute(
            "SELECT DISTINCT user_id FROM push_subscriptions WHERE user_id IS NOT NULL"
        ).fetchall():
            _grant(str(user_id), ConsentPurpose.NOTIFICATIONS, "migration:push_subscription")

    if _table_exists(cursor, "health_connections"):
        for (user_id,) in cursor.execute(
            "SELECT DISTINCT user_id FROM health_connections "
            "WHERE user_id IS NOT NULL AND state = 'connected'"
        ).fetchall():
            _grant(str(user_id), ConsentPurpose.HEALTH_DATA, "migration:health_connection")

    if _table_exists(cursor, "user_api_keys"):
        placeholders = ", ".join("?" for _ in _AI_PROVIDERS)
        for (user_id,) in cursor.execute(
            f"SELECT DISTINCT user_id FROM user_api_keys "
            f"WHERE user_id IS NOT NULL AND provider IN ({placeholders}) "
            "AND COALESCE(status, 'active') = 'active'",
            _AI_PROVIDERS,
        ).fetchall():
            _grant(str(user_id), ConsentPurpose.AI_PROCESSING, "migration:ai_key")


__all__ = [
    "ALL_SCOPES",
    "POLICIES",
    "POLICY_VERSION",
    "TABLE",
    "ActorType",
    "ConsentDecision",
    "ConsentError",
    "ConsentPolicy",
    "ConsentPurpose",
    "ConsentRecord",
    "backfill_enabled_features",
    "current_decision",
    "ensure_schema",
    "grant_consent_at",
    "has_consent",
    "has_consent_at",
    "policy_for",
    "record_decision",
    "snapshot",
    "snapshot_at",
    "withdraw_consent_at",
]
