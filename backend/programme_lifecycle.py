"""Transactional programme activation, week states and deterministic adaptation.

The active programme is an immutable revision.  Week states, planned exposures,
workout observations, adaptation decisions and durable user overrides are stored
alongside it so every decision can be explained without replaying prose or an
LLM conversation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import database
from dynamic_programme import (
    ProgrammePreviewRequest,
    build_programme_preview,
    compute_preview_token,
    serialise_hevy_source,
)
from hevy_reader import CompletedWorkout, HevyTrainingData
from programme_engine import (
    AdaptationContext,
    AdaptationDecision,
    DecisionType,
    PriorDecision,
    ProgrammeValidationError,
    RecoveryContext,
    compute_source_hash,
    decide,
    diff_routine_snapshots,
    parse_rep_range,
    programme_fatigue_decision,
    validate_programme_definition,
)

LIFECYCLE_ENGINE_VERSION = "hevy-programme-v1"
_CADENCES = {"rolling", "calendar"}
_OVERRIDE_TYPES = {
    DecisionType.HOLD.value,
    DecisionType.PROGRESS_LOAD.value,
    DecisionType.PROGRESS_REPS.value,
    DecisionType.REDUCE_LOAD.value,
    DecisionType.REDUCE_SETS.value,
    DecisionType.ALTER_REP_RANGE.value,
}

LIFECYCLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS programme_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id),
        programme_id INTEGER NOT NULL REFERENCES programmes(id),
        revision_number INTEGER NOT NULL,
        engine_version TEXT NOT NULL,
        rule_version INTEGER NOT NULL DEFAULT 1,
        preview_token TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        definition TEXT NOT NULL,
        supersedes_revision_id INTEGER REFERENCES programme_revisions(id),
        reason TEXT NOT NULL DEFAULT 'activation',
        created_at TEXT NOT NULL,
        UNIQUE(user_id, programme_id, revision_number)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_programme_revisions_user
    ON programme_revisions (user_id, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_programme_revisions_programme
    ON programme_revisions (programme_id, revision_number DESC)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_programme_revisions_immutable
    BEFORE UPDATE ON programme_revisions
    BEGIN
        SELECT RAISE(ABORT, 'programme revisions are immutable');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS programme_week_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id),
        revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
        week_number INTEGER NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        cadence TEXT NOT NULL,
        timezone TEXT NOT NULL DEFAULT 'UTC',
        rotation_offset INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'planned',
        overrides TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(user_id, revision_id, week_number)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_programme_week_states_user
    ON programme_week_states (user_id, revision_id, week_number)
    """,
    """
    CREATE TABLE IF NOT EXISTS programme_exposures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id),
        revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
        week_id INTEGER NOT NULL REFERENCES programme_week_states(id),
        week_number INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        rotation_position INTEGER NOT NULL,
        planned_date TEXT NOT NULL,
        routine_id TEXT NOT NULL,
        routine_source_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'planned',
        completed_workout_id TEXT,
        match_confidence TEXT,
        match_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(user_id, revision_id, week_number, slot)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_programme_exposures_due
    ON programme_exposures (user_id, revision_id, status, week_number, slot)
    """,
    """
    CREATE TABLE IF NOT EXISTS programme_workout_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id),
        revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
        workout_id TEXT NOT NULL,
        observed_at TEXT,
        match_confidence TEXT NOT NULL,
        routine_id TEXT,
        exposure_id INTEGER REFERENCES programme_exposures(id),
        status TEXT NOT NULL,
        partial INTEGER NOT NULL DEFAULT 0,
        skipped_exercises TEXT NOT NULL DEFAULT '[]',
        substitutions TEXT NOT NULL DEFAULT '[]',
        payload_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, revision_id, workout_id, payload_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_programme_observations_user
    ON programme_workout_observations (user_id, revision_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS programme_adaptation_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id),
        revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
        week_number INTEGER NOT NULL,
        routine_id TEXT,
        exercise_template_id TEXT NOT NULL,
        decision_key TEXT NOT NULL,
        decision_type TEXT NOT NULL,
        applied INTEGER NOT NULL DEFAULT 0,
        resolution TEXT NOT NULL DEFAULT 'auto',
        requires_confirmation INTEGER NOT NULL DEFAULT 0,
        programme_level INTEGER NOT NULL DEFAULT 0,
        confidence TEXT,
        match_confidence TEXT,
        baseline_confidence TEXT,
        recovery_freshness TEXT,
        rationale_codes TEXT NOT NULL DEFAULT '[]',
        observation_refs TEXT NOT NULL DEFAULT '{}',
        previous_prescription TEXT NOT NULL DEFAULT '{}',
        proposed_prescription TEXT NOT NULL DEFAULT '{}',
        accepted_prescription TEXT NOT NULL DEFAULT '{}',
        load_change_pct REAL NOT NULL DEFAULT 0,
        set_change INTEGER NOT NULL DEFAULT 0,
        target_met INTEGER,
        rule_version INTEGER NOT NULL,
        engine_version TEXT NOT NULL,
        previous_decision_id INTEGER,
        from_revision_id INTEGER,
        to_revision_id INTEGER,
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        UNIQUE(user_id, decision_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_programme_decisions_user
    ON programme_adaptation_decisions (user_id, revision_id, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_programme_decisions_exercise
    ON programme_adaptation_decisions
        (user_id, revision_id, exercise_template_id, id DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS programme_overrides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id),
        revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
        exercise_template_id TEXT NOT NULL,
        decision_type TEXT NOT NULL,
        prescription TEXT NOT NULL,
        start_week INTEGER NOT NULL,
        end_week INTEGER NOT NULL,
        reason TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_programme_overrides_active
    ON programme_overrides
        (user_id, revision_id, exercise_template_id, active, start_week, end_week)
    """,
    """
    CREATE TABLE IF NOT EXISTS programme_audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id),
        revision_id INTEGER REFERENCES programme_revisions(id),
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_programme_audit_user
    ON programme_audit_events (user_id, id DESC)
    """,
)


@dataclass(frozen=True, slots=True)
class ActivationResult:
    programme: dict[str, Any]
    revision: dict[str, Any]
    week: dict[str, Any]
    exposures: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str
    workout_id: str
    match_confidence: str
    routine_id: str | None
    exposure_id: int | None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    duplicate: bool = False


@contextlib.contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a tenant database connection returning named rows for this module."""
    with database._connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


def _now(value: datetime | None = None) -> datetime:
    return value or datetime.now(tz=timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _now(value).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ValueError, ZoneInfoNotFoundError):
        return ZoneInfo("UTC")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create lifecycle tables and enforce one active programme per tenant."""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE programmes
        SET active = 0
        WHERE active = 1
          AND id NOT IN (
              SELECT MAX(id) FROM programmes WHERE active = 1 GROUP BY user_id
          )
        """
    )
    for statement in LIFECYCLE_DDL:
        cursor.execute(statement)
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_programmes_one_active_per_user
        ON programmes (user_id) WHERE active = 1
        """
    )


def _row_dict(row: sqlite3.Row | tuple[Any, ...], columns: list[str]) -> dict[str, Any]:
    return {column: row[index] for index, column in enumerate(columns)}


_REVISION_COLUMNS = [
    "id",
    "user_id",
    "programme_id",
    "revision_number",
    "engine_version",
    "rule_version",
    "preview_token",
    "source_hash",
    "definition",
    "supersedes_revision_id",
    "reason",
    "created_at",
]


def _revision_from_row(row: Any) -> dict[str, Any]:
    data = _row_dict(row, _REVISION_COLUMNS)
    data["definition"] = _loads(data.get("definition"), {})
    return data


def get_revision(
    user_id: str,
    revision_id: int,
    db_path: str = database.DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Return one revision only when it belongs to the authenticated tenant."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM programme_revisions WHERE user_id = ? AND id = ?",
            (user_id, revision_id),
        ).fetchone()
    return _revision_from_row(row) if row else None


def get_active_revision(
    user_id: str,
    db_path: str = database.DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Return the revision attached to the single active programme."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT r.*
            FROM programme_revisions r
            JOIN programmes p
              ON p.id = r.programme_id AND p.user_id = r.user_id
            WHERE r.user_id = ? AND p.active = 1
            ORDER BY r.id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return _revision_from_row(row) if row else None


def list_revisions(
    user_id: str,
    db_path: str = database.DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM programme_revisions
            WHERE user_id = ?
            ORDER BY id DESC
            """,
            (user_id,),
        ).fetchall()
    return [_revision_from_row(row) for row in rows]


def list_decisions(
    user_id: str,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    revision_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = """
        SELECT * FROM programme_adaptation_decisions
        WHERE user_id = ?
    """
    params: list[Any] = [user_id]
    if revision_id is not None:
        query += " AND revision_id = ?"
        params.append(revision_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, tuple(params)).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in (
            "rationale_codes",
            "observation_refs",
            "previous_prescription",
            "proposed_prescription",
            "accepted_prescription",
        ):
            item[key] = _loads(item.get(key), [] if key == "rationale_codes" else {})
        item["applied"] = bool(item.get("applied"))
        item["requires_confirmation"] = bool(item.get("requires_confirmation"))
        item["programme_level"] = bool(item.get("programme_level"))
        result.append(item)
    return result


def _audit(
    conn: sqlite3.Connection,
    user_id: str,
    revision_id: int | None,
    event_type: str,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO programme_audit_events
            (user_id, revision_id, event_type, payload, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, revision_id, event_type, _dumps(payload), _iso(now)),
    )


def _session_count(spec: dict[str, Any], day_count: int) -> int:
    if spec.get("sessions_per_week") is not None:
        return max(1, min(7, int(spec["sessions_per_week"])))
    return max(1, min(7, day_count))


def _calendar_dates(
    week_start: date,
    sessions: int,
    weekdays: list[int] | None,
) -> list[date]:
    if weekdays:
        result = []
        for weekday in weekdays[:sessions]:
            offset = (int(weekday) - week_start.weekday()) % 7
            result.append(week_start + timedelta(days=offset))
        while len(result) < sessions:
            result.append(result[-1] + timedelta(days=7) if result else week_start)
        return result[:sessions]
    if sessions >= 7:
        return [week_start + timedelta(days=index) for index in range(sessions)]
    return [
        week_start + timedelta(days=round(index * 7 / sessions) % 7)
        for index in range(sessions)
    ]


def _week_start(spec: dict[str, Any], week_number: int) -> date:
    return date.fromisoformat(str(spec["start_date"])) + timedelta(
        weeks=max(1, week_number) - 1
    )


def _week_number_for_definition(
    definition: dict[str, Any],
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> int:
    spec = definition.get("programme_spec") or {}
    try:
        start = date.fromisoformat(str(spec["start_date"]))
        duration = max(1, int(spec.get("duration_weeks") or 1))
    except (KeyError, TypeError, ValueError):
        return 1
    local_day = _now(now).astimezone(_tz(timezone_name)).date()
    return max(1, min(duration, (local_day - start).days // 7 + 1))


def _build_week(
    definition: dict[str, Any],
    week_number: int,
    *,
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    spec = definition.get("programme_spec") or {}
    days = definition.get("days") or []
    start = _week_start(spec, week_number)
    weekdays = spec.get("weekdays")
    if isinstance(weekdays, str):
        weekdays = [int(item) for item in weekdays.split(",") if item.strip()]
    cadence = str(spec.get("cadence") or "rolling").strip().lower()
    if cadence not in _CADENCES:
        cadence = "rolling"
    sessions = _session_count(spec, len(days))
    rotation_offset = ((week_number - 1) * sessions) % max(1, len(days))
    if cadence == "calendar":
        dates = _calendar_dates(start, sessions, weekdays)
    else:
        dates = [
            start + timedelta(days=round(index * 7 / sessions) % 7)
            for index in range(sessions)
        ]
    exposures = []
    for slot, planned in enumerate(dates):
        rotation_position = (rotation_offset + slot) % max(1, len(days))
        day = days[rotation_position]
        exposures.append(
            {
                "slot": slot,
                "rotation_position": rotation_position,
                "planned_date": planned.isoformat(),
                "routine_id": str(day.get("routine_id")),
                "routine_source_hash": str(day.get("source_hash") or ""),
            }
        )
    return {
        "week_number": week_number,
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=6)).isoformat(),
        "cadence": cadence,
        "timezone": timezone_name,
        "rotation_offset": rotation_offset,
        "status": "planned",
        "overrides": {},
        "exposures": exposures,
    }


def _insert_week(
    conn: sqlite3.Connection,
    user_id: str,
    revision_id: int,
    week: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    timestamp = _iso(now)
    cursor = conn.execute(
        """
        INSERT INTO programme_week_states
            (user_id, revision_id, week_number, start_date, end_date, cadence,
             timezone, rotation_offset, status, overrides, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            revision_id,
            week["week_number"],
            week["start_date"],
            week["end_date"],
            week["cadence"],
            week["timezone"],
            week["rotation_offset"],
            week["status"],
            _dumps(week.get("overrides") or {}),
            timestamp,
            timestamp,
        ),
    )
    week_id = int(cursor.lastrowid or 0)
    exposures = []
    for exposure in week["exposures"]:
        cur = conn.execute(
            """
            INSERT INTO programme_exposures
                (user_id, revision_id, week_id, week_number, slot,
                 rotation_position, planned_date, routine_id, routine_source_hash,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
            """,
            (
                user_id,
                revision_id,
                week_id,
                week["week_number"],
                exposure["slot"],
                exposure["rotation_position"],
                exposure["planned_date"],
                exposure["routine_id"],
                exposure["routine_source_hash"],
                timestamp,
                timestamp,
            ),
        )
        exposures.append(
            {
                **exposure,
                "id": int(cur.lastrowid or 0),
                "week_number": week["week_number"],
                "status": "planned",
            }
        )
    return week_id, exposures


def _materialise(
    conn: sqlite3.Connection,
    user_id: str,
    revision_id: int,
    definition: dict[str, Any],
    week_number: int,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    existing = conn.execute(
        """
        SELECT * FROM programme_week_states
        WHERE user_id = ? AND revision_id = ? AND week_number = ?
        """,
        (user_id, revision_id, week_number),
    ).fetchone()
    if existing:
        week = dict(existing)
        week["overrides"] = _loads(week.get("overrides"), {})
        exposures = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM programme_exposures
                WHERE user_id = ? AND revision_id = ? AND week_number = ?
                ORDER BY slot
                """,
                (user_id, revision_id, week_number),
            ).fetchall()
        ]
        return week, exposures
    week = _build_week(definition, week_number, timezone_name=timezone_name)
    _insert_week(conn, user_id, revision_id, week, now=now)
    stored = conn.execute(
        """
        SELECT * FROM programme_week_states
        WHERE user_id = ? AND revision_id = ? AND week_number = ?
        """,
        (user_id, revision_id, week_number),
    ).fetchone()
    stored_week = dict(stored) if stored else week
    stored_week["overrides"] = _loads(stored_week.get("overrides"), {})
    exposures = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM programme_exposures
            WHERE user_id = ? AND revision_id = ? AND week_number = ?
            ORDER BY slot
            """,
            (user_id, revision_id, week_number),
        ).fetchall()
    ]
    return stored_week, exposures


def _timezone_for(user_id: str, db_path: str) -> str:
    user = database.get_user_by_id(user_id, db_path)
    return (user.get("timezone") if user else None) or "UTC"


def materialise_week(
    user_id: str,
    week_number: int,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Materialise (idempotently) a dated week from the active revision."""
    revision = get_active_revision(user_id, db_path)
    if not revision:
        return None
    timezone_name = _timezone_for(user_id, db_path)
    with _connect(db_path) as conn:
        week, exposures = _materialise(
            conn,
            user_id,
            int(revision["id"]),
            revision["definition"],
            max(1, int(week_number)),
            timezone_name,
            now=now,
        )
    return {**week, "revision_id": revision["id"], "exposures": exposures}


def current_week_number(
    user_id: str,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    today: date | None = None,
) -> int | None:
    revision = get_active_revision(user_id, db_path)
    if not revision:
        return None
    spec = revision["definition"].get("programme_spec") or {}
    try:
        start = date.fromisoformat(str(spec["start_date"]))
        duration = max(1, int(spec.get("duration_weeks") or 1))
    except (KeyError, TypeError, ValueError):
        return None
    timezone_name = _timezone_for(user_id, db_path)
    local_day = today or _now().astimezone(_tz(timezone_name)).date()
    week = (local_day - start).days // 7 + 1
    return max(0, min(duration + 1, week))


def ensure_current_week(
    user_id: str,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    today: date | None = None,
) -> dict[str, Any] | None:
    week_number = current_week_number(user_id, db_path, today=today)
    if not week_number or week_number < 1:
        return None
    return materialise_week(user_id, week_number, db_path)


def next_exposure(
    user_id: str,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    revision_id: int | None = None,
) -> dict[str, Any] | None:
    """Return the next planned/partial exposure, never skipping a missed one."""
    active = get_active_revision(user_id, db_path) if revision_id is None else None
    target_revision = int(revision_id or (active or {}).get("id") or 0)
    if not target_revision:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM programme_exposures
            WHERE user_id = ? AND revision_id = ?
              AND status IN ('planned', 'partial')
            ORDER BY week_number, slot
            LIMIT 1
            """,
            (user_id, target_revision),
        ).fetchone()
    return dict(row) if row else None


def _persist_revision(
    conn: sqlite3.Connection,
    user_id: str,
    preview: dict[str, Any],
    db_path: str,
    *,
    reason: str,
    supersedes_revision_id: int | None,
    programme_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    validate_programme_definition(preview)
    expected_token = compute_preview_token(
        preview["programme_spec"],
        preview["source_snapshots"],
        engine_version=str(preview["engine_version"]),
    )
    if preview.get("preview_token") != expected_token:
        raise ProgrammeValidationError(
            "Preview token does not match the programme specification and source hashes."
        )
    timestamp = _iso(now)
    template_key = f"hevy:{str(preview['preview_token'])[:20]}"
    if programme_id is not None:
        row = conn.execute(
            "SELECT id FROM programmes WHERE user_id = ? AND id = ?",
            (user_id, programme_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM programmes WHERE user_id = ? AND template_key = ?",
            (user_id, template_key),
        ).fetchone()
    if row:
        programme_id = int(row[0])
        conn.execute(
            """
            UPDATE programmes
            SET source = 'hevy', definition = ?, active = 1, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (_dumps(preview), timestamp, programme_id, user_id),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO programmes
                (user_id, source, template_key, definition, active,
                 created_at, updated_at)
            VALUES (?, 'hevy', ?, ?, 1, ?, ?)
            """,
            (user_id, template_key, _dumps(preview), timestamp, timestamp),
        )
        programme_id = int(cursor.lastrowid or 0)
    revision_row = conn.execute(
        """
        SELECT COALESCE(MAX(revision_number), 0)
        FROM programme_revisions
        WHERE user_id = ? AND programme_id = ?
        """,
        (user_id, programme_id),
    ).fetchone()
    revision_number = int(revision_row[0] if revision_row else 0) + 1
    cursor = conn.execute(
        """
        INSERT INTO programme_revisions
            (user_id, programme_id, revision_number, engine_version, rule_version,
             preview_token, source_hash, definition, supersedes_revision_id,
             reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            programme_id,
            revision_number,
            str(preview["engine_version"]),
            int(preview.get("rule_version") or 1),
            str(preview["preview_token"]),
            compute_source_hash(preview["source_snapshots"]),
            _dumps(preview),
            supersedes_revision_id,
            reason,
            timestamp,
        ),
    )
    revision_id = int(cursor.lastrowid or 0)
    return {
        "id": revision_id,
        "user_id": user_id,
        "programme_id": programme_id,
        "revision_number": revision_number,
        "engine_version": str(preview["engine_version"]),
        "rule_version": int(preview.get("rule_version") or 1),
        "preview_token": str(preview["preview_token"]),
        "source_hash": compute_source_hash(preview["source_snapshots"]),
        "definition": preview,
        "supersedes_revision_id": supersedes_revision_id,
        "reason": reason,
        "created_at": timestamp,
    }


def activate_from_preview(
    user_id: str,
    preview: dict[str, Any],
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
    expected_token: str | None = None,
) -> ActivationResult:
    """Atomically archive the old programme and activate an immutable revision."""
    if not database.get_user_by_id(user_id, db_path):
        raise ProgrammeValidationError("Unknown programme owner.")
    if expected_token is not None and preview.get("preview_token") != expected_token:
        raise ProgrammeValidationError(
            "The preview token is stale. Generate a fresh preview before activation."
        )
    validate_programme_definition(preview)
    timestamp = _iso(now)
    timezone_name = _timezone_for(user_id, db_path)
    with _connect(db_path) as conn:
        previous = conn.execute(
            """
            SELECT r.id, r.programme_id
            FROM programme_revisions r
            JOIN programmes p
              ON p.id = r.programme_id AND p.user_id = r.user_id
            WHERE r.user_id = ? AND p.active = 1
            ORDER BY r.id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE programmes
            SET active = 0, updated_at = ?
            WHERE user_id = ? AND active = 1
            """,
            (timestamp, user_id),
        )
        revision = _persist_revision(
            conn,
            user_id,
            preview,
            db_path,
            reason="activation",
            supersedes_revision_id=int(previous["id"]) if previous else None,
            programme_id=int(previous["programme_id"]) if previous else None,
            now=now,
        )
        week, exposures = _materialise(
            conn,
            user_id,
            int(revision["id"]),
            preview,
            1,
            timezone_name,
            now=now,
        )
        _audit(
            conn,
            user_id,
            int(revision["id"]),
            "programme_activated",
            {
                "revision_number": revision["revision_number"],
                "engine_version": revision["engine_version"],
                "preview_token": revision["preview_token"],
                "source_hash": revision["source_hash"],
            },
            now=now,
        )
        conn.execute(
            """
            INSERT INTO notifications
                (user_id, type, title, message, link, is_read, created_at)
            VALUES (?, 'programme', ?, ?, '/plan', 0, ?)
            """,
            (
                user_id,
                "Hevy programme activated",
                (
                    f"{preview.get('name') or 'Your programme'} is active for "
                    f"{preview.get('cycle_weeks')} weeks using "
                    f"{preview.get('total_days')} selected routines."
                ),
                timestamp,
            ),
        )
        programme_row = conn.execute(
            "SELECT * FROM programmes WHERE id = ? AND user_id = ?",
            (revision["programme_id"], user_id),
        ).fetchone()
    programme = dict(programme_row) if programme_row else {}
    programme["definition"] = _loads(programme.get("definition"), preview)
    programme["active"] = bool(programme.get("active"))
    return ActivationResult(
        programme=programme,
        revision=revision,
        week=week,
        exposures=exposures,
    )


def _normalise_workout(workout: dict[str, Any] | CompletedWorkout) -> dict[str, Any]:
    if isinstance(workout, CompletedWorkout):
        return {
            "id": workout.id,
            "title": workout.title,
            "start_time": workout.start_time,
            "end_time": workout.end_time,
            "routine_id": None,
            "exercises": [
                {
                    "exercise_template_id": item.template_id,
                    "title": item.title,
                    "sets": item.sets,
                    "total_sets": item.total_sets,
                    "top_weight_kg": item.top_weight_kg,
                    "top_reps": item.top_reps,
                }
                for item in workout.exercises
            ],
        }
    return dict(workout)


def _template_ids(exercises: list[dict[str, Any]]) -> set[str]:
    result = set()
    for exercise in exercises:
        template_id = (
            exercise.get("exercise_template_id")
            or exercise.get("template_id")
            or exercise.get("id")
        )
        if template_id:
            result.add(str(template_id))
    return result


def _match_workout(
    definition: dict[str, Any],
    workout: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, str]:
    days = definition.get("days") or []
    provider_routine = str(workout.get("routine_id") or "").strip()
    if provider_routine:
        for day in days:
            if str(day.get("routine_id")) == provider_routine:
                return "high", day, "provider_routine_id"
        return "unmatched", None, "provider_routine_not_in_active_programme"
    workout_templates = _template_ids(list(workout.get("exercises") or []))
    if not workout_templates:
        return "unmatched", None, "no_exercise_identity"
    scored: list[tuple[float, dict[str, Any]]] = []
    for day in days:
        day_templates = _template_ids(list(day.get("exercises") or []))
        if not day_templates:
            continue
        overlap = len(workout_templates & day_templates)
        union = len(workout_templates | day_templates)
        if not union:
            continue
        score = overlap / union
        if score >= 0.6:
            scored.append((score, day))
    if not scored:
        return "unmatched", None, "no_routine_signature_match"
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15:
        return "ambiguous", None, "multiple_routine_signature_candidates"
    return "probable", scored[0][1], "exercise_signature_match"


def _workout_exercise_map(workout: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for exercise in workout.get("exercises") or []:
        template_id = str(
            exercise.get("exercise_template_id")
            or exercise.get("template_id")
            or exercise.get("id")
            or ""
        )
        if template_id:
            result[template_id] = exercise
    return result


def _exercise_actuals(exercise: dict[str, Any] | None) -> tuple[int, int | None, float | None]:
    if not exercise:
        return 0, None, None
    sets = exercise.get("sets")
    completed = len(sets) if isinstance(sets, list) else int(exercise.get("total_sets") or 0)
    reps = exercise.get("top_reps")
    load = exercise.get("top_weight_kg")
    if isinstance(sets, list):
        parsed = []
        loads = []
        for item in sets:
            if not isinstance(item, dict):
                continue
            if item.get("reps") is not None:
                parsed.append(int(item["reps"]))
            if item.get("weight_kg") is not None:
                loads.append(float(item["weight_kg"]))
        if parsed:
            reps = max(parsed)
        if loads:
            load = max(loads)
    return completed, (int(reps) if reps is not None else None), (
        float(load) if load is not None else None
    )


def _prescription_for_week(
    exercise: dict[str, Any],
    week_number: int,
) -> dict[str, Any]:
    prescriptions = exercise.get("prescriptions") or []
    for item in prescriptions:
        if int(item.get("block_number") or 0) == int(week_number):
            return dict(item)
    block_number = 1
    for index, item in enumerate(prescriptions, start=1):
        if index == 1:
            block_number = int(item.get("block_number") or 1)
    for item in prescriptions:
        if int(item.get("block_number") or 0) == block_number:
            return dict(item)
    return prescriptions[0] if prescriptions else {"sets": 1, "rep_range": "1-1"}


def _block_number(definition: dict[str, Any], week_number: int) -> int:
    for index, block in enumerate(definition.get("blocks") or [], start=1):
        if int(block.get("start_week") or 1) <= week_number <= int(
            block.get("end_week") or 1
        ):
            return int(block.get("number") or index)
    return 1


def _source_load(source_snapshot: dict[str, Any], template_id: str) -> float | None:
    for exercise in source_snapshot.get("exercises") or []:
        if str(exercise.get("template_id")) != template_id:
            continue
        if exercise.get("target_weight_kg") is not None:
            return float(exercise["target_weight_kg"])
        for target in exercise.get("set_targets") or []:
            if isinstance(target, dict) and target.get("weight_kg") is not None:
                return float(target["weight_kg"])
    return None


def _active_override(
    conn: sqlite3.Connection,
    user_id: str,
    revision_id: int,
    exercise_template_id: str,
    week_number: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM programme_overrides
        WHERE user_id = ? AND revision_id = ? AND exercise_template_id = ?
          AND active = 1 AND start_week <= ? AND end_week >= ?
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, revision_id, exercise_template_id, week_number, week_number),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["prescription"] = _loads(item.get("prescription"), {})
    return item


def _decision_history(
    conn: sqlite3.Connection,
    user_id: str,
    revision_id: int,
    exercise_template_id: str,
    *,
    limit: int = 20,
) -> tuple[PriorDecision, ...]:
    rows = conn.execute(
        """
        SELECT decision_type, applied, target_met,
               accepted_prescription
        FROM programme_adaptation_decisions
        WHERE user_id = ? AND revision_id = ? AND exercise_template_id = ?
          AND programme_level = 0
        ORDER BY id DESC LIMIT ?
        """,
        (user_id, revision_id, exercise_template_id, limit),
    ).fetchall()
    history = []
    for row in reversed(rows):
        history.append(
            PriorDecision(
                decision_type=str(row["decision_type"]),
                applied=bool(row["applied"]),
                target_met=(
                    None if row["target_met"] is None else bool(row["target_met"])
                ),
                proposed_prescription=_loads(row["accepted_prescription"], {}),
            )
        )
    return tuple(history)


def _insert_decision(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    revision_id: int,
    week_number: int,
    routine_id: str | None,
    exercise_template_id: str,
    decision_key: str,
    decision: AdaptationDecision,
    match_confidence: str,
    baseline_confidence: str,
    recovery_freshness: str | None,
    observation_refs: dict[str, Any],
    engine_version: str,
    target_met: bool | None = None,
    previous_decision_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    applied = 1 if decision.applied else 0
    accepted = decision.proposed_prescription if decision.applied else decision.previous_prescription
    previous = dict(decision.previous_prescription)
    proposed = dict(decision.proposed_prescription)
    load_change = (
        abs(float(proposed.get("load_kg") or 0) - float(previous.get("load_kg") or 0))
        / float(previous.get("load_kg") or 1)
        if previous.get("load_kg")
        else 0.0
    )
    try:
        set_change = int(proposed.get("sets") or 0) - int(previous.get("sets") or 0)
    except (TypeError, ValueError):
        set_change = 0
    cursor = conn.execute(
        """
        INSERT INTO programme_adaptation_decisions
            (user_id, revision_id, week_number, routine_id, exercise_template_id,
             decision_key, decision_type, applied, resolution, requires_confirmation,
             programme_level, confidence, match_confidence, baseline_confidence,
             recovery_freshness, rationale_codes, observation_refs,
             previous_prescription, proposed_prescription, accepted_prescription,
             load_change_pct, set_change, target_met, rule_version, engine_version,
             previous_decision_id, from_revision_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, decision_key) DO NOTHING
        """,
        (
            user_id,
            revision_id,
            week_number,
            routine_id,
            exercise_template_id,
            decision_key,
            decision.decision_type.value,
            applied,
            "auto" if decision.applied else "pending",
            1 if decision.requires_confirmation else 0,
            1 if decision.programme_level else 0,
            decision.confidence,
            match_confidence,
            baseline_confidence,
            recovery_freshness,
            _dumps(list(decision.rationale_codes)),
            _dumps(observation_refs),
            _dumps(previous),
            _dumps(proposed),
            _dumps(accepted),
            load_change,
            set_change,
            None if target_met is None else (1 if target_met else 0),
            decision.rule_version,
            engine_version,
            previous_decision_id,
            revision_id,
            _iso(now),
        ),
    )
    row = conn.execute(
        "SELECT * FROM programme_adaptation_decisions WHERE user_id = ? AND decision_key = ?",
        (user_id, decision_key),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    for key in (
        "rationale_codes",
        "observation_refs",
        "previous_prescription",
        "proposed_prescription",
        "accepted_prescription",
    ):
        item[key] = _loads(item.get(key), [] if key == "rationale_codes" else {})
    item["applied"] = bool(item.get("applied"))
    item["requires_confirmation"] = bool(item.get("requires_confirmation"))
    item["programme_level"] = bool(item.get("programme_level"))
    if cursor.rowcount == 0:
        item["duplicate"] = True
    return item


def _update_week_overrides(
    conn: sqlite3.Connection,
    user_id: str,
    revision_id: int,
    week_number: int,
    exercise_template_id: str,
    prescription: dict[str, Any],
    decision_id: int,
) -> None:
    row = conn.execute(
        """
        SELECT id, overrides FROM programme_week_states
        WHERE user_id = ? AND revision_id = ? AND week_number = ?
        """,
        (user_id, revision_id, week_number),
    ).fetchone()
    if not row:
        return
    overrides = _loads(row[1], {})
    overrides[exercise_template_id] = {
        "prescription": {
            key: value
            for key, value in prescription.items()
            if not str(key).startswith("_")
        },
        "decision_id": decision_id,
    }
    conn.execute(
        """
        UPDATE programme_week_states
        SET overrides = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (_dumps(overrides), _iso(), row[0], user_id),
    )


def ingest_workout_event(
    user_id: str,
    workout: dict[str, Any] | CompletedWorkout,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
    recovery: RecoveryContext | None = None,
    feedback: dict[str, dict[str, Any]] | None = None,
) -> IngestResult:
    """Match one completed workout and persist idempotent adaptation decisions."""
    revision = get_active_revision(user_id, db_path)
    if not revision:
        raise ValueError("No active programme is available for workout ingestion.")
    normalised = _normalise_workout(workout)
    workout_id = str(normalised.get("id") or "").strip()
    if not workout_id:
        raise ValueError("A completed workout requires a provider workout ID.")
    revision_id = int(revision["id"])
    definition = revision["definition"]
    payload_hash = hashlib.sha256(_dumps(normalised).encode("utf-8")).hexdigest()
    with _connect(db_path) as conn:
        existing = conn.execute(
            """
            SELECT * FROM programme_workout_observations
            WHERE user_id = ? AND revision_id = ? AND workout_id = ?
              AND payload_hash = ?
            """,
            (user_id, revision_id, workout_id, payload_hash),
        ).fetchone()
        if existing:
            decisions = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM programme_adaptation_decisions
                    WHERE user_id = ? AND observation_refs LIKE ?
                    ORDER BY id
                    """,
                    (user_id, f'%"{workout_id}"%'),
                ).fetchall()
            ]
            return IngestResult(
                status=str(existing["status"]),
                workout_id=workout_id,
                match_confidence=str(existing["match_confidence"]),
                routine_id=existing["routine_id"],
                exposure_id=existing["exposure_id"],
                decisions=decisions,
                duplicate=True,
            )

        match_confidence, day, match_reason = _match_workout(definition, normalised)
        week_number = current_week_number(user_id, db_path) or 1
        if match_confidence in {"ambiguous", "unmatched"}:
            conn.execute(
                """
                INSERT INTO programme_workout_observations
                    (user_id, revision_id, workout_id, observed_at,
                     match_confidence, routine_id, exposure_id, status, partial,
                     skipped_exercises, substitutions, payload_hash, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 0, '[]', '[]', ?, ?)
                """,
                (
                    user_id,
                    revision_id,
                    workout_id,
                    normalised.get("end_time") or normalised.get("start_time"),
                    match_confidence,
                    "ambiguous" if match_confidence == "ambiguous" else "unmatched",
                    payload_hash,
                    _iso(now),
                ),
            )
            decision = decide(
                AdaptationContext(
                    exercise_template_id="",
                    match_confidence=match_confidence,
                )
            )
            _insert_decision(
                conn,
                user_id=user_id,
                revision_id=revision_id,
                week_number=week_number,
                routine_id=None,
                exercise_template_id="",
                decision_key=(
                    f"r{revision_id}:w{workout_id}:{payload_hash[:12]}:match"
                ),
                decision=decision,
                match_confidence=match_confidence,
                baseline_confidence="sufficient",
                recovery_freshness=None,
                observation_refs={"workout_id": workout_id, "reason": match_reason},
                engine_version=str(revision["engine_version"]),
                now=now,
            )
            _audit(
                conn,
                user_id,
                revision_id,
                "workout_match_requires_confirmation",
                {"workout_id": workout_id, "reason": match_reason},
                now=now,
            )
            return IngestResult(
                status="ambiguous" if match_confidence == "ambiguous" else "unmatched",
                workout_id=workout_id,
                match_confidence=match_confidence,
                routine_id=None,
                exposure_id=None,
                decisions=[],
            )

        assert day is not None
        routine_id = str(day.get("routine_id"))
        exposure_row = conn.execute(
            """
            SELECT * FROM programme_exposures
            WHERE user_id = ? AND revision_id = ? AND routine_id = ?
              AND status IN ('planned', 'partial')
            ORDER BY week_number, slot LIMIT 1
            """,
            (user_id, revision_id, routine_id),
        ).fetchone()
        if not exposure_row:
            exposure_row = conn.execute(
                """
                SELECT * FROM programme_exposures
                WHERE user_id = ? AND revision_id = ?
                  AND completed_workout_id = ?
                ORDER BY week_number DESC, slot DESC LIMIT 1
                """,
                (user_id, revision_id, workout_id),
            ).fetchone()
        if not exposure_row:
            conn.execute(
                """
                INSERT INTO programme_workout_observations
                    (user_id, revision_id, workout_id, observed_at,
                     match_confidence, routine_id, exposure_id, status, partial,
                     skipped_exercises, substitutions, payload_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 'unmatched', 0, '[]', '[]', ?, ?)
                """,
                (
                    user_id,
                    revision_id,
                    workout_id,
                    normalised.get("end_time") or normalised.get("start_time"),
                    match_confidence,
                    routine_id,
                    payload_hash,
                    _iso(now),
                ),
            )
            _audit(
                conn,
                user_id,
                revision_id,
                "workout_without_planned_exposure",
                {"workout_id": workout_id, "routine_id": routine_id},
                now=now,
            )
            return IngestResult(
                status="unmatched",
                workout_id=workout_id,
                match_confidence=match_confidence,
                routine_id=routine_id,
                exposure_id=None,
            )

        exposure = dict(exposure_row)
        week_number = int(exposure["week_number"])
        actuals = _workout_exercise_map(normalised)
        planned_exercises = list(day.get("exercises") or [])
        planned_ids = [_template_id(item) for item in planned_exercises]
        present_ids = set(actuals)
        skipped = [item for item in planned_ids if item and item not in present_ids]
        substituted = [item for item in present_ids if item not in set(planned_ids)]
        partial = bool(skipped) or bool(substituted)
        for template_id in planned_ids:
            if not template_id or template_id not in actuals:
                continue
            completed_sets, completed_reps, completed_load = _exercise_actuals(
                actuals[template_id]
            )
            current_prescription = _prescription_for_week(
                next(
                    item
                    for item in planned_exercises
                    if _template_id(item) == template_id
                ),
                _block_number(definition, week_number),
            )
            if completed_sets < int(current_prescription.get("sets") or 1):
                partial = True
        status = "partial" if partial else "completed"
        conn.execute(
            """
            UPDATE programme_exposures
            SET status = ?, completed_workout_id = ?, match_confidence = ?,
                match_reason = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                status,
                workout_id,
                match_confidence,
                match_reason,
                _iso(now),
                exposure["id"],
                user_id,
            ),
        )
        cursor = conn.execute(
            """
            INSERT INTO programme_workout_observations
                (user_id, revision_id, workout_id, observed_at,
                 match_confidence, routine_id, exposure_id, status, partial,
                 skipped_exercises, substitutions, payload_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                revision_id,
                workout_id,
                normalised.get("end_time") or normalised.get("start_time"),
                match_confidence,
                routine_id,
                exposure["id"],
                status,
                1 if partial else 0,
                _dumps(skipped),
                _dumps(substituted),
                payload_hash,
                _iso(now),
            ),
        )
        observation_id = int(cursor.lastrowid or 0)

        source_snapshot: dict[str, Any] = next(
            (
                item
                for item in definition.get("source_snapshots") or []
                if str(item.get("id")) == routine_id
            ),
            {},
        )
        analysis = definition.get("analysis") or {}
        baseline_confidence = (
            "sparse" if str(analysis.get("confidence") or "").lower() == "low" else "sufficient"
        )
        prior_observations = conn.execute(
            """
            SELECT COUNT(*) FROM programme_workout_observations
            WHERE user_id = ? AND revision_id = ? AND routine_id = ?
              AND id != ?
            """,
            (user_id, revision_id, routine_id, observation_id),
        ).fetchone()
        if int(prior_observations[0] if prior_observations else 0) < 3:
            baseline_confidence = "sparse"
        feedback = feedback or {}
        workout_decisions: list[dict[str, Any]] = []
        for planned in planned_exercises:
            template_id = _template_id(planned)
            if not template_id:
                continue
            exercise_feedback = feedback.get(template_id, {})
            actual = actuals.get(template_id)
            completed_sets, completed_reps, completed_load = _exercise_actuals(actual)
            prescription = _prescription_for_week(
                planned, _block_number(definition, week_number)
            )
            rep_range = parse_rep_range(prescription.get("rep_range")) or (8, 12)
            prescribed_sets = int(prescription.get("sets") or planned.get("sets") or 1)
            rpe = exercise_feedback.get("rpe")
            target_met = (
                completed_sets >= prescribed_sets
                and bool(
                    exercise_feedback.get(
                        "target_rir_met",
                        not (rpe is not None and float(rpe) >= 9.5),
                    )
                )
                and (
                    completed_reps is None
                    or completed_reps >= int(rep_range[0])
                )
            )
            active_override = _active_override(
                conn, user_id, revision_id, template_id, week_number
            )
            if active_override:
                override_prescription = active_override["prescription"]
                decision = AdaptationDecision(
                    decision_type=DecisionType.USER_OVERRIDE,
                    previous_prescription=dict(override_prescription),
                    proposed_prescription=dict(override_prescription),
                    rationale_codes=("durable_user_override",),
                    confidence="high",
                    applied=True,
                    requires_confirmation=False,
                    previous_decision=active_override["decision_type"],
                )
            else:
                prescribed_load = prescription.get("load_kg")
                if prescribed_load is None:
                    prescribed_load = _source_load(source_snapshot, template_id)
                context = AdaptationContext(
                    exercise_template_id=template_id,
                    exercise_type=str(planned.get("exercise_type") or "weight_reps"),
                    role=str(planned.get("role") or ""),
                    match_confidence=match_confidence,
                    baseline_confidence=baseline_confidence,
                    hard_constraint_block=bool(
                        exercise_feedback.get("hard_constraint")
                    ),
                    user_override=exercise_feedback.get("override"),
                    pain_flag=bool(exercise_feedback.get("pain")),
                    technique_flag=bool(exercise_feedback.get("technique_issue")),
                    substitution_available=bool(
                        exercise_feedback.get("substitution_available")
                    ),
                    prescribed_sets=prescribed_sets,
                    prescribed_rep_low=rep_range[0],
                    prescribed_rep_high=rep_range[1],
                    prescribed_load_kg=(
                        float(prescribed_load) if prescribed_load is not None else None
                    ),
                    target_rir=str(prescription.get("target_rir") or ""),
                    completed_sets=completed_sets,
                    completed_reps=completed_reps,
                    completed_load_kg=completed_load,
                    target_rir_met=bool(target_met),
                    performance_trend=exercise_feedback.get("performance_trend"),
                    logged_rpe=float(rpe) if rpe is not None else None,
                    adherence_ratio=(
                        completed_sets / prescribed_sets
                        if prescribed_sets
                        else 1.0
                    ),
                    recovery=recovery,
                    aggressiveness=str(
                        (definition.get("programme_spec") or {}).get(
                            "adaptation_aggressiveness"
                        )
                        or "balanced"
                    ),
                    block_min_sets=1,
                    block_max_sets=max(prescribed_sets + 1, 2),
                    block_rep_low=max(1, rep_range[0]),
                    block_rep_high=rep_range[1] + 2,
                    week_change=_week_change(conn, user_id, revision_id, week_number),
                    history=_decision_history(
                        conn, user_id, revision_id, template_id
                    ),
                )
                decision = decide(context)
            decision_row = _insert_decision(
                conn,
                user_id=user_id,
                revision_id=revision_id,
                week_number=week_number,
                routine_id=routine_id,
                exercise_template_id=template_id,
                decision_key=(
                    f"r{revision_id}:w{workout_id}:{payload_hash[:12]}:e{template_id}"
                ),
                decision=decision,
                match_confidence=match_confidence,
                baseline_confidence=baseline_confidence,
                recovery_freshness=(
                    "fresh"
                    if recovery and recovery.fresh
                    else ("stale" if recovery else "missing")
                ),
                observation_refs={
                    "workout_id": workout_id,
                    "observation_id": observation_id,
                    "exposure_id": exposure["id"],
                    "planned_sets": prescribed_sets,
                    "completed_sets": completed_sets,
                },
                engine_version=str(revision["engine_version"]),
                target_met=target_met,
                now=now,
            )
            if decision_row:
                workout_decisions.append(decision_row)
                if decision.applied:
                    _update_week_overrides(
                        conn,
                        user_id,
                        revision_id,
                        week_number,
                        template_id,
                        decision.proposed_prescription,
                        int(decision_row["id"]),
                    )
        fatigue = [
            item
            for item in workout_decisions
            if item.get("decision_type") == DecisionType.FATIGUE_MANAGEMENT.value
        ]
        if len(fatigue) >= 2:
            programme_decision = programme_fatigue_decision(
                [str(item.get("decision_type")) for item in fatigue]
            )
            _insert_decision(
                conn,
                user_id=user_id,
                revision_id=revision_id,
                week_number=week_number,
                routine_id=routine_id,
                exercise_template_id="",
                decision_key=(
                    f"r{revision_id}:w{workout_id}:{payload_hash[:12]}:programme-fatigue"
                ),
                decision=programme_decision,
                match_confidence=match_confidence,
                baseline_confidence=baseline_confidence,
                recovery_freshness=(
                    "fresh" if recovery and recovery.fresh else "missing"
                ),
                observation_refs={"workout_id": workout_id, "observation_id": observation_id},
                engine_version=str(revision["engine_version"]),
                now=now,
            )
        _audit(
            conn,
            user_id,
            revision_id,
            "workout_ingested",
            {
                "workout_id": workout_id,
                "observation_id": observation_id,
                "status": status,
                "match_confidence": match_confidence,
                "decision_count": len(workout_decisions),
            },
            now=now,
        )
    return IngestResult(
        status=status,
        workout_id=workout_id,
        match_confidence=match_confidence,
        routine_id=routine_id,
        exposure_id=int(exposure["id"]),
        decisions=workout_decisions,
    )


def delete_workout_event(
    user_id: str,
    workout_id: str,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> bool:
    """Handle a provider workout deletion without deleting decision history."""
    revision = get_active_revision(user_id, db_path)
    revision_id = int(revision["id"]) if revision else None
    with _connect(db_path) as conn:
        observations = conn.execute(
            """
            SELECT id FROM programme_workout_observations
            WHERE user_id = ? AND workout_id = ?
            """,
            (user_id, workout_id),
        ).fetchall()
        if not observations:
            return False
        conn.execute(
            """
            UPDATE programme_workout_observations
            SET status = 'deleted'
            WHERE user_id = ? AND workout_id = ?
            """,
            (user_id, workout_id),
        )
        conn.execute(
            """
            UPDATE programme_exposures
            SET status = 'planned', completed_workout_id = NULL,
                match_confidence = NULL, match_reason = NULL, updated_at = ?
            WHERE user_id = ? AND completed_workout_id = ?
            """,
            (_iso(now), user_id, workout_id),
        )
        _audit(
            conn,
            user_id,
            revision_id,
            "workout_deleted",
            {"workout_id": workout_id},
            now=now,
        )
    return True


def _template_id(exercise: dict[str, Any]) -> str:
    return str(exercise.get("template_id") or "")


def _week_change(
    conn: sqlite3.Connection,
    user_id: str,
    revision_id: int,
    week_number: int,
) -> dict[str, float]:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(load_change_pct), 0), COALESCE(SUM(set_change), 0)
        FROM programme_adaptation_decisions
        WHERE user_id = ? AND revision_id = ? AND week_number = ?
          AND applied = 1 AND programme_level = 0
        """,
        (user_id, revision_id, week_number),
    ).fetchone()
    return {
        "load_pct": float(row[0] or 0) if row else 0.0,
        "set_delta": float(row[1] or 0) if row else 0.0,
    }


def record_override(
    user_id: str,
    revision_id: int,
    exercise_template_id: str,
    decision_type: str,
    prescription: dict[str, Any],
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    start_week: int = 1,
    end_week: int | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a durable, scoped user override without mutating provider data."""
    revision = get_revision(user_id, revision_id, db_path)
    if not revision:
        raise ValueError("Revision not found for this user.")
    decision_type = str(decision_type or "").strip().lower()
    if decision_type not in _OVERRIDE_TYPES:
        raise ValueError("Unsupported override decision type.")
    try:
        sets = int(str(prescription.get("sets")))
    except (TypeError, ValueError) as exc:
        raise ValueError("Override prescription requires a positive set count.") from exc
    if sets < 1:
        raise ValueError("Override prescription requires a positive set count.")
    if not str(prescription.get("rep_range") or "").strip():
        raise ValueError("Override prescription requires a rep range or work target.")
    duration = int((revision["definition"].get("programme_spec") or {}).get(
        "duration_weeks"
    ) or 1)
    start = max(1, int(start_week))
    end = min(duration, int(end_week or duration))
    if end < start:
        raise ValueError("Override end week must not precede its start week.")
    timestamp = _iso(now)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO programme_overrides
                (user_id, revision_id, exercise_template_id, decision_type,
                 prescription, start_week, end_week, reason, active,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                user_id,
                revision_id,
                exercise_template_id,
                decision_type,
                _dumps(
                    {
                        key: value
                        for key, value in prescription.items()
                        if not str(key).startswith("_")
                    }
                ),
                start,
                end,
                reason,
                timestamp,
                timestamp,
            ),
        )
        override_id = int(cursor.lastrowid or 0)
        _audit(
            conn,
            user_id,
            revision_id,
            "programme_override_recorded",
            {
                "override_id": override_id,
                "exercise_template_id": exercise_template_id,
                "decision_type": decision_type,
                "start_week": start,
                "end_week": end,
            },
            now=now,
        )
    return {
        "id": override_id,
        "user_id": user_id,
        "revision_id": revision_id,
        "exercise_template_id": exercise_template_id,
        "decision_type": decision_type,
        "prescription": prescription,
        "start_week": start,
        "end_week": end,
        "reason": reason,
        "active": True,
    }


def list_overrides(
    user_id: str,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    revision_id: int | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM programme_overrides WHERE user_id = ?"
    params: list[Any] = [user_id]
    if revision_id is not None:
        query += " AND revision_id = ?"
        params.append(revision_id)
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY id DESC"
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, tuple(params)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["prescription"] = _loads(item.get("prescription"), {})
        item["active"] = bool(item.get("active"))
        result.append(item)
    return result


def revert_override(
    user_id: str,
    override_id: int,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT revision_id FROM programme_overrides WHERE id = ? AND user_id = ?",
            (override_id, user_id),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            """
            UPDATE programme_overrides
            SET active = 0, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (_iso(now), override_id, user_id),
        )
        _audit(
            conn,
            user_id,
            int(row[0]),
            "programme_override_reverted",
            {"override_id": override_id},
            now=now,
        )
    return True


def resolve_decision(
    user_id: str,
    decision_id: int,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    confirmed: bool,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Confirm or revert a pending decision while preserving its audit row."""
    timezone_name = _timezone_for(user_id, db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM programme_adaptation_decisions
            WHERE id = ? AND user_id = ?
            """,
            (decision_id, user_id),
        ).fetchone()
        if not row:
            return None
        existing = dict(row)
        if not existing.get("requires_confirmation"):
            return existing
        resolution = "user_confirmed" if confirmed else "reverted"
        accepted = existing.get("proposed_prescription") if confirmed else existing.get(
            "previous_prescription"
        )
        if (
            confirmed
            and existing.get("decision_type")
            == DecisionType.PROGRAMME_FATIGUE_REVISION.value
        ):
            revision = conn.execute(
                "SELECT * FROM programme_revisions WHERE user_id = ? AND id = ?",
                (user_id, existing["revision_id"]),
            ).fetchone()
            if revision:
                revision_data = _revision_from_row(revision)
                definition = deepcopy(revision_data["definition"])
                for day in definition.get("days") or []:
                    for exercise in day.get("exercises") or []:
                        for item in exercise.get("prescriptions") or []:
                            item["sets"] = max(1, int(item.get("sets") or 1) - 1)
                conn.execute(
                    """
                    UPDATE programmes
                    SET active = 0, updated_at = ?
                    WHERE user_id = ? AND active = 1
                    """,
                    (_iso(now), user_id),
                )
                merged = _persist_revision(
                    conn,
                    user_id,
                    definition,
                    db_path,
                    reason="confirmed_fatigue_revision",
                    supersedes_revision_id=int(revision_data["id"]),
                    programme_id=int(revision_data["programme_id"]),
                    now=now,
                )
                _materialise(
                    conn,
                    user_id,
                    int(merged["id"]),
                    definition,
                    _week_number_for_definition(
                        definition, timezone_name, now=now
                    ),
                    timezone_name,
                    now=now,
                )
        conn.execute(
            """
            UPDATE programme_adaptation_decisions
            SET applied = ?, resolution = ?, accepted_prescription = ?,
                resolved_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                1 if confirmed else 0,
                resolution,
                accepted or "{}",
                _iso(now),
                decision_id,
                user_id,
            ),
        )
        _audit(
            conn,
            user_id,
            int(existing["revision_id"]),
            "adaptation_decision_resolved",
            {"decision_id": decision_id, "resolution": resolution},
            now=now,
        )
    return {**existing, "applied": bool(confirmed), "resolution": resolution}


def diff_active_source(
    user_id: str,
    current_source: dict[str, Any],
    db_path: str = database.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    revision = get_active_revision(user_id, db_path)
    if not revision:
        raise ValueError("No active programme revision is available.")
    snapshots = revision["definition"].get("source_snapshots") or []
    current = current_source.get("routines") or []
    diff = diff_routine_snapshots(snapshots, current)
    return {
        **diff,
        "revision_id": revision["id"],
        "revision_number": revision["revision_number"],
        "active_source_hash": revision["source_hash"],
    }


def rebase_active_programme(
    user_id: str,
    training_data: HevyTrainingData,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    action: str = "keep",
    request: ProgrammePreviewRequest | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Keep, merge, rebase or replace source drift by creating a new revision."""
    revision = get_active_revision(user_id, db_path)
    if not revision:
        raise ValueError("No active programme revision is available.")
    current_source = serialise_hevy_source(training_data)
    diff = diff_active_source(user_id, current_source, db_path)
    action = str(action or "keep").strip().lower()
    if action not in {"keep", "merge", "rebase", "replace"}:
        raise ValueError("Unsupported drift resolution action.")
    if action == "keep":
        with _connect(db_path) as conn:
            _audit(
                conn,
                user_id,
                int(revision["id"]),
                "source_drift_kept",
                {"diff": diff},
                now=now,
            )
        return {"action": "keep", "diff": diff, "revision": revision}
    spec = dict(revision["definition"].get("programme_spec") or {})
    if request is None:
        allowed = {
            "selected_routine_ids",
            "duration_weeks",
            "goal",
            "start_date",
            "sessions_per_week",
            "experience",
            "max_session_minutes",
            "adaptation_aggressiveness",
        }
        request = ProgrammePreviewRequest(
            **{key: value for key, value in spec.items() if key in allowed}
        )
    preview = build_programme_preview(training_data, request)
    if spec.get("paused"):
        preview["programme_spec"]["paused"] = True
        preview["programme_spec"]["paused_at"] = spec.get("paused_at")
        preview["preview_token"] = compute_preview_token(
            preview["programme_spec"],
            preview["source_snapshots"],
            engine_version=str(preview["engine_version"]),
        )
    validate_programme_definition(preview)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE programmes SET active = 0, updated_at = ? WHERE user_id = ? AND active = 1",
            (_iso(now), user_id),
        )
        merged = _persist_revision(
            conn,
            user_id,
            preview,
            db_path,
            reason=f"drift_{action}",
            supersedes_revision_id=int(revision["id"]),
            programme_id=int(revision["programme_id"]),
            now=now,
        )
        timezone_name = _timezone_for(user_id, db_path)
        week_number = current_week_number(user_id, db_path) or 1
        week, exposures = _materialise(
            conn,
            user_id,
            int(merged["id"]),
            preview,
            max(1, week_number),
            timezone_name,
            now=now,
        )
        _audit(
            conn,
            user_id,
            int(merged["id"]),
            f"source_drift_{action}",
            {
                "diff": diff,
                "supersedes_revision_id": revision["id"],
                "revision_number": merged["revision_number"],
            },
            now=now,
        )
    return {
        "action": action,
        "diff": diff,
        "revision": merged,
        "week": week,
        "exposures": exposures,
    }


def _revise_definition(
    user_id: str,
    definition: dict[str, Any],
    reason: str,
    db_path: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a derived definition as a new revision of the active programme."""
    revision = get_active_revision(user_id, db_path)
    if not revision:
        raise ValueError("No active programme revision is available.")
    definition["preview_token"] = compute_preview_token(
        definition["programme_spec"],
        definition["source_snapshots"],
        engine_version=str(definition["engine_version"]),
    )
    validate_programme_definition(definition)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE programmes
            SET active = 0, updated_at = ?
            WHERE user_id = ? AND active = 1
            """,
            (_iso(now), user_id),
        )
        merged = _persist_revision(
            conn,
            user_id,
            definition,
            db_path,
            reason=reason,
            supersedes_revision_id=int(revision["id"]),
            programme_id=int(revision["programme_id"]),
            now=now,
        )
        _audit(
            conn,
            user_id,
            int(merged["id"]),
            reason,
            {"supersedes_revision_id": revision["id"]},
            now=now,
        )
    return merged


def extend_programme(
    user_id: str,
    additional_weeks: int,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Extend an active programme through a new immutable revision."""
    try:
        extra = int(additional_weeks)
    except (TypeError, ValueError) as exc:
        raise ValueError("Additional weeks must be a positive integer.") from exc
    if extra < 1:
        raise ValueError("Additional weeks must be a positive integer.")
    revision = get_active_revision(user_id, db_path)
    if not revision:
        raise ValueError("No active programme revision is available.")
    definition = deepcopy(revision["definition"])
    spec = definition.get("programme_spec") or {}
    duration = int(spec.get("duration_weeks") or 0) + extra
    spec["duration_weeks"] = duration
    definition["programme_spec"] = spec
    definition["cycle_weeks"] = duration
    blocks = definition.get("blocks") or []
    if not blocks:
        raise ValueError("The active programme has no blocks to extend.")
    last = blocks[-1]
    start = int(last.get("end_week") or 0) + 1
    new_block = deepcopy(last)
    new_block.update(
        {
            "number": max(int(item.get("number") or 0) for item in blocks) + 1,
            "key": f"extension_{start}",
            "name": "Programme extension",
            "start_week": start,
            "end_week": start + extra - 1,
            "duration_weeks": extra,
        }
    )
    blocks.append(new_block)
    for day in definition.get("days") or []:
        for exercise in day.get("exercises") or []:
            prescriptions = exercise.get("prescriptions") or []
            if not prescriptions:
                continue
            extension_prescription = deepcopy(prescriptions[-1])
            extension_prescription["block_number"] = new_block["number"]
            extension_prescription["block_key"] = new_block["key"]
            prescriptions.append(extension_prescription)
    return _revise_definition(
        user_id,
        definition,
        "programme_extension",
        db_path,
        now=now,
    )


def set_programme_paused(
    user_id: str,
    paused: bool,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pause or resume an active programme through a new revision."""
    revision = get_active_revision(user_id, db_path)
    if not revision:
        raise ValueError("No active programme revision is available.")
    definition = deepcopy(revision["definition"])
    spec = definition.get("programme_spec") or {}
    currently_paused = bool(spec.get("paused"))
    timezone_name = _timezone_for(user_id, db_path)
    local_day = _now(now).astimezone(_tz(timezone_name)).date()
    if paused == currently_paused:
        return revision
    if paused:
        spec["paused"] = True
        spec["paused_at"] = local_day.isoformat()
        reason = "programme_paused"
    else:
        paused_at = spec.get("paused_at")
        try:
            paused_day = date.fromisoformat(str(paused_at))
            shift = max(0, (local_day - paused_day).days)
        except (TypeError, ValueError):
            shift = 0
        spec["start_date"] = (
            date.fromisoformat(str(spec["start_date"])) + timedelta(days=shift)
        ).isoformat()
        spec["paused"] = False
        spec.pop("paused_at", None)
        reason = "programme_resumed"
    definition["programme_spec"] = spec
    return _revise_definition(user_id, definition, reason, db_path, now=now)


def programme_state(
    user_id: str,
    db_path: str = database.DEFAULT_DB_PATH,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Return the active revision, current week state and next exposure."""
    revision = get_active_revision(user_id, db_path)
    if not revision:
        return {
            "setup_required": True,
            "revision": None,
            "week": None,
            "next_exposure": None,
        }
    week = ensure_current_week(user_id, db_path, today=today)
    return {
        "setup_required": False,
        "revision": revision,
        "week": week,
        "next_exposure": next_exposure(
            user_id, db_path, revision_id=int(revision["id"])
        ),
    }
