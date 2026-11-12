"""Account and health-data deletion workflow.

Lifecycle: typed confirmation -> request (with a cancellation window for account
closure) -> fenced, idempotent, step-by-step erasure -> verification -> a
personal-data-free tombstone. The authoritative description of what is erased and
what is retained lives in ``docs/ACCOUNT_DELETION.md``.

Safety properties:

* Every statement is scoped by the owner's ``user_id`` and that identifier always
  comes from the authenticated session or from a persisted request row - never
  from a client-supplied owner field.
* A request fences the account: scheduled jobs skip it, and once erasure starts
  the web API refuses it until the tombstone exists (then it fails closed).
* Erasure is verified. A step that leaves residual rows fails the request rather
  than reporting success, and the account is only closed (``users`` row removed,
  tombstone written) in a single transaction that re-verifies under the write lock.
* The ledger tables hold opaque identifiers, timestamps and row counts only.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from health_delete import HEALTH_USER_TABLES

logger = logging.getLogger(__name__)

POLICY_VERSION = "1.0.0"
EXPORT_DIRNAME = "exports"
HEALTH_CONFIRMATION = "DELETE HEALTH DATA"

# The shared single-user tenant created by init_db; it has no authenticated
# owner, so it can never be closed through this workflow.
_LEGACY_EMAIL = "legacy@local"

DEFAULT_GRACE_DAYS = 7
MAX_GRACE_DAYS = 30
DEFAULT_TOMBSTONE_RETENTION_DAYS = 365
# Tombstones must outlive the oldest backup that can still contain erased data
# (docs/BACKUP_RESTORE_RUNBOOK.md keeps 35 days), so a restore can be reconciled.
MIN_TOMBSTONE_RETENTION_DAYS = 35
MAX_TOMBSTONE_RETENTION_DAYS = 3650

LEASE = timedelta(minutes=15)
RETRY_BACKOFF = timedelta(minutes=5)
MAX_ATTEMPTS = 5
STRAGGLER_SWEEP_WINDOW = timedelta(minutes=15)
_FINALISE_PASSES = 3
_BATCH_SIZE = 20

_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class DeletionScope(StrEnum):
    ACCOUNT = "account"
    HEALTH_DATA = "health_data"


class DeletionState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AccountGate(StrEnum):
    """What the rest of the application may do for an account."""

    OPEN = "open"
    PENDING = "pending"  # closure scheduled: usable, background work paused
    LOCKED = "locked"  # erasing (or awaiting an operator): unusable
    CLOSED = "closed"  # tombstoned: every session fails closed


class Step(StrEnum):
    QUEUED_JOBS = "queued_jobs"
    NOTIFICATIONS = "notifications"
    CONNECTOR_TOKENS = "connector_tokens"
    DERIVED_INSIGHTS = "derived_insights"
    PRIMARY_RECORDS = "primary_records"
    CACHES = "caches"
    SEARCH_INDEXES = "search_indexes"
    RETAINED_EXPORTS = "retained_exports"
    UNREGISTERED_STORES = "unregistered_stores"
    ACCOUNT_IDENTITY = "account_identity"


STEP_LABELS: dict[Step, str] = {
    Step.QUEUED_JOBS: "Stop scheduled and queued jobs",
    Step.NOTIFICATIONS: "Delete notifications and push subscriptions",
    Step.CONNECTOR_TOKENS: "Delete connector tokens and API keys",
    Step.DERIVED_INSIGHTS: "Delete coaching insights, chat and check-ins",
    Step.PRIMARY_RECORDS: "Delete workout, programme and health records",
    Step.CACHES: "Clear server-side caches",
    Step.SEARCH_INDEXES: "Clear search indexes",
    Step.RETAINED_EXPORTS: "Delete retained exports",
    Step.UNREGISTERED_STORES: "Sweep any other records tied to the account",
    Step.ACCOUNT_IDENTITY: "Close the account and write the deletion tombstone",
}

PLANS: dict[DeletionScope, tuple[Step, ...]] = {
    DeletionScope.ACCOUNT: (
        Step.QUEUED_JOBS,
        Step.NOTIFICATIONS,
        Step.CONNECTOR_TOKENS,
        Step.DERIVED_INSIGHTS,
        Step.PRIMARY_RECORDS,
        Step.CACHES,
        Step.SEARCH_INDEXES,
        Step.RETAINED_EXPORTS,
        Step.UNREGISTERED_STORES,
        Step.ACCOUNT_IDENTITY,
    ),
    DeletionScope.HEALTH_DATA: (
        Step.CONNECTOR_TOKENS,
        Step.DERIVED_INSIGHTS,
        Step.PRIMARY_RECORDS,
        Step.SEARCH_INDEXES,
        Step.RETAINED_EXPORTS,
    ),
}

# user_api_keys.provider values that import health data.
HEALTH_PROVIDER_KEYS: tuple[str, ...] = (
    "fitbit",
    "garmin",
    "google_health",
    "health_connect",
    "oura",
    "polar",
    "withings",
)


@dataclass(frozen=True, slots=True)
class DataStore:
    """One SQL table holding user-owned rows."""

    table: str
    step: Step
    # Column holding the owning account id. Almost always ``user_id``; a table that
    # links to an account another way (e.g. ``accepted_user_id``) names it here.
    owner: str = "user_id"
    # Erased by the health_data scope (all of it, or only ``health_filter`` rows).
    health: bool = False
    health_filter: tuple[str, tuple[str, ...]] | None = None
    # Created lazily or by an optional migration, so it may not exist.
    optional: bool = False


def _health_table_store(table: str) -> DataStore:
    step = (
        Step.CONNECTOR_TOKENS if table == "health_connections" else Step.PRIMARY_RECORDS
    )
    return DataStore(table, step, health=True, optional=True)


USER_DATA_STORES: tuple[DataStore, ...] = (
    DataStore("notifications", Step.NOTIFICATIONS),
    DataStore("push_subscriptions", Step.NOTIFICATIONS),
    DataStore(
        "user_api_keys",
        Step.CONNECTOR_TOKENS,
        health=True,
        health_filter=(
            "provider IN (" + ", ".join("?" for _ in HEALTH_PROVIDER_KEYS) + ")",
            HEALTH_PROVIDER_KEYS,
        ),
    ),
    DataStore(
        "hevy_meta",
        Step.CONNECTOR_TOKENS,
        health=True,
        health_filter=("key LIKE ? ESCAPE '\\'", ("google\\_health\\_%",)),
    ),
    DataStore("dashboard_insights", Step.DERIVED_INSIGHTS, health=True),
    DataStore("deep_correlations", Step.DERIVED_INSIGHTS, health=True),
    DataStore("reasoning_logs", Step.DERIVED_INSIGHTS, health=True),
    DataStore("chat_messages", Step.DERIVED_INSIGHTS, health=True),
    DataStore("check_ins", Step.DERIVED_INSIGHTS, health=True),
    DataStore("workout_history", Step.PRIMARY_RECORDS),
    DataStore("exercise_progress", Step.PRIMARY_RECORDS),
    DataStore("daily_log", Step.PRIMARY_RECORDS),
    DataStore("programme_state", Step.PRIMARY_RECORDS),
    DataStore("programmes", Step.PRIMARY_RECORDS),
    DataStore("programme_revisions", Step.PRIMARY_RECORDS),
    DataStore("programme_routines", Step.PRIMARY_RECORDS),
    DataStore("programme_blocks", Step.PRIMARY_RECORDS),
    DataStore("exercise_prescriptions", Step.PRIMARY_RECORDS),
    DataStore("programme_week_states", Step.PRIMARY_RECORDS),
    DataStore("adaptation_decisions", Step.PRIMARY_RECORDS),
    DataStore("programme_decisions", Step.PRIMARY_RECORDS),
    DataStore("user_preferences", Step.PRIMARY_RECORDS),
    DataStore("onboarding_skips", Step.PRIMARY_RECORDS),
    DataStore("training_rules", Step.PRIMARY_RECORDS),
    DataStore("hevy_routines", Step.PRIMARY_RECORDS),
    DataStore("hevy_routine_snapshots", Step.PRIMARY_RECORDS),
    DataStore("routine_workout_matches", Step.PRIMARY_RECORDS),
    DataStore("routine_match_overrides", Step.PRIMARY_RECORDS),
    DataStore("exercise_history_baselines", Step.PRIMARY_RECORDS),
    DataStore("hevy_connections", Step.CONNECTOR_TOKENS),
    DataStore("user_identities", Step.PRIMARY_RECORDS),
    DataStore("user_sessions", Step.CACHES),
    DataStore("body_metrics", Step.PRIMARY_RECORDS, health=True),
    DataStore("record_provenance", Step.DERIVED_INSIGHTS, health=True),
    # Export jobs (account_export.py). The archives sit in the retained-exports
    # directory erased by the same step, and a health-data erasure removes both
    # because an archive embeds that data.
    DataStore("account_export_requests", Step.RETAINED_EXPORTS, health=True),
    DataStore("csrf_tokens", Step.CACHES, optional=True),
    # Canonical health platform tables (health_schema.py). Optional because the
    # additive migration may not have been applied to this database.
    *(_health_table_store(table) for table in HEALTH_USER_TABLES),
)

# Tables that intentionally survive erasure (documented retention exceptions).
# The deletion ledger holds opaque identifiers, timestamps and row counts only.
# The two audit trails are append-only evidence with pseudonymous references;
# the application never deletes them, including during account closure, and they
# never hold secret material.
RETAINED_LEDGER_TABLES: tuple[str, ...] = (
    "account_deletion_requests",
    "account_deletion_steps",
    "account_tombstones",
    "credential_audit_events",
    "security_audit_events",
)

# Search indexes that hold user content. There are none today: the check in
# _unregistered_search_indexes() fails closed if one is introduced unregistered.
REGISTERED_SEARCH_INDEXES: tuple[str, ...] = ()


class DeletionError(Exception):
    """A rejected request. ``code`` is a stable, non-sensitive identifier."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class _ResidualData(Exception):
    """Erasure left rows behind; the request must not report success."""


class StepStatus(TypedDict):
    step: str
    label: str
    state: str
    rows_affected: int
    remaining_after: int | None


class DeletionStatus(TypedDict):
    request_id: str
    scope: str
    state: str
    requested_at: str
    execute_after: str
    completed_at: str | None
    cancelled_at: str | None
    can_cancel: bool
    attempts: int
    error_code: str | None
    steps: list[StepStatus]
    progress_percent: int


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _clock(started: datetime) -> datetime:
    """Wall-clock time, but never earlier than the caller's reference time."""
    return max(_utcnow(), started)


def _ts(moment: datetime) -> str:
    """Fixed-width UTC timestamp so lexical comparison equals time order."""
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _bounded_days(name: str, raw: str, default: int, minimum: int, maximum: int) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s; using %d.", name, default)
        return default
    clamped = min(max(value, minimum), maximum)
    if clamped != value:
        logger.warning(
            "%s is outside %d-%d; using %d.", name, minimum, maximum, clamped
        )
    return clamped


def grace_days() -> int:
    """Cancellation window for account closure (ACCOUNT_DELETION_GRACE_DAYS)."""
    return _bounded_days(
        "ACCOUNT_DELETION_GRACE_DAYS",
        os.environ.get("ACCOUNT_DELETION_GRACE_DAYS", ""),
        DEFAULT_GRACE_DAYS,
        0,
        MAX_GRACE_DAYS,
    )


def tombstone_retention_days() -> int:
    """How long a tombstone is kept (ACCOUNT_TOMBSTONE_RETENTION_DAYS)."""
    return _bounded_days(
        "ACCOUNT_TOMBSTONE_RETENTION_DAYS",
        os.environ.get("ACCOUNT_TOMBSTONE_RETENTION_DAYS", ""),
        DEFAULT_TOMBSTONE_RETENTION_DAYS,
        MIN_TOMBSTONE_RETENTION_DAYS,
        MAX_TOMBSTONE_RETENTION_DAYS,
    )


def required_confirmation(scope: DeletionScope, email: str) -> str:
    """The exact text the user must type to confirm *scope*."""
    return email if scope is DeletionScope.ACCOUNT else HEALTH_CONFIRMATION


def _normalise_confirmation(text: str) -> str:
    return " ".join(text.split()).casefold()


@contextmanager
def _transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """A write transaction that holds SQLite's write lock from the first statement."""
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            with contextlib.suppress(sqlite3.OperationalError):
                # SQLite may already have rolled back after a fatal I/O error.
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


@contextmanager
def _reader(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        yield conn
    finally:
        conn.close()


def _quote(identifier: str) -> str:
    """Quote an SQL identifier taken from the manifest or from sqlite_master."""
    return '"' + identifier.replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Account gate
# ---------------------------------------------------------------------------


def account_gate(db_path: str, user_id: str) -> AccountGate:
    """Return how far deletion has progressed for *user_id*.

    A database without the ledger tables cannot contain a deletion, so a missing
    table means OPEN; every other database error propagates (fail closed).
    """
    try:
        with _reader(db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM account_tombstones WHERE user_id = ?", (user_id,)
            ).fetchone():
                return AccountGate.CLOSED
            row = conn.execute(
                "SELECT state FROM account_deletion_requests "
                "WHERE user_id = ? AND state IN ('pending', 'in_progress', 'failed')",
                (user_id,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return AccountGate.OPEN
        raise
    if row is None:
        return AccountGate.OPEN
    if row["state"] == DeletionState.PENDING:
        return AccountGate.PENDING
    return AccountGate.LOCKED


def is_account_closing(db_path: str, user_id: str) -> bool:
    """True when background work for *user_id* must not start or continue."""
    return account_gate(db_path, user_id) is not AccountGate.OPEN


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _load_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, email FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def _is_eligible(user: sqlite3.Row | None) -> bool:
    return user is not None and str(user["email"]).strip().lower() != _LEGACY_EMAIL


def _require_status(status: DeletionStatus | None) -> DeletionStatus:
    if status is None:  # pragma: no cover - a just-written request always exists
        raise DeletionError("not_found")
    return status


def request_deletion(
    db_path: str,
    user_id: str,
    scope: DeletionScope | str,
    confirmation: str,
    *,
    now: datetime | None = None,
) -> DeletionStatus:
    """Record a confirmed deletion request for the authenticated user.

    Account closure waits out the cancellation window; health-data erasure is due
    immediately. Callers run :func:`execute_deletion` afterwards, which only does
    work once the request is due.
    """
    try:
        scope = DeletionScope(scope)
    except ValueError as exc:
        raise DeletionError("invalid_scope") from exc
    if not user_id:
        raise DeletionError("not_eligible")
    moment = now or _utcnow()
    delay = (
        timedelta(days=grace_days()) if scope is DeletionScope.ACCOUNT else timedelta(0)
    )
    due = _ts(moment + delay)
    request_id = uuid4().hex

    with _transaction(db_path) as conn:
        user = _load_user(conn, user_id)
        if user is None or not _is_eligible(user):
            raise DeletionError("not_eligible")
        expected = required_confirmation(scope, str(user["email"]))
        if _normalise_confirmation(confirmation) != _normalise_confirmation(expected):
            raise DeletionError("confirmation_mismatch")
        if conn.execute(
            "SELECT 1 FROM account_tombstones WHERE user_id = ?", (user_id,)
        ).fetchone():
            raise DeletionError("not_eligible")
        try:
            conn.execute(
                "INSERT INTO account_deletion_requests "
                "(id, user_id, scope, state, requested_at, execute_after, next_attempt_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (request_id, user_id, scope.value, _ts(moment), due, due),
            )
        except sqlite3.IntegrityError as exc:
            raise DeletionError("already_requested") from exc
        for step in PLANS[scope]:
            conn.execute(
                "INSERT INTO account_deletion_steps (request_id, step, state) "
                "VALUES (?, ?, 'pending')",
                (request_id, step.value),
            )
    _audit("requested", request_id, scope, window_days=delay.days)
    return _require_status(get_status(db_path, request_id, now=moment))


def cancel_deletion(
    db_path: str, user_id: str, *, now: datetime | None = None
) -> DeletionStatus:
    """Cancel the caller's pending request while its window is still open."""
    moment = now or _utcnow()
    with _transaction(db_path) as conn:
        row = conn.execute(
            "SELECT id, scope, state FROM account_deletion_requests "
            "WHERE user_id = ? AND state IN ('pending', 'in_progress', 'failed')",
            (user_id,),
        ).fetchone()
        if row is None:
            raise DeletionError("not_found")
        cancelled = conn.execute(
            "UPDATE account_deletion_requests "
            "SET state = 'cancelled', cancelled_at = ?, error_code = NULL "
            "WHERE id = ? AND state = 'pending' AND execute_after > ?",
            (_ts(moment), row["id"], _ts(moment)),
        ).rowcount
        if cancelled != 1:
            raise DeletionError("not_cancellable")
        request_id = str(row["id"])
        scope = DeletionScope(row["scope"])
    _audit("cancelled", request_id, scope)
    return _require_status(get_status(db_path, request_id, now=moment))


def get_status(
    db_path: str, request_id: str, *, now: datetime | None = None
) -> DeletionStatus | None:
    with _reader(db_path) as conn:
        return _status(conn, request_id, now or _utcnow())


def _status(
    conn: sqlite3.Connection, request_id: str, now: datetime
) -> DeletionStatus | None:
    row = conn.execute(
        "SELECT id, scope, state, requested_at, execute_after, completed_at, "
        "cancelled_at, attempts, error_code FROM account_deletion_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    scope = DeletionScope(row["scope"])
    recorded = {
        step["step"]: step
        for step in conn.execute(
            "SELECT step, state, rows_affected, remaining_after "
            "FROM account_deletion_steps WHERE request_id = ?",
            (request_id,),
        )
    }
    steps: list[StepStatus] = []
    if row["state"] != DeletionState.CANCELLED:
        for step in PLANS[scope]:
            saved = recorded.get(step.value)
            steps.append(
                StepStatus(
                    step=step.value,
                    label=STEP_LABELS[step],
                    state=str(saved["state"]) if saved else "pending",
                    rows_affected=int(saved["rows_affected"]) if saved else 0,
                    remaining_after=saved["remaining_after"] if saved else None,
                )
            )
    done = sum(1 for step in steps if step["state"] == "done")
    percent = (
        100
        if row["state"] == DeletionState.COMPLETED
        else (int(done * 100 / len(steps)) if steps else 0)
    )
    return DeletionStatus(
        request_id=str(row["id"]),
        scope=str(row["scope"]),
        state=str(row["state"]),
        requested_at=str(row["requested_at"]),
        execute_after=str(row["execute_after"]),
        completed_at=row["completed_at"],
        cancelled_at=row["cancelled_at"],
        can_cancel=row["state"] == DeletionState.PENDING
        and str(row["execute_after"]) > _ts(now),
        attempts=int(row["attempts"]),
        error_code=row["error_code"],
        steps=steps,
        progress_percent=percent,
    )


def describe(
    db_path: str, user_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """What the Settings page needs: eligibility, required text and latest request."""
    moment = now or _utcnow()
    with _reader(db_path) as conn:
        user = _load_user(conn, user_id)
        if user is None or not _is_eligible(user):
            return {"eligible": False, "reason": "not_eligible", "request": None}
        latest = conn.execute(
            "SELECT id FROM account_deletion_requests WHERE user_id = ? "
            "ORDER BY (state IN ('pending', 'in_progress', 'failed')) DESC, "
            "requested_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        request = _status(conn, str(latest["id"]), moment) if latest else None
    return {
        "eligible": True,
        "reason": None,
        "scopes": [
            {
                "scope": DeletionScope.HEALTH_DATA.value,
                "required_confirmation": HEALTH_CONFIRMATION,
                "grace_days": 0,
            },
            {
                "scope": DeletionScope.ACCOUNT.value,
                "required_confirmation": str(user["email"]),
                "grace_days": grace_days(),
            },
        ],
        "request": request,
    }


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------


def _predicate(store: DataStore, scope: DeletionScope) -> tuple[str, tuple[str, ...]]:
    owned = f"{_quote(store.owner)} = ?"
    if scope is DeletionScope.HEALTH_DATA and store.health_filter is not None:
        clause, params = store.health_filter
        return f"{owned} AND ({clause})", params
    return owned, ()


def _stores_for(step: Step, scope: DeletionScope) -> list[DataStore]:
    return [
        store
        for store in USER_DATA_STORES
        if store.step is step and (scope is DeletionScope.ACCOUNT or store.health)
    ]


def export_directory(db_path: str, user_id: str) -> Path | None:
    """Where retained exports for *user_id* must live: ``<data dir>/exports/<id>``.

    Returns ``None`` for an identifier that is not a safe single path component,
    so a hostile id can never address anything outside the exports root.
    """
    if not _SAFE_PATH_COMPONENT.fullmatch(user_id):
        return None
    return Path(db_path).resolve().parent / EXPORT_DIRNAME / user_id


def _unregistered_user_tables(conn: sqlite3.Connection) -> list[str]:
    known = {store.table for store in USER_DATA_STORES} | set(RETAINED_LEDGER_TABLES)
    found: list[str] = []
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        name = str(row[0])
        if name in known:
            continue
        columns = {col[1] for col in conn.execute(f"PRAGMA table_info({_quote(name)})")}
        if "user_id" in columns:
            found.append(name)
    return sorted(found)


def _unregistered_search_indexes(conn: sqlite3.Connection) -> list[str]:
    return sorted(
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND sql LIKE 'CREATE VIRTUAL TABLE%'"
        )
        if str(row[0]) not in REGISTERED_SEARCH_INDEXES
    )


def _count(
    conn: sqlite3.Connection, table: str, clause: str, params: tuple[Any, ...]
) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {_quote(table)} WHERE {clause}", params
        ).fetchone()[0]
    )


def _erase_step(
    conn: sqlite3.Connection,
    db_path: str,
    user_id: str,
    scope: DeletionScope,
    step: Step,
) -> int:
    """Erase everything *step* covers for *user_id*; return the rows removed."""
    removed = 0
    for store in _stores_for(step, scope):
        if store.optional and not _table_exists(conn, store.table):
            continue
        clause, params = _predicate(store, scope)
        removed += max(
            conn.execute(
                f"DELETE FROM {_quote(store.table)} WHERE {clause}", (user_id, *params)
            ).rowcount,
            0,
        )
    if step is Step.UNREGISTERED_STORES:
        for table in _unregistered_user_tables(conn):
            erased = max(
                conn.execute(
                    f"DELETE FROM {_quote(table)} WHERE user_id = ?", (user_id,)
                ).rowcount,
                0,
            )
            removed += erased
            if erased:
                logger.warning(
                    "Erased %d rows from unregistered table %s.", erased, table
                )
    elif step is Step.RETAINED_EXPORTS:
        removed += _erase_exports(db_path, user_id)
    return removed


def _remaining_step(
    conn: sqlite3.Connection,
    db_path: str,
    user_id: str,
    scope: DeletionScope,
    step: Step,
) -> int:
    """Count rows (or artefacts) *step* should have removed but did not."""
    remaining = 0
    for store in _stores_for(step, scope):
        if store.optional and not _table_exists(conn, store.table):
            continue
        clause, params = _predicate(store, scope)
        remaining += _count(conn, store.table, clause, (user_id, *params))
    if step is Step.UNREGISTERED_STORES:
        for table in _unregistered_user_tables(conn):
            remaining += _count(conn, table, "user_id = ?", (user_id,))
    elif step is Step.SEARCH_INDEXES:
        remaining += len(_unregistered_search_indexes(conn))
    elif step is Step.RETAINED_EXPORTS:
        target = export_directory(db_path, user_id)
        if target is not None and (target.exists() or target.is_symlink()):
            remaining += 1
    elif step is Step.QUEUED_JOBS:
        remaining += int(
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE id = ? AND id NOT IN ("
                "SELECT user_id FROM account_deletion_requests "
                "WHERE state IN ('pending', 'in_progress', 'failed'))",
                (user_id,),
            ).fetchone()[0]
        )
    elif step is Step.ACCOUNT_IDENTITY:
        remaining += int(
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)
            ).fetchone()[0]
        )
    return remaining


def _erase_exports(db_path: str, user_id: str) -> int:
    target = export_directory(db_path, user_id)
    if target is None or not (target.exists() or target.is_symlink()):
        return 0
    if target.is_symlink() or target.is_file():
        target.unlink()
        return 1
    files = sum(1 for path in target.rglob("*") if path.is_file())
    shutil.rmtree(target)
    return max(files, 1)


def _record_step(
    conn: sqlite3.Connection,
    request_id: str,
    step: Step,
    rows: int,
    remaining: int,
    when: datetime,
) -> None:
    conn.execute(
        "INSERT INTO account_deletion_steps "
        "(request_id, step, state, rows_affected, remaining_after, completed_at) "
        "VALUES (?, ?, 'done', ?, ?, ?) "
        "ON CONFLICT(request_id, step) DO UPDATE SET state = 'done', "
        "rows_affected = rows_affected + excluded.rows_affected, "
        "remaining_after = excluded.remaining_after, completed_at = excluded.completed_at",
        (request_id, step.value, rows, remaining, _ts(when)),
    )


def _run_step(
    db_path: str,
    request_id: str,
    user_id: str,
    scope: DeletionScope,
    step: Step,
    started: datetime,
) -> None:
    with _transaction(db_path) as conn:
        removed = _erase_step(conn, db_path, user_id, scope, step)
        remaining = _remaining_step(conn, db_path, user_id, scope, step)
        if remaining:
            raise _ResidualData(step.value)
        _record_step(conn, request_id, step, removed, remaining, _clock(started))
        conn.execute(
            "UPDATE account_deletion_requests SET next_attempt_at = ? WHERE id = ?",
            (_ts(_clock(started) + LEASE), request_id),
        )
    _audit("step_done", request_id, scope, step=step.value, rows=removed)


def _finalise(
    db_path: str,
    request_id: str,
    user_id: str,
    scope: DeletionScope,
    started: datetime,
) -> bool:
    """Verify under the write lock, then close the account and write the tombstone.

    Returns ``False`` (writing nothing) when a late writer left rows behind, so the
    caller sweeps again; the account is never closed with data still attached.
    """
    moment = _clock(started)
    with _transaction(db_path) as conn:
        for step in PLANS[scope]:
            if step is Step.ACCOUNT_IDENTITY:
                continue
            if _remaining_step(conn, db_path, user_id, scope, step):
                return False
        if scope is DeletionScope.ACCOUNT:
            closed = max(
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount, 0
            )
            if _remaining_step(conn, db_path, user_id, scope, Step.ACCOUNT_IDENTITY):
                raise _ResidualData(Step.ACCOUNT_IDENTITY.value)
            conn.execute(
                "DELETE FROM account_deletion_steps WHERE request_id IN ("
                "SELECT id FROM account_deletion_requests WHERE user_id = ? AND id <> ?)",
                (user_id, request_id),
            )
            conn.execute(
                "DELETE FROM account_deletion_requests WHERE user_id = ? AND id <> ?",
                (user_id, request_id),
            )
            _record_step(conn, request_id, Step.ACCOUNT_IDENTITY, closed, 0, moment)
            conn.execute(
                "INSERT OR REPLACE INTO account_tombstones "
                "(user_id, request_id, deleted_at, retain_until, policy_version) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    user_id,
                    request_id,
                    _ts(moment),
                    _ts(moment + timedelta(days=tombstone_retention_days())),
                    POLICY_VERSION,
                ),
            )
        conn.execute(
            "UPDATE account_deletion_requests SET state = 'completed', completed_at = ?, "
            "next_attempt_at = ?, error_code = NULL WHERE id = ?",
            (_ts(moment), _ts(moment), request_id),
        )
    return True


def _claim(db_path: str, request_id: str, now: datetime) -> sqlite3.Row | None:
    """Atomically take a due request; ``None`` when it is not due or already taken."""
    with _transaction(db_path) as conn:
        claimed = conn.execute(
            "UPDATE account_deletion_requests SET state = 'in_progress', "
            "started_at = COALESCE(started_at, ?), attempts = attempts + 1, "
            "next_attempt_at = ?, error_code = NULL "
            "WHERE id = ? AND state IN ('pending', 'in_progress', 'failed') "
            "AND next_attempt_at <= ? AND attempts < ?",
            (_ts(now), _ts(now + LEASE), request_id, _ts(now), MAX_ATTEMPTS),
        ).rowcount
        if claimed != 1:
            return None
        return conn.execute(
            "SELECT id, user_id, scope FROM account_deletion_requests WHERE id = ?",
            (request_id,),
        ).fetchone()


def _mark_failed(db_path: str, request_id: str, code: str, now: datetime) -> None:
    with _transaction(db_path) as conn:
        conn.execute(
            "UPDATE account_deletion_requests SET state = 'failed', error_code = ?, "
            "next_attempt_at = ? WHERE id = ? AND state = 'in_progress'",
            (code, _ts(now + RETRY_BACKOFF), request_id),
        )


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, DeletionError):
        return exc.code
    if isinstance(exc, _ResidualData):
        return "residual_data"
    if isinstance(exc, sqlite3.Error):
        return "database_error"
    if isinstance(exc, OSError):
        return "filesystem_error"
    return "internal_error"


def execute_deletion(
    db_path: str, request_id: str, *, now: datetime | None = None
) -> DeletionStatus | None:
    """Run a due request to completion; a no-op if it is not due or already running.

    Safe to call from any process and to repeat: the claim is atomic, every step is
    idempotent, and a crashed run is resumed once its lease expires.
    """
    moment = now or _utcnow()
    claimed = _claim(db_path, request_id, moment)
    if claimed is None:
        return get_status(db_path, request_id, now=moment)
    user_id = str(claimed["user_id"])
    scope = DeletionScope(claimed["scope"])
    _audit("started", request_id, scope)
    try:
        with _reader(db_path) as conn:
            if scope is DeletionScope.ACCOUNT and not _is_eligible(
                _load_user(conn, user_id)
            ):
                raise DeletionError("not_eligible")
        for _ in range(_FINALISE_PASSES):
            for step in PLANS[scope]:
                if step is not Step.ACCOUNT_IDENTITY:
                    _run_step(db_path, request_id, user_id, scope, step, moment)
            if _finalise(db_path, request_id, user_id, scope, moment):
                _audit("completed", request_id, scope)
                break
        else:
            raise _ResidualData("late_writes")
    except Exception as exc:
        code = _error_code(exc)
        logger.exception("Account deletion %s failed with %s.", request_id, code)
        _mark_failed(db_path, request_id, code, _clock(moment))
    return get_status(db_path, request_id, now=moment)


# ---------------------------------------------------------------------------
# Scheduler tick, straggler sweep and tombstone retention
# ---------------------------------------------------------------------------


def _erase_account_data(conn: sqlite3.Connection, db_path: str, user_id: str) -> int:
    """Erase every store for *user_id* without touching the request state machine."""
    return sum(
        _erase_step(conn, db_path, user_id, DeletionScope.ACCOUNT, step)
        for step in PLANS[DeletionScope.ACCOUNT]
        if step not in (Step.ACCOUNT_IDENTITY, Step.QUEUED_JOBS)
    )


def sweep_recent_tombstones(
    db_path: str,
    *,
    now: datetime | None = None,
    window: timedelta = STRAGGLER_SWEEP_WINDOW,
) -> int:
    """Re-erase rows written for a just-closed account by an in-flight request or job.

    A chat stream or coaching run that began before the account closed can still
    write once; for ``window`` after closure every tick removes such stragglers.
    """
    moment = now or _utcnow()
    try:
        with _reader(db_path) as conn:
            recent = [
                str(row["user_id"])
                for row in conn.execute(
                    "SELECT user_id FROM account_tombstones WHERE deleted_at >= ?",
                    (_ts(moment - window),),
                )
            ]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    removed = 0
    for user_id in recent:
        with _transaction(db_path) as conn:
            removed += _erase_account_data(conn, db_path, user_id)
    if removed:
        logger.warning("Swept %d straggler rows for recently closed accounts.", removed)
    return removed


def prune_expired_tombstones(db_path: str, *, now: datetime | None = None) -> int:
    """Delete tombstones (and their receipts) whose retention period has ended."""
    moment = now or _utcnow()
    with _transaction(db_path) as conn:
        expired = [
            (str(row["user_id"]), str(row["request_id"]))
            for row in conn.execute(
                "SELECT user_id, request_id FROM account_tombstones WHERE retain_until <= ?",
                (_ts(moment),),
            )
        ]
        for user_id, request_id in expired:
            conn.execute(
                "DELETE FROM account_deletion_steps WHERE request_id = ?", (request_id,)
            )
            conn.execute(
                "DELETE FROM account_deletion_requests WHERE id = ?", (request_id,)
            )
            conn.execute("DELETE FROM account_tombstones WHERE user_id = ?", (user_id,))
    return len(expired)


def process_due_deletions(db_path: str, *, now: datetime | None = None) -> int:
    """Worker tick for the scheduler process; returns requests completed this tick."""
    moment = now or _utcnow()
    with _reader(db_path) as conn:
        due = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM account_deletion_requests "
                "WHERE state IN ('pending', 'in_progress', 'failed') "
                "AND next_attempt_at <= ? AND attempts < ? "
                "ORDER BY next_attempt_at LIMIT ?",
                (_ts(moment), MAX_ATTEMPTS, _BATCH_SIZE),
            )
        ]
    completed = 0
    for request_id in due:
        status = execute_deletion(db_path, request_id, now=moment)
        if status is not None and status["state"] == DeletionState.COMPLETED:
            completed += 1
    sweep_recent_tombstones(db_path, now=moment)
    prune_expired_tombstones(db_path, now=moment)
    return completed


def _audit(event: str, request_id: str, scope: DeletionScope, **fields: object) -> None:
    """Structured audit line. Carries the receipt id only - never an account id."""
    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info(
        "account_deletion event=%s request=%s scope=%s %s",
        event,
        request_id,
        scope.value,
        detail,
    )


# ---------------------------------------------------------------------------
# Operator tools: keep deletions effective across a backup restore
# ---------------------------------------------------------------------------


def list_tombstoned_ids(db_path: str) -> list[str]:
    """Opaque ids of every closed account (no personal data) for off-database safekeeping."""
    with _reader(db_path) as conn:
        return [
            str(row["user_id"])
            for row in conn.execute(
                "SELECT user_id FROM account_tombstones ORDER BY deleted_at"
            )
        ]


def reapply_tombstones(
    db_path: str, user_ids: Iterable[str], *, now: datetime | None = None
) -> int:
    """Re-erase accounts whose deletion a backup restore may have undone.

    A restored database predates deletions made after the backup was taken, so those
    accounts (and their data) reappear. Given the ids from :func:`list_tombstoned_ids`
    (taken from the pre-restore database), this erases each one again and writes a
    fresh tombstone so their sessions fail closed. Returns the accounts newly closed.
    """
    moment = now or _utcnow()
    applied = 0
    for user_id in dict.fromkeys(uid.strip() for uid in user_ids if uid.strip()):
        with _transaction(db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM account_tombstones WHERE user_id = ?", (user_id,)
            ).fetchone():
                continue
            user = _load_user(conn, user_id)
            if user is not None and not _is_eligible(user):
                raise DeletionError("not_eligible")
            # The tombstone goes in first, inside the same transaction, so a
            # deletion exemption on an append-only table (see the docs) applies.
            conn.execute(
                "INSERT INTO account_tombstones "
                "(user_id, request_id, deleted_at, retain_until, policy_version) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    user_id,
                    f"reapplied-{uuid4().hex}",
                    _ts(moment),
                    _ts(moment + timedelta(days=tombstone_retention_days())),
                    POLICY_VERSION,
                ),
            )
            _erase_account_data(conn, db_path, user_id)
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.execute(
                "DELETE FROM account_deletion_steps WHERE request_id IN ("
                "SELECT id FROM account_deletion_requests WHERE user_id = ?)",
                (user_id,),
            )
            conn.execute(
                "DELETE FROM account_deletion_requests WHERE user_id = ?", (user_id,)
            )
        applied += 1
        logger.info("account_deletion event=reapplied tombstones=%d", applied)
    return applied


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operator tools for the account deletion ledger."
    )
    parser.add_argument(
        "--database", default=os.environ.get("DATABASE_PATH", "workout_agent.db")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-tombstones", help="Print one closed account id per line.")
    reconcile = commands.add_parser(
        "reconcile",
        help="Re-erase the accounts listed in a file (after restoring a backup).",
    )
    reconcile.add_argument("ids_file", type=Path)
    args = parser.parse_args(argv)

    if args.command == "list-tombstones":
        for user_id in list_tombstoned_ids(args.database):
            print(user_id)
        return 0
    ids = args.ids_file.read_text(encoding="utf-8").splitlines()
    print(f"Re-applied {reapply_tombstones(args.database, ids)} deletion(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
