"""Tamper-evident, tenant-aware security-event audit log.

The audit log answers *who did what to which account or integration, when, from
which request, and did it work?* for the actions that change an account's
security posture: sign-ins, session revocation, credential and connector
changes, exports, deletions, administrative access and permission changes.

Design contract
---------------
* **Append-only.** Rows are written with a single ``INSERT`` and SQLite triggers
  reject ``UPDATE`` and ``DELETE`` so the immutable tenant column cannot be
  rewritten through the application connection.
* **Tamper-evident.** Every row stores ``prev_hash`` and ``event_hash``. The hash
  covers every column plus the previous hash and the sequence number, so editing,
  reordering or removing a row is detected from that row onwards. When a key is
  configured the digest is an HMAC-SHA-256, so a party that can only write to the
  database cannot recompute the chain. Rows written without a key still chain with
  plain SHA-256, which detects accidental damage but not a deliberate rewrite.
* **Tenant aware.** ``user_id`` is the tenant that owns the affected account or
  data (``NULL`` for unauthenticated or deployment-wide events). Callers derive it
  from the authenticated session, never from client input.
* **Safe by construction.** Metadata is flat, bounded, key-classified through
  ``data_classification`` and value-scrubbed. Network addresses and attempted
  login identities are stored only as keyed pseudonyms.
* **Never blocks the product.** ``record_security_event`` logs and swallows
  storage failures so an audit outage cannot take sign-in or settings down.
  Failures are logged at ERROR so they stay visible to operators.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from data_classification import redact_for_log
from log_redaction import redact_text

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
UNKEYED_KEY_ID = "unkeyed"
AUDIT_KEY_ENV = "AUDIT_LOG_HMAC_KEY"
SESSION_SECRET_ENV = "WEB_AUTH_SECRET"
MINIMUM_KEY_LENGTH = 32

TABLE = "security_audit_events"
_DERIVATION_CONTEXT = b"workout-agent/security-audit/v1/"
_PSEUDONYM_LENGTH = 24
_MAX_METADATA_ITEMS = 24
_MAX_LIST_ITEMS = 20
_MAX_STRING_LENGTH = 200
_MAX_METADATA_BYTES = 4096
_MAX_IDENTIFIER_LENGTH = 64
_UNSUPPORTED = "[UNSUPPORTED]"
_INVALID_IDENTIFIER = "[invalid]"


class SecurityAuditConfigurationError(RuntimeError):
    """Raised before startup when the audit key configuration is unsafe."""


class AuditEventType(StrEnum):
    """Closed vocabulary of audited actions and the alerts derived from them."""

    LOGIN = "auth.login"
    SESSION_REVOKED = "auth.session_revoked"
    CREDENTIAL_SAVED = "credential.saved"
    CREDENTIAL_DELETED = "credential.deleted"
    CONNECTOR_CONNECTED = "connector.connected"
    CONNECTOR_DISCONNECTED = "connector.disconnected"
    CONNECTOR_SYNC = "connector.sync"
    CONNECTOR_TEST = "connector.test"
    DATA_EXPORTED = "data.exported"
    DATA_DELETED = "data.deleted"
    ADMIN_ACCESS = "admin.access"
    PERMISSION_CHANGED = "permission.changed"
    ALERT_REPEATED_AUTH_FAILURES = "alert.repeated_auth_failures"
    ALERT_PRODUCTION_AUTH_DISABLED = "alert.production_auth_disabled"
    ALERT_UNUSUAL_EXPORT_ACTIVITY = "alert.unusual_export_activity"
    ALERT_CONNECTOR_ABUSE = "alert.connector_abuse"


ALERT_EVENT_TYPES = frozenset(
    event_type for event_type in AuditEventType if event_type.startswith("alert.")
)


class AuditOutcome(StrEnum):
    """Result of the audited action. ``RAISED`` is used only by alert events."""

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    RAISED = "raised"


class AuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ActorType(StrEnum):
    USER = "user"
    OPERATOR = "operator"
    ANONYMOUS = "anonymous"
    SYSTEM = "system"


class AuditSubjectType(StrEnum):
    ACCOUNT = "account"
    IDENTITY = "identity"
    SESSION = "session"
    CREDENTIAL = "credential"
    CONNECTOR = "connector"
    DATA = "data"
    PERMISSION = "permission"
    SOURCE = "source"
    SYSTEM = "system"


def default_severity(outcome: AuditOutcome) -> AuditSeverity:
    """Severity used when a caller does not state one explicitly."""

    if outcome is AuditOutcome.SUCCESS:
        return AuditSeverity.INFO
    return AuditSeverity.WARNING


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    """One audited action, before sanitisation and chaining.

    ``user_id`` is the tenant that owns the affected account or data. It must be
    derived from the authenticated session, never from a client-supplied value.
    """

    event_type: AuditEventType
    outcome: AuditOutcome
    actor_type: ActorType
    subject_type: AuditSubjectType
    actor_id: str | None = None
    user_id: str | None = None
    subject_id: str | None = None
    request_id: str | None = None
    source_ref: str | None = None
    severity: AuditSeverity | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            self.actor_type in {ActorType.USER, ActorType.OPERATOR}
            and not self.actor_id
        ):
            raise ValueError("user and operator audit events require an actor_id")
        if self.actor_type in {ActorType.ANONYMOUS, ActorType.SYSTEM} and self.actor_id:
            raise ValueError(
                "anonymous and system audit events cannot carry an actor_id"
            )
        if self.occurred_at is not None and self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """A persisted, chained audit row."""

    seq: int
    schema_version: int
    event_id: str
    occurred_at: str
    event_type: str
    outcome: str
    severity: str
    actor_type: str
    actor_id: str | None
    user_id: str | None
    subject_type: str
    subject_id: str | None
    request_id: str | None
    source_ref: str | None
    metadata: dict[str, Any]
    key_id: str
    prev_hash: str
    event_hash: str

    @property
    def occurred(self) -> datetime:
        return datetime.fromisoformat(self.occurred_at)


@dataclass(frozen=True, slots=True)
class AuditPolicy:
    """Bounds that keep the audit table from becoming a denial-of-service sink."""

    alert_cooldown: timedelta = timedelta(minutes=30)
    anonymous_events_per_window: int = 30
    anonymous_window: timedelta = timedelta(minutes=10)


DEFAULT_POLICY = AuditPolicy()


# ---------------------------------------------------------------------------
# Keys and pseudonyms
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditKey:
    """Digest key for the chain and for pseudonymised identifiers.

    ``secret`` is ``None`` only when no key material is configured. The
    ``key_id`` is a one-way fingerprint of the derived key and reveals nothing
    usable about the configured secret.
    """

    secret: bytes | None
    key_id: str

    @property
    def keyed(self) -> bool:
        return self.secret is not None

    def digest(self, payload: bytes) -> str:
        message = b"chain:" + payload
        if self.secret is None:
            return hashlib.sha256(message).hexdigest()
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def pseudonym(self, kind: str, value: str) -> str:
        """Return a stable, non-reversible reference for an address or identity."""

        message = f"pseudonym:{kind}:{value}".encode()
        if self.secret is None:
            digest = hashlib.sha256(message).hexdigest()
        else:
            digest = hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        return digest[:_PSEUDONYM_LENGTH]


UNKEYED = AuditKey(secret=None, key_id=UNKEYED_KEY_ID)


def _derive_key(material: str, label: str) -> AuditKey:
    secret = hmac.new(
        material.encode("utf-8"),
        _DERIVATION_CONTEXT + label.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return AuditKey(secret=secret, key_id=hashlib.sha256(secret).hexdigest()[:12])


def resolve_audit_key(environ: Mapping[str, str] | None = None) -> AuditKey:
    """Resolve the digest key from the environment.

    ``AUDIT_LOG_HMAC_KEY`` wins when present and must be at least 32 characters.
    Otherwise a purpose-separated key is derived from ``WEB_AUTH_SECRET`` (which
    production already requires), so production chains are always keyed. With no
    key material at all (local development) the chain is unkeyed.
    """

    source = os.environ if environ is None else environ
    configured = source.get(AUDIT_KEY_ENV, "").strip()
    if configured:
        if len(configured) < MINIMUM_KEY_LENGTH:
            raise SecurityAuditConfigurationError(
                f"{AUDIT_KEY_ENV} must contain at least {MINIMUM_KEY_LENGTH} characters."
            )
        return _derive_key(configured, "dedicated")

    session_secret = source.get(SESSION_SECRET_ENV, "").strip()
    if len(session_secret) >= MINIMUM_KEY_LENGTH:
        return _derive_key(session_secret, "session-derived")
    return UNKEYED


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------

_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"),
        "[REDACTED:JWT]",
    ),
    (
        re.compile(
            r"(?i)\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abprs])[-_][A-Za-z0-9_-]{12,}"
        ),
        "[REDACTED:KEY]",
    ),
    (
        re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"),
        "[REDACTED:UUID]",
    ),
    (re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"), "[REDACTED:EMAIL]"),
    (re.compile(r"\?\S+"), "?[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(?:code|state|token|access_token|refresh_token|id_token|api_key|"
            r"apikey|client_secret|secret|password|key)=\S+"
        ),
        "[REDACTED:PARAM]",
    ),
    (re.compile(r"[A-Za-z0-9_+/=]{32,}"), "[REDACTED:OPAQUE]"),
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]+")
_METADATA_KEY_INVALID = re.compile(r"[^a-z0-9_]")


def scrub_text(value: str, *, limit: int = _MAX_STRING_LENGTH) -> str:
    """Remove secret-shaped fragments, control characters and excess length."""

    # The central layer first (registered and environment secrets, credential
    # pairs, query values, addresses), then the audit-specific scrubbers below.
    text = redact_text(_CONTROL_CHARACTERS.sub(" ", str(value)))
    for pattern, replacement in _SCRUBBERS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def _metadata_key(raw_key: str) -> str:
    key = _METADATA_KEY_INVALID.sub("_", str(raw_key).strip().lower())[:40]
    if not key or not key[0].isalpha():
        key = f"k_{key}"[:40]
    return key


def _metadata_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return scrub_text(value)
    return _UNSUPPORTED


def _metadata_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, frozenset)):
        items = (
            sorted(value, key=str)
            if isinstance(value, (set, frozenset))
            else list(value)
        )
        return [_metadata_scalar(item) for item in items[:_MAX_LIST_ITEMS]]
    return _metadata_scalar(value)


def sanitise_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return flat, bounded, secret-free metadata. Never raises.

    Keys are classified with the repository's data-classification policy, so a
    credential, identifier, health, workout or prompt field is redacted even if a
    caller adds it by mistake. Nested structures are replaced with a marker.
    """

    if not metadata:
        return {}
    classified = redact_for_log(dict(metadata))
    result: dict[str, Any] = {}
    for raw_key in sorted(classified)[:_MAX_METADATA_ITEMS]:
        result[_metadata_key(raw_key)] = _metadata_value(classified[raw_key])
    if len(canonical_json(result)) > _MAX_METADATA_BYTES:
        return {"truncated": True}
    return result


def _identifier(value: str | None) -> str | None:
    """Accept only compact identifier-shaped values for indexed columns."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > _MAX_IDENTIFIER_LENGTH or not _IDENTIFIER.fullmatch(text):
        return _INVALID_IDENTIFIER
    return text


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        event_id TEXT NOT NULL UNIQUE,
        occurred_at TEXT NOT NULL,
        event_type TEXT NOT NULL,
        outcome TEXT NOT NULL,
        severity TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        actor_id TEXT,
        user_id TEXT REFERENCES users(id),
        subject_type TEXT NOT NULL,
        subject_id TEXT,
        request_id TEXT,
        source_ref TEXT,
        metadata_json TEXT NOT NULL,
        key_id TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL UNIQUE
    )
    """,
    (
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_user "
        f"ON {TABLE} (user_id, event_type, occurred_at)"
    ),
    (
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_source "
        f"ON {TABLE} (source_ref, event_type, occurred_at)"
    ),
    (
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_subject "
        f"ON {TABLE} (event_type, subject_id, occurred_at)"
    ),
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_request ON {TABLE} (request_id)",
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_no_update
    BEFORE UPDATE ON {TABLE}
    BEGIN
        SELECT RAISE(ABORT, 'security audit events are append-only');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_no_delete
    BEFORE DELETE ON {TABLE}
    BEGIN
        SELECT RAISE(ABORT, 'security audit events are append-only');
    END
    """,
)

_COLUMN_NAMES = (
    "seq",
    "schema_version",
    "event_id",
    "occurred_at",
    "event_type",
    "outcome",
    "severity",
    "actor_type",
    "actor_id",
    "user_id",
    "subject_type",
    "subject_id",
    "request_id",
    "source_ref",
    "metadata_json",
    "key_id",
    "prev_hash",
    "event_hash",
)
_COLUMNS = ", ".join(_COLUMN_NAMES)
# Every stored column except the digest itself is covered by the digest.
_HASHED_COLUMNS = tuple(name for name in _COLUMN_NAMES if name != "event_hash")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the audit table, tenant-first index and append-only triggers."""

    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def _open(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def open_readonly(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("database does not exist")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _record_from_fields(fields: Mapping[str, Any]) -> AuditRecord:
    return AuditRecord(
        seq=int(fields["seq"]),
        schema_version=int(fields["schema_version"]),
        event_id=str(fields["event_id"]),
        occurred_at=str(fields["occurred_at"]),
        event_type=str(fields["event_type"]),
        outcome=str(fields["outcome"]),
        severity=str(fields["severity"]),
        actor_type=str(fields["actor_type"]),
        actor_id=fields["actor_id"],
        user_id=fields["user_id"],
        subject_type=str(fields["subject_type"]),
        subject_id=fields["subject_id"],
        request_id=fields["request_id"],
        source_ref=fields["source_ref"],
        metadata=json.loads(str(fields["metadata_json"])),
        key_id=str(fields["key_id"]),
        prev_hash=str(fields["prev_hash"]),
        event_hash=str(fields["event_hash"]),
    )


def event_hash_for(key: AuditKey, fields: Mapping[str, Any]) -> str:
    """Digest the hashed columns of one row in a canonical, stable encoding."""

    payload = canonical_json({name: fields[name] for name in _HASHED_COLUMNS})
    return key.digest(payload.encode("ascii"))


def _is_suppressed(
    conn: sqlite3.Connection,
    event: SecurityEvent,
    fields: Mapping[str, Any],
    occurred: datetime,
    policy: AuditPolicy,
) -> bool:
    """Apply the in-transaction bounds that keep the audit table finite."""

    if event.event_type in ALERT_EVENT_TYPES:
        cutoff = _timestamp(occurred - policy.alert_cooldown)
        row = conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE event_type = ? AND subject_type = ? "
            "AND subject_id IS ? AND severity = ? AND occurred_at >= ? LIMIT 1",
            (
                fields["event_type"],
                fields["subject_type"],
                fields["subject_id"],
                fields["severity"],
                cutoff,
            ),
        ).fetchone()
        return row is not None

    if event.actor_type is ActorType.ANONYMOUS and fields["source_ref"]:
        cutoff = _timestamp(occurred - policy.anonymous_window)
        row = conn.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE actor_type = ? AND source_ref = ? "
            "AND event_type = ? AND occurred_at >= ?",
            (
                ActorType.ANONYMOUS.value,
                fields["source_ref"],
                fields["event_type"],
                cutoff,
            ),
        ).fetchone()
        return int(row[0]) >= policy.anonymous_events_per_window
    return False


def append_event(
    db_path: str,
    event: SecurityEvent,
    *,
    key: AuditKey | None = None,
    policy: AuditPolicy = DEFAULT_POLICY,
) -> AuditRecord | None:
    """Append one chained event and return it, or ``None`` if bounded away.

    Writers are serialised with ``BEGIN IMMEDIATE`` so concurrent web workers and
    the scheduler extend a single chain. Raises storage errors; most callers want
    :func:`record_security_event`, which never raises.
    """

    resolved_key = key if key is not None else resolve_audit_key()
    occurred = (event.occurred_at or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    )
    severity = event.severity or default_severity(event.outcome)
    metadata_json = canonical_json(sanitise_metadata(event.metadata))

    fields: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "occurred_at": _timestamp(occurred),
        "event_type": event.event_type.value,
        "outcome": event.outcome.value,
        "severity": severity.value,
        "actor_type": event.actor_type.value,
        "actor_id": _identifier(event.actor_id),
        "user_id": _identifier(event.user_id),
        "subject_type": event.subject_type.value,
        "subject_id": _identifier(event.subject_id),
        "request_id": _identifier(event.request_id),
        "source_ref": _identifier(event.source_ref),
        "metadata_json": metadata_json,
        "key_id": resolved_key.key_id,
    }

    conn = _open(db_path)
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _is_suppressed(conn, event, fields, occurred, policy):
                conn.execute("ROLLBACK")
                logger.info(
                    "security_audit_suppressed event_type=%s outcome=%s",
                    fields["event_type"],
                    fields["outcome"],
                )
                return None

            head = conn.execute(
                f"SELECT seq, event_hash FROM {TABLE} ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            sequence = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?", (TABLE,)
            ).fetchone()
            last_seq, prev_hash = (
                (int(head[0]), str(head[1])) if head else (0, GENESIS_HASH)
            )
            # AUTOINCREMENT remembers the highest sequence ever issued, so rows
            # removed from the tail leave a permanent, detectable gap.
            fields["seq"] = max(last_seq, int(sequence[0]) if sequence else 0) + 1
            fields["prev_hash"] = prev_hash
            fields["event_hash"] = event_hash_for(resolved_key, fields)

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


def record_security_event(
    db_path: str,
    event: SecurityEvent,
    *,
    key: AuditKey | None = None,
    policy: AuditPolicy = DEFAULT_POLICY,
) -> AuditRecord | None:
    """Append an event without ever raising into the calling request.

    Returns ``None`` when the event was bounded away or could not be stored. A
    storage failure is logged at ERROR with the event type only, so operators see
    the gap without the log gaining sensitive detail.
    """

    try:
        record = append_event(db_path, event, key=key, policy=policy)
    except (sqlite3.Error, OSError, ValueError, SecurityAuditConfigurationError):
        logger.exception(
            "security_audit_write_failed event_type=%s outcome=%s",
            event.event_type.value,
            event.outcome.value,
        )
        return None
    if record is not None:
        logger.info(
            "security_audit seq=%d event_type=%s outcome=%s severity=%s "
            "request_id=%s event_hash=%s",
            record.seq,
            record.event_type,
            record.outcome,
            record.severity,
            record.request_id,
            record.event_hash,
        )
    return record


# ---------------------------------------------------------------------------
# Verification and operator reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """Outcome of walking the chain from the first row to the head."""

    ok: bool
    checked: int
    head_seq: int | None
    head_hash: str | None
    keyed: bool
    unverifiable: int = 0
    unkeyed: int = 0
    failure_seq: int | None = None
    failure_reason: str | None = None

    @property
    def fully_authenticated(self) -> bool:
        """True only when every row's digest was recomputed with a known key.

        ``ok`` alone is not enough for a gate: a row stamped with an unknown key
        id keeps its sequence and link but its digest cannot be checked, so an
        attacker who can write the database could claim a "rotation".
        """

        return self.ok and self.unverifiable == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "fully_authenticated": self.fully_authenticated,
            "checked": self.checked,
            "head_seq": self.head_seq,
            "head_hash": self.head_hash,
            "keyed": self.keyed,
            "unverifiable": self.unverifiable,
            "unkeyed": self.unkeyed,
            "failure_seq": self.failure_seq,
            "failure_reason": self.failure_reason,
        }


def _row_fields(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_COLUMN_NAMES, row, strict=True))


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def verify_chain(db_path: str, *, key: AuditKey | None = None) -> ChainVerification:
    """Walk every row and report the first sequence, link or digest violation.

    Rows signed with a key that is not available (for example after a secret
    rotation) cannot have their digest recomputed; their sequence and link are
    still verified and they are counted in ``unverifiable``. Unkeyed rows are
    counted in ``unkeyed`` and are rejected (``key_downgrade``) once any keyed row
    has been seen, so a database-only attacker cannot rewrite a keyed chain with
    plain SHA-256.
    """

    resolved_key = key if key is not None else resolve_audit_key()
    checked = 0
    unverifiable = 0
    unkeyed = 0
    seen_keyed = False
    previous_seq = 0
    previous_hash = GENESIS_HASH

    def failure(seq: int, reason: str) -> ChainVerification:
        return ChainVerification(
            ok=False,
            checked=checked,
            head_seq=previous_seq or None,
            head_hash=previous_hash if previous_seq else None,
            keyed=resolved_key.keyed,
            unverifiable=unverifiable,
            unkeyed=unkeyed,
            failure_seq=seq,
            failure_reason=reason,
        )

    with open_readonly(db_path) as conn:
        if not _table_exists(conn):
            return ChainVerification(
                ok=True,
                checked=0,
                head_seq=None,
                head_hash=None,
                keyed=resolved_key.keyed,
            )
        cursor = conn.execute(f"SELECT {_COLUMNS} FROM {TABLE} ORDER BY seq")
        while batch := cursor.fetchmany(500):
            for row in batch:
                fields = _row_fields(row)
                seq = int(fields["seq"])
                if seq != previous_seq + 1:
                    return failure(seq, "sequence_gap")
                if fields["prev_hash"] != previous_hash:
                    return failure(seq, "broken_link")
                row_key: AuditKey | None
                if fields["key_id"] == UNKEYED_KEY_ID:
                    if seen_keyed:
                        return failure(seq, "key_downgrade")
                    unkeyed += 1
                    row_key = UNKEYED
                else:
                    seen_keyed = True
                    row_key = (
                        resolved_key
                        if fields["key_id"] == resolved_key.key_id
                        else None
                    )
                if row_key is None:
                    unverifiable += 1
                elif not hmac.compare_digest(
                    event_hash_for(row_key, fields), str(fields["event_hash"])
                ):
                    return failure(seq, "hash_mismatch")
                previous_seq = seq
                previous_hash = str(fields["event_hash"])
                checked += 1

        issued = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?", (TABLE,)
        ).fetchone()
        if issued is not None and int(issued[0]) != previous_seq:
            return failure(previous_seq + 1, "tail_truncated")

    return ChainVerification(
        ok=True,
        checked=checked,
        head_seq=previous_seq or None,
        head_hash=previous_hash if previous_seq else None,
        keyed=resolved_key.keyed,
        unverifiable=unverifiable,
        unkeyed=unkeyed,
    )


def iter_alert_records(db_path: str, *, limit: int = 10) -> Iterator[AuditRecord]:
    """Yield the newest alert events across all tenants (operator use only)."""

    alert_types = sorted(event_type.value for event_type in ALERT_EVENT_TYPES)
    placeholders = ", ".join("?" * len(alert_types))
    with open_readonly(db_path) as conn:
        if not _table_exists(conn):
            return
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE event_type IN ({placeholders}) "
            "ORDER BY seq DESC LIMIT ?",
            (*alert_types, max(1, limit)),
        ).fetchall()
    for row in rows:
        yield _record_from_fields(_row_fields(row))


def count_events(db_path: str) -> dict[str, int]:
    """Return event counts keyed by ``event_type`` (operator use only)."""

    with open_readonly(db_path) as conn:
        if not _table_exists(conn):
            return {}
        rows = conn.execute(
            f"SELECT event_type, COUNT(*) FROM {TABLE} GROUP BY event_type "
            "ORDER BY event_type"
        ).fetchall()
    return {str(name): int(count) for name, count in rows}
