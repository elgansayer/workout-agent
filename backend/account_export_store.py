"""Tenant-scoped persistence for account export jobs and the account audit trail.

Every function that serves a request takes the authenticated owner and filters on
it; a job id alone never grants access. The maintenance functions
(``expire_due``, ``reap_stale``, ``prune_terminal``, ``live_export_ids``) are
cross-tenant by design but only move rows through lifecycle states or list opaque
ids; they never read export contents.

Lifecycle: ``queued`` -> ``running`` -> ``ready`` -> ``expired``, with ``failed``
reachable from ``queued``/``running`` and ``deleted`` reachable from any state.
All transitions are conditional updates so concurrent workers and web replicas
cannot resurrect or double-complete a job.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Reuse database.py's transactional connection helper (WAL, commit/rollback) so
# export persistence follows the same conventions as every other table.
from database import DEFAULT_DB_PATH, _connect

QUEUED = "queued"
RUNNING = "running"
READY = "ready"
FAILED = "failed"
EXPIRED = "expired"
DELETED = "deleted"

EXPORT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

RESOURCE_TYPE = "account_export"
EVENT_REQUESTED = "account_export.requested"
EVENT_COMPLETED = "account_export.completed"
EVENT_FAILED = "account_export.failed"
EVENT_DOWNLOADED = "account_export.downloaded"
EVENT_DELETED = "account_export.deleted"
EVENT_EXPIRED = "account_export.expired"

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"

_SELECT_JOB = (
    "SELECT id, user_id, status, schema_version, requested_at, started_at, "
    "heartbeat_at, completed_at, expires_at, size_bytes, checksum_sha256, "
    "record_counts, error_code FROM account_exports "
)


class ExportRateLimited(Exception):
    """Raised when an account has requested too many exports in the window."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("account export request limit reached")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class ExportJob:
    id: str
    user_id: str
    status: str
    schema_version: int
    requested_at: str
    started_at: str | None
    heartbeat_at: str
    completed_at: str | None
    expires_at: str | None
    size_bytes: int | None
    checksum_sha256: str | None
    record_counts: dict[str, int] = field(default_factory=dict)
    error_code: str | None = None


def utc_iso(moment: datetime) -> str:
    """Return the canonical second-precision UTC timestamp used by this module.

    A single fixed format keeps lexicographic SQL comparisons chronological.
    """

    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _record_counts(raw: Any) -> dict[str, int]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): count
        for key, count in value.items()
        if isinstance(count, int) and not isinstance(count, bool)
    }


def _to_job(row: tuple[Any, ...]) -> ExportJob:
    return ExportJob(
        id=str(row[0]),
        user_id=str(row[1]),
        status=str(row[2]),
        schema_version=int(row[3]),
        requested_at=str(row[4]),
        started_at=None if row[5] is None else str(row[5]),
        heartbeat_at=str(row[6]),
        completed_at=None if row[7] is None else str(row[7]),
        expires_at=None if row[8] is None else str(row[8]),
        size_bytes=None if row[9] is None else int(row[9]),
        checksum_sha256=None if row[10] is None else str(row[10]),
        record_counts=_record_counts(row[11]),
        error_code=None if row[12] is None else str(row[12]),
    )


def _insert_audit(
    conn: sqlite3.Connection,
    user_id: str,
    event_type: str,
    outcome: str,
    export_id: str,
    now: datetime,
) -> None:
    conn.execute(
        "INSERT INTO account_audit_events "
        "(user_id, event_type, outcome, resource_type, resource_id, occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, event_type, outcome, RESOURCE_TYPE, export_id, utc_iso(now)),
    )


def _fetch_in_flight(conn: sqlite3.Connection, user_id: str) -> ExportJob | None:
    row = conn.execute(
        _SELECT_JOB + "WHERE user_id = ? AND status IN ('queued', 'running')",
        (user_id,),
    ).fetchone()
    return _to_job(row) if row else None


def create_job(
    user_id: str,
    export_id: str,
    *,
    schema_version: int,
    now: datetime,
    max_requests: int,
    window: timedelta,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[ExportJob, bool]:
    """Queue an export for *user_id*, or return the one already in flight.

    Returns ``(job, created)``. The partial unique index on in-flight jobs makes
    the one-at-a-time rule atomic even when two replicas race.
    """

    if not user_id:
        raise ValueError("an account export requires an authenticated owner")
    if not EXPORT_ID_PATTERN.fullmatch(export_id):
        raise ValueError("export ids must be 32 lowercase hexadecimal characters")

    now_iso = utc_iso(now)
    cutoff = utc_iso(now - window)
    with _connect(db_path) as conn:
        existing = _fetch_in_flight(conn, user_id)
        if existing is not None:
            return existing, False

        recent_count, oldest = conn.execute(
            "SELECT COUNT(*), MIN(requested_at) FROM account_exports "
            "WHERE user_id = ? AND requested_at >= ?",
            (user_id, cutoff),
        ).fetchone()
        if recent_count >= max_requests:
            oldest_at = datetime.fromisoformat(oldest) if oldest else now
            retry_after = int((oldest_at + window - now).total_seconds())
            raise ExportRateLimited(max(retry_after, 1))

        try:
            conn.execute(
                "INSERT INTO account_exports "
                "(id, user_id, status, schema_version, requested_at, heartbeat_at) "
                "VALUES (?, ?, 'queued', ?, ?, ?)",
                (export_id, user_id, schema_version, now_iso, now_iso),
            )
        except sqlite3.IntegrityError:
            raced = _fetch_in_flight(conn, user_id)
            if raced is not None:
                return raced, False
            raise
        _insert_audit(conn, user_id, EVENT_REQUESTED, OUTCOME_SUCCESS, export_id, now)
        row = conn.execute(
            _SELECT_JOB + "WHERE id = ? AND user_id = ?", (export_id, user_id)
        ).fetchone()
    return _to_job(row), True


def get_job(
    user_id: str, export_id: str, *, db_path: str = DEFAULT_DB_PATH
) -> ExportJob | None:
    """Return the owner's job, or ``None`` for unknown or foreign ids."""

    if not user_id or not EXPORT_ID_PATTERN.fullmatch(export_id):
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            _SELECT_JOB + "WHERE id = ? AND user_id = ?", (export_id, user_id)
        ).fetchone()
    return _to_job(row) if row else None


def list_jobs(
    user_id: str, *, limit: int = 20, db_path: str = DEFAULT_DB_PATH
) -> list[ExportJob]:
    """Return the owner's non-deleted jobs, newest first."""

    if not user_id:
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            _SELECT_JOB
            + "WHERE user_id = ? AND status != 'deleted' "
            "ORDER BY requested_at DESC, rowid DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [_to_job(row) for row in rows]


def claim_job(
    export_id: str, *, now: datetime, db_path: str = DEFAULT_DB_PATH
) -> ExportJob | None:
    """Move a queued job to running. The owner comes from the stored row."""

    if not EXPORT_ID_PATTERN.fullmatch(export_id):
        return None
    now_iso = utc_iso(now)
    with _connect(db_path) as conn:
        claimed = conn.execute(
            "UPDATE account_exports SET status = 'running', started_at = ?, "
            "heartbeat_at = ? WHERE id = ? AND status = 'queued'",
            (now_iso, now_iso, export_id),
        ).rowcount
        if claimed != 1:
            return None
        row = conn.execute(_SELECT_JOB + "WHERE id = ?", (export_id,)).fetchone()
    return _to_job(row) if row else None


def touch_job(
    export_id: str, *, now: datetime, db_path: str = DEFAULT_DB_PATH
) -> bool:
    """Refresh the lease. ``False`` means the job was cancelled or reaped."""

    with _connect(db_path) as conn:
        return (
            conn.execute(
                "UPDATE account_exports SET heartbeat_at = ? "
                "WHERE id = ? AND status = 'running'",
                (utc_iso(now), export_id),
            ).rowcount
            == 1
        )


def complete_job(
    export_id: str,
    *,
    now: datetime,
    expires_at: datetime,
    size_bytes: int,
    checksum_sha256: str,
    record_counts: dict[str, int],
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Publish a running job as ready. ``False`` means it was cancelled/reaped."""

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id FROM account_exports WHERE id = ? AND status = 'running'",
            (export_id,),
        ).fetchone()
        if row is None:
            return False
        updated = conn.execute(
            "UPDATE account_exports SET status = 'ready', completed_at = ?, "
            "heartbeat_at = ?, expires_at = ?, size_bytes = ?, checksum_sha256 = ?, "
            "record_counts = ?, error_code = NULL "
            "WHERE id = ? AND status = 'running'",
            (
                utc_iso(now),
                utc_iso(now),
                utc_iso(expires_at),
                size_bytes,
                checksum_sha256,
                json.dumps(record_counts, sort_keys=True),
                export_id,
            ),
        ).rowcount
        if updated != 1:
            return False
        _insert_audit(conn, row[0], EVENT_COMPLETED, OUTCOME_SUCCESS, export_id, now)
    return True


def fail_job(
    export_id: str,
    *,
    now: datetime,
    error_code: str,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Mark an in-flight job failed with a stable, non-sensitive code."""

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id FROM account_exports "
            "WHERE id = ? AND status IN ('queued', 'running')",
            (export_id,),
        ).fetchone()
        if row is None:
            return False
        updated = conn.execute(
            "UPDATE account_exports SET status = 'failed', completed_at = ?, "
            "error_code = ?, expires_at = NULL "
            "WHERE id = ? AND status IN ('queued', 'running')",
            (utc_iso(now), error_code, export_id),
        ).rowcount
        if updated != 1:
            return False
        _insert_audit(conn, row[0], EVENT_FAILED, OUTCOME_FAILURE, export_id, now)
    return True


def delete_job(
    user_id: str,
    export_id: str,
    *,
    now: datetime,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Mark the owner's job deleted. ``False`` for unknown/foreign/deleted ids."""

    if not user_id or not EXPORT_ID_PATTERN.fullmatch(export_id):
        return False
    with _connect(db_path) as conn:
        updated = conn.execute(
            "UPDATE account_exports SET status = 'deleted', expires_at = NULL, "
            "completed_at = COALESCE(completed_at, ?) "
            "WHERE id = ? AND user_id = ? AND status != 'deleted'",
            (utc_iso(now), export_id, user_id),
        ).rowcount
        if updated != 1:
            return False
        _insert_audit(conn, user_id, EVENT_DELETED, OUTCOME_SUCCESS, export_id, now)
    return True


def record_download(
    user_id: str,
    export_id: str,
    *,
    now: datetime,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Append an audit event for a download the owner has started."""

    with _connect(db_path) as conn:
        _insert_audit(conn, user_id, EVENT_DOWNLOADED, OUTCOME_SUCCESS, export_id, now)


def expire_job(
    user_id: str,
    export_id: str,
    *,
    now: datetime,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Expire the owner's ready export whose artifact is gone or unusable."""

    with _connect(db_path) as conn:
        return _expire_ready(conn, user_id, export_id, now)


def _expire_ready(
    conn: sqlite3.Connection, user_id: str, export_id: str, now: datetime
) -> bool:
    updated = conn.execute(
        "UPDATE account_exports SET status = 'expired', expires_at = ? "
        "WHERE id = ? AND user_id = ? AND status = 'ready'",
        (utc_iso(now), export_id, user_id),
    ).rowcount
    if updated != 1:
        return False
    _insert_audit(conn, user_id, EVENT_EXPIRED, OUTCOME_SUCCESS, export_id, now)
    return True


def expire_due(
    now: datetime,
    *,
    user_id: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> list[ExportJob]:
    """Expire ready exports whose retention has passed.

    With *user_id* the sweep is limited to that owner (request-time hygiene);
    without it, every tenant is swept (scheduled maintenance). Returns the jobs
    that transitioned so the caller can remove their artifacts.
    """

    now_iso = utc_iso(now)
    with _connect(db_path) as conn:
        if user_id is None:
            rows = conn.execute(
                _SELECT_JOB
                + "WHERE status = 'ready' AND expires_at IS NOT NULL "
                "AND expires_at <= ?",
                (now_iso,),
            ).fetchall()
        else:
            rows = conn.execute(
                _SELECT_JOB
                + "WHERE user_id = ? AND status = 'ready' "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (user_id, now_iso),
            ).fetchall()
        expired: list[ExportJob] = []
        for row in rows:
            job = _to_job(row)
            if _expire_ready(conn, job.user_id, job.id, now):
                expired.append(job)
    return expired


def expire_oldest_ready(
    user_id: str,
    *,
    keep: int,
    now: datetime,
    db_path: str = DEFAULT_DB_PATH,
) -> list[ExportJob]:
    """Expire the owner's oldest ready exports beyond the newest *keep*."""

    with _connect(db_path) as conn:
        rows = conn.execute(
            _SELECT_JOB
            + "WHERE user_id = ? AND status = 'ready' "
            "ORDER BY requested_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (user_id, max(keep, 0)),
        ).fetchall()
        expired: list[ExportJob] = []
        for row in rows:
            job = _to_job(row)
            if _expire_ready(conn, job.user_id, job.id, now):
                expired.append(job)
    return expired


def reap_stale(
    now: datetime,
    *,
    running_lease: timedelta,
    queued_lease: timedelta,
    user_id: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> list[ExportJob]:
    """Fail jobs whose worker vanished (process restart, crash, lost queue)."""

    running_cutoff = utc_iso(now - running_lease)
    queued_cutoff = utc_iso(now - queued_lease)
    with _connect(db_path) as conn:
        if user_id is None:
            rows = conn.execute(
                _SELECT_JOB
                + "WHERE (status = 'running' AND heartbeat_at < ?) "
                "OR (status = 'queued' AND heartbeat_at < ?)",
                (running_cutoff, queued_cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                _SELECT_JOB
                + "WHERE user_id = ? AND ((status = 'running' AND heartbeat_at < ?) "
                "OR (status = 'queued' AND heartbeat_at < ?))",
                (user_id, running_cutoff, queued_cutoff),
            ).fetchall()
        reaped: list[ExportJob] = []
        for row in rows:
            job = _to_job(row)
            updated = conn.execute(
                "UPDATE account_exports SET status = 'failed', completed_at = ?, "
                "error_code = 'interrupted', expires_at = NULL "
                "WHERE id = ? AND status IN ('queued', 'running')",
                (utc_iso(now), job.id),
            ).rowcount
            if updated == 1:
                _insert_audit(
                    conn, job.user_id, EVENT_FAILED, OUTCOME_FAILURE, job.id, now
                )
                reaped.append(job)
    return reaped


def prune_terminal(
    cutoff: datetime, *, db_path: str = DEFAULT_DB_PATH
) -> int:
    """Delete finished job rows requested before *cutoff*; audit events remain."""

    with _connect(db_path) as conn:
        return conn.execute(
            "DELETE FROM account_exports "
            "WHERE status IN ('failed', 'expired', 'deleted') AND requested_at < ?",
            (utc_iso(cutoff),),
        ).rowcount


def live_export_ids(*, db_path: str = DEFAULT_DB_PATH) -> set[str]:
    """Return ids whose artifact may legitimately exist on disk."""

    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM account_exports "
            "WHERE status IN ('queued', 'running', 'ready')"
        ).fetchall()
    return {str(row[0]) for row in rows}
