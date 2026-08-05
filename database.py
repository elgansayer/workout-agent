"""SQLite persistence for the workout agent.

Stores the current position in the 6-day cycle and a history of the raw Hevy
payloads so progress can be reviewed over time.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from encryption import decrypt, encrypt
from program import SPLIT_NAME, TOTAL_DAYS

if TYPE_CHECKING:
    from hevy_parser import WorkoutSummary

DEFAULT_DB_PATH = "workout_agent.db"


@contextlib.contextmanager
def _connect(db_path: str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create tables and seed the default programme state if empty."""
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS workout_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                hevy_payload TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_day INTEGER NOT NULL,
                split_name TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS exercise_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                exercise_name TEXT NOT NULL,
                top_weight_kg REAL,
                top_reps INTEGER,
                sets INTEGER NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS hevy_routines (
                routine_key TEXT PRIMARY KEY,
                routine_id TEXT NOT NULL,
                content_hash TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS hevy_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS check_ins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number INTEGER NOT NULL,
                date TEXT NOT NULL,
                workouts_done INTEGER NOT NULL,
                weeks INTEGER NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                day INTEGER,
                focus TEXT NOT NULL,
                carb_tier TEXT NOT NULL,
                plan TEXT NOT NULL,
                lifestyle TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS body_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                weight_kg REAL,
                body_fat_pct REAL,
                muscle_pct REAL,
                resting_hr INTEGER,
                hrv REAL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_insights (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                date TEXT NOT NULL,
                insight_json TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS deep_correlations (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                date TEXT NOT NULL,
                insight_markdown TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reasoning_logs (
                context_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                exercise_name TEXT NOT NULL,
                reasoning TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO programme_state (id, current_day, split_name)
            VALUES (1, 1, ?)
            """,
            (SPLIT_NAME,),
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO hevy_meta (key, value)
            VALUES ('programme_start_date', ?)
            """,
            (datetime.now(tz=timezone.utc).date().isoformat(),),
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_workout_history_date_id ON workout_history (date DESC, id DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_body_metrics_date_id ON body_metrics (date DESC, id DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_log_date_id ON daily_log (date DESC, id DESC)"
        )
        # ⚡ Bolt Optimization: Add indexes to eliminate slow TEMP B-TREE sorts on large progress tables.
        # - idx_exercise_progress_name_id optimizes get_progress_history, get_recent_bests, and get_exercise_volumes
        # - idx_exercise_progress_date optimizes get_session_volumes
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_progress_name_id ON exercise_progress (exercise_name, id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_progress_date ON exercise_progress (date)"
        )

        # Migration: Add hrv column to body_metrics if it doesn't exist
        cursor.execute("PRAGMA table_info(body_metrics)")
        columns = [col[1] for col in cursor.fetchall()]
        if "hrv" not in columns:
            cursor.execute("ALTER TABLE body_metrics ADD COLUMN hrv REAL")

        # ---- Multi-user tables (Sprint 1) ----
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                display_name TEXT,
                created_at TEXT NOT NULL,
                timezone TEXT DEFAULT 'UTC',
                units TEXT DEFAULT 'metric'
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                provider TEXT NOT NULL,
                api_key TEXT NOT NULL,
                client_id TEXT,
                client_secret TEXT,
                refresh_token TEXT,
                extra_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, provider)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                goals TEXT,
                constraints TEXT,
                experience_level TEXT DEFAULT 'intermediate',
                coaching_style TEXT DEFAULT 'direct',
                preferred_ai TEXT DEFAULT 'gemini',
                ai_model TEXT,
                custom_rules TEXT,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )

        # Migration: Add user_id column to workout_history for multi-tenancy
        cursor.execute("PRAGMA table_info(workout_history)")
        woh_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in woh_columns:
            cursor.execute(
                "ALTER TABLE workout_history ADD COLUMN user_id TEXT REFERENCES users(id)"
            )
            # Backfill existing rows with a synthesised legacy user
            from uuid import uuid4

            now = datetime.now(tz=timezone.utc).isoformat()
            # Check if legacy user exists, create if not
            legacy_row = cursor.execute(
                "SELECT id FROM users WHERE email = ?", ("legacy@local",)
            ).fetchone()
            if legacy_row:
                legacy_id = legacy_row[0]
            else:
                legacy_id = str(uuid4())
                cursor.execute(
                    "INSERT INTO users (id, email, display_name, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (legacy_id, "legacy@local", "Legacy Data", now),
                )
            cursor.execute(
                "UPDATE workout_history SET user_id = ? WHERE user_id IS NULL",
                (legacy_id,),
            )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_workout_history_user_date "
            "ON workout_history (user_id, date DESC, id DESC)"
        )


def get_current_day(db_path: str = DEFAULT_DB_PATH) -> int:
    """Return the current day in the cycle (1-6)."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT current_day FROM programme_state WHERE id = 1"
        ).fetchone()
    return int(row[0]) if row else 1


def advance_day(db_path: str = DEFAULT_DB_PATH) -> int:
    """Move to the next day, wrapping from TOTAL_DAYS back to 1."""
    current = get_current_day(db_path)
    nxt = current + 1 if current < TOTAL_DAYS else 1
    with _connect(db_path) as conn:
        conn.execute("UPDATE programme_state SET current_day = ? WHERE id = 1", (nxt,))
    return nxt


def save_workout(
    payload: Any,
    db_path: str = DEFAULT_DB_PATH,
    when: str | None = None,
    *,
    user_id: str | None = None,
) -> None:
    """Persist a raw Hevy payload for historical reference.

    If *user_id* is provided, the workout is scoped to that user.
    """
    if payload is None:
        return
    today = when or datetime.now(tz=timezone.utc).date().isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO workout_history (date, hevy_payload, user_id) "
            "VALUES (?, ?, ?)",
            (today, json.dumps(payload), user_id),
        )


def get_recent_hevy_logs(
    limit: int = 14,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent raw Hevy payloads for autonomous analysis.

    If *user_id* is provided, results are scoped to that user.
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT hevy_payload FROM workout_history "
                "WHERE user_id = ? "
                "ORDER BY date DESC, id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT hevy_payload FROM workout_history "
                "ORDER BY date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    logs = []
    for row in rows:
        try:
            parsed = json.loads(row[0])
            # Handle if it's a list or wrapped in {"workouts": [...]}
            if isinstance(parsed, dict) and "workouts" in parsed:
                logs.extend(parsed["workouts"])
            elif isinstance(parsed, list):
                logs.extend(parsed)
            else:
                logs.append(parsed)
        except Exception:  # noqa: BLE001, S110
            pass
    return logs[:limit]


def save_progress(
    summary: WorkoutSummary | None, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Persist the per-exercise top sets from a parsed workout summary."""
    if summary is None:
        return
    today = (
        summary.date[:10]
        if summary.date
        else datetime.now(tz=timezone.utc).date().isoformat()
    )
    with _connect(db_path) as conn:
        for exercise in summary.exercises:
            conn.execute(
                """
                INSERT INTO exercise_progress
                    (date, exercise_name, top_weight_kg, top_reps, sets)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    today,
                    exercise.name,
                    exercise.top_weight_kg,
                    exercise.top_reps,
                    exercise.sets,
                ),
            )


def get_recent_bests(db_path: str = DEFAULT_DB_PATH) -> dict[str, dict[str, Any]]:
    """Return the most recently logged top set for each exercise by name."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT exercise_name, top_weight_kg, top_reps, sets, date
            FROM exercise_progress
            WHERE id IN (
                SELECT MAX(id) FROM exercise_progress GROUP BY exercise_name
            )
            ORDER BY exercise_name
            """
        ).fetchall()

    bests: dict[str, dict[str, Any]] = {}
    for name, weight, reps, sets, when in rows:
        bests[name] = {
            "top_weight_kg": weight,
            "top_reps": reps,
            "sets": sets,
            "date": when,
        }
    return bests


def get_progress_history(
    limit_per_exercise: int = 12, db_path: str = DEFAULT_DB_PATH
) -> dict[str, list[dict[str, Any]]]:
    """Return recent logged top sets per exercise, oldest first within each."""
    # Performance Optimization (Bolt ⚡): Use a window function to limit the
    # rows returned per exercise at the database level, preventing memory
    # exhaustion and reducing processing time as the database grows.
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT exercise_name, top_weight_kg, top_reps, sets, date
            FROM (
                SELECT exercise_name, top_weight_kg, top_reps, sets, date, id,
                       ROW_NUMBER() OVER (PARTITION BY exercise_name ORDER BY id DESC) as rn
                FROM exercise_progress
            )
            WHERE rn <= ?
            ORDER BY exercise_name, id ASC
            """,
            (limit_per_exercise,),
        ).fetchall()

    series: dict[str, list[dict[str, Any]]] = {}
    for name, weight, reps, sets, when in rows:
        series.setdefault(name, []).append(
            {
                "top_weight_kg": weight,
                "top_reps": reps,
                "sets": sets,
                "date": when,
            }
        )
    return series


def get_session_volumes(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return a per-session training-load proxy, oldest first.

    Only the top set of each exercise is stored, so this is an estimate of
    relative session load (sum of top_weight x top_reps x sets), useful for
    spotting volume trends rather than an exact tonnage figure.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT date,
                   SUM(COALESCE(top_weight_kg, 0) * COALESCE(top_reps, 0) * sets) AS volume,
                   COUNT(*) AS exercises
            FROM exercise_progress
            GROUP BY date
            ORDER BY date ASC
            """
        ).fetchall()
    return [
        {"date": when, "volume": float(volume or 0), "exercises": int(exercises)}
        for when, volume, exercises in rows
    ]


def get_exercise_volumes(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return total logged training-load per exercise, biggest first.

    Like ``get_session_volumes`` this is a top-set proxy (weight x reps x sets),
    useful for breaking volume down by exercise or muscle group.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT exercise_name,
                   SUM(COALESCE(top_weight_kg, 0) * COALESCE(top_reps, 0) * sets) AS volume,
                   COUNT(*) AS sessions
            FROM exercise_progress
            GROUP BY exercise_name
            ORDER BY volume DESC
            """
        ).fetchall()
    return [
        {"exercise": name, "volume": float(volume or 0), "sessions": int(sessions)}
        for name, volume, sessions in rows
    ]


def get_personal_records(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return the all-time best estimated 1RM for each exercise.

    Uses the Epley estimate (weight x (1 + reps / 30)) across every logged top
    set, so personal records surface even as the rep targets change by block.
    """
    # Performance Optimization (Bolt ⚡): Delegate the O(N) iteration and PR calculation
    # to the SQLite engine to eliminate retrieving every historical record into Python
    # memory. SQLite ensures bare columns align with the row containing the MAX value.
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT exercise_name,
                   top_weight_kg,
                   top_reps,
                   date,
                   MAX(top_weight_kg * (1.0 + top_reps / 30.0)) AS e1rm
            FROM exercise_progress
            WHERE top_weight_kg IS NOT NULL AND top_reps IS NOT NULL
            GROUP BY exercise_name
            ORDER BY e1rm DESC
            """
        ).fetchall()

    return [
        {
            "exercise": name,
            "e1rm": float(e1rm),
            "weight_kg": float(weight),
            "reps": int(reps),
            "date": when,
        }
        for name, weight, reps, when, e1rm in rows
    ]


def get_routine_record(
    routine_key: str, db_path: str = DEFAULT_DB_PATH
) -> tuple[str, str] | None:
    """Return (routine_id, content_hash) for a synced routine, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT routine_id, content_hash FROM hevy_routines WHERE routine_key = ?",
            (routine_key,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def save_routine_record(
    routine_key: str,
    routine_id: str,
    content_hash: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Persist the Hevy routine id and content hash for a routine key."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO hevy_routines (routine_key, routine_id, content_hash)
            VALUES (?, ?, ?)
            ON CONFLICT(routine_key) DO UPDATE SET
                routine_id = excluded.routine_id,
                content_hash = excluded.content_hash
            """,
            (routine_key, routine_id, content_hash),
        )


def get_meta(key: str, db_path: str = DEFAULT_DB_PATH) -> str | None:
    """Return a stored metadata value, or None if absent."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM hevy_meta WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """Store a metadata value under the given key."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO hevy_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def delete_routine_record(routine_key: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """Remove a tracked routine record (used when a routine is renamed)."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM hevy_routines WHERE routine_key = ?", (routine_key,))


def get_programme_start_date(db_path: str = DEFAULT_DB_PATH) -> date:
    """Return the programme start date, defaulting to today if unset."""
    value = get_meta("programme_start_date", db_path)
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc).date()


def save_checkin(
    number: int,
    when: str,
    workouts_done: int,
    weeks: int,
    message: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Persist a completed programme check-in for later review."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO check_ins (number, date, workouts_done, weeks, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (number, when, workouts_done, weeks, message),
        )


def get_checkins(
    limit: int = 20, db_path: str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Return recent check-ins, most recent first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT number, date, workouts_done, weeks, message
            FROM check_ins
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "number": number,
            "date": when,
            "workouts_done": workouts_done,
            "weeks": weeks,
            "message": message,
        }
        for number, when, workouts_done, weeks, message in rows
    ]


def save_daily_log(
    when: str,
    day: int | None,
    focus: str,
    carb_tier: str,
    plan: str,
    lifestyle: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Log the full plan and lifestyle guidance issued for a day.

    One row per date: a re-run on the same day replaces the earlier entry so the
    log always holds the latest guidance that was sent.
    """
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM daily_log WHERE date = ?", (when,))
        conn.execute(
            """
            INSERT INTO daily_log (date, day, focus, carb_tier, plan, lifestyle)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (when, day, focus, carb_tier, plan, lifestyle),
        )


def get_daily_logs(
    limit: int = 30, db_path: str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Return recent daily logs, most recent first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT date, day, focus, carb_tier, plan, lifestyle
            FROM daily_log
            ORDER BY date DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "date": when,
            "day": day,
            "focus": focus,
            "carb_tier": carb_tier,
            "plan": plan,
            "lifestyle": lifestyle,
        }
        for when, day, focus, carb_tier, plan, lifestyle in rows
    ]


def save_body_metrics(
    metrics: dict[str, Any] | None,
    when: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Persist a body-composition reading (weight, body fat, muscle, resting HR).

    Accepts the dict produced by `health_connect.body_metrics_from_recovery`, or
    None (a no-op). One row per date: a later reading on the same day replaces
    the earlier one, so the morning weigh-in is what gets stored.
    """
    if not metrics:
        return
    when = when or datetime.now(tz=timezone.utc).date().isoformat()
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM body_metrics WHERE date = ?", (when,))
        conn.execute(
            """
            INSERT INTO body_metrics
                (date, weight_kg, body_fat_pct, muscle_pct, resting_hr, hrv)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                when,
                metrics.get("weight_kg"),
                metrics.get("body_fat_pct"),
                metrics.get("muscle_pct"),
                metrics.get("resting_hr"),
                metrics.get("hrv"),
            ),
        )


def get_body_metrics(
    limit: int = 60, db_path: str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Return recent body-composition readings, oldest first for charting."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT date, weight_kg, body_fat_pct, muscle_pct, resting_hr, hrv
            FROM (
                SELECT id, date, weight_kg, body_fat_pct, muscle_pct, resting_hr, hrv
                FROM body_metrics
                ORDER BY date DESC, id DESC
                LIMIT ?
            )
            ORDER BY date ASC, id ASC
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "date": when,
            "weight_kg": weight,
            "body_fat_pct": body_fat,
            "muscle_pct": muscle,
            "resting_hr": resting_hr,
            "hrv": hrv,
        }
        for when, weight, body_fat, muscle, resting_hr, hrv in rows
    ]


def save_dashboard_insight(insight_json: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """Save the daily dashboard insight JSON."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO dashboard_insights (id, date, insight_json)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                date = excluded.date,
                insight_json = excluded.insight_json
            """,
            (datetime.now(tz=timezone.utc).date().isoformat(), insight_json),
        )


def get_dashboard_insight(db_path: str = DEFAULT_DB_PATH) -> dict | None:
    """Get the latest dashboard insight JSON as a dict."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT insight_json FROM dashboard_insights WHERE id = 1"
        ).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            pass
    return None


def save_reasoning_log(
    context_id: str, exercise_name: str, reasoning: str, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Save an AI reasoning log for an exercise change."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO reasoning_logs (context_id, date, exercise_name, reasoning)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(context_id) DO UPDATE SET
                reasoning = excluded.reasoning
            """,
            (
                context_id,
                datetime.now(tz=timezone.utc).date().isoformat(),
                exercise_name,
                reasoning,
            ),
        )


def get_reasoning_log(context_id: str, db_path: str = DEFAULT_DB_PATH) -> str | None:
    """Get the reasoning log by context_id."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT reasoning FROM reasoning_logs WHERE context_id = ?", (context_id,)
        ).fetchone()
    return row[0] if row else None


def save_deep_correlation(
    insight_markdown: str, db_path: str = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO deep_correlations (id, date, insight_markdown)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                date = excluded.date,
                insight_markdown = excluded.insight_markdown
            """,
            (datetime.now(tz=timezone.utc).date().isoformat(), insight_markdown),
        )


def get_deep_correlation(db_path: str = DEFAULT_DB_PATH) -> str | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT insight_markdown FROM deep_correlations WHERE id = 1"
        ).fetchone()
    return row[0] if row else None


def save_chat_message(role: str, content: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """Persist a chat message (role is 'user' or 'assistant')."""
    from datetime import datetime

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (role, content, created_at)
            VALUES (?, ?, ?)
            """,
            (role, content, datetime.now(tz=timezone.utc).isoformat()),
        )


def get_chat_messages(
    limit: int = 50, db_path: str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Return chat messages, oldest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT role, content, created_at FROM (
                SELECT role, content, created_at
                FROM chat_messages
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY rowid ASC
            """,
            (limit,),
        ).fetchall()
    return [
        {"role": role, "content": content, "created_at": created_at}
        for role, content, created_at in rows
    ]


def clear_chat_messages(db_path: str = DEFAULT_DB_PATH) -> None:
    """Delete all chat messages."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM chat_messages")


# ---------------------------------------------------------------------------
# Multi-user management (Sprint 1)
# ---------------------------------------------------------------------------


def get_or_create_user(
    email: str,
    display_name: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return the user row for an email, creating one on first login."""
    import uuid
    from datetime import datetime

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, email, display_name, created_at, timezone, units "
            "FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if row:
            return {
                "id": row[0],
                "email": row[1],
                "display_name": row[2],
                "created_at": row[3],
                "timezone": row[4],
                "units": row[5],
            }

        user_id = str(uuid.uuid4())
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO users (id, email, display_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, email, display_name, now),
        )
        return {
            "id": user_id,
            "email": email,
            "display_name": display_name,
            "created_at": now,
            "timezone": "UTC",
            "units": "metric",
        }


def get_user_by_id(
    user_id: str, db_path: str = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    """Return the user row for a user_id, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, email, display_name, created_at, timezone, units "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "display_name": row[2],
        "created_at": row[3],
        "timezone": row[4],
        "units": row[5],
    }


# ---- API key management ----


def save_user_api_key(
    user_id: str,
    provider: str,
    api_key: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    extra: dict[str, Any] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Store (or update) an encrypted API key for a user + provider pair."""
    from datetime import datetime

    now = datetime.now(tz=timezone.utc).isoformat()
    encrypted_key = encrypt(api_key) if api_key else ""
    encrypted_secret = encrypt(client_secret) if client_secret else None
    encrypted_refresh = encrypt(refresh_token) if refresh_token else None
    extra_json = json.dumps(extra) if extra else None

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_api_keys
                (user_id, provider, api_key, client_id, client_secret,
                 refresh_token, extra_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                api_key = excluded.api_key,
                client_id = excluded.client_id,
                client_secret = excluded.client_secret,
                refresh_token = excluded.refresh_token,
                extra_json = excluded.extra_json,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                provider.lower(),
                encrypted_key,
                client_id,
                encrypted_secret,
                encrypted_refresh,
                extra_json,
                now,
                now,
            ),
        )


def get_user_api_key(
    user_id: str, provider: str, db_path: str = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    """Return the decrypted API key record for a user + provider, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT api_key, client_id, client_secret, refresh_token,
                   extra_json, updated_at
            FROM user_api_keys
            WHERE user_id = ? AND provider = ?
            """,
            (user_id, provider.lower()),
        ).fetchone()
    if not row:
        return None
    return {
        "api_key": decrypt(row[0]) if row[0] else "",
        "client_id": row[1],
        "client_secret": decrypt(row[2]) if row[2] else None,
        "refresh_token": decrypt(row[3]) if row[3] else None,
        "extra": json.loads(row[4]) if row[4] else None,
        "updated_at": row[5],
    }


def get_user_api_keys(
    user_id: str, db_path: str = DEFAULT_DB_PATH
) -> dict[str, dict[str, Any]]:
    """Return all API key records for a user, keyed by provider name.

    Keys are decrypted. Providers without a stored key are omitted.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT provider, api_key, client_id, client_secret,
                   refresh_token, extra_json, updated_at
            FROM user_api_keys
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()

    result: dict[str, dict[str, Any]] = {}
    for provider, key, cid, csecret, rtoken, extra, updated in rows:
        result[provider] = {
            "api_key": decrypt(key) if key else "",
            "client_id": cid,
            "client_secret": decrypt(csecret) if csecret else None,
            "refresh_token": decrypt(rtoken) if rtoken else None,
            "extra": json.loads(extra) if extra else None,
            "updated_at": updated,
        }
    return result


def delete_user_api_key(
    user_id: str, provider: str, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Remove a stored API key for a user + provider."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM user_api_keys WHERE user_id = ? AND provider = ?",
            (user_id, provider.lower()),
        )


# ---- User preferences ----


def save_user_preferences(
    user_id: str,
    *,
    goals: list[str] | None = None,
    constraints: list[str] | None = None,
    experience_level: str | None = None,
    coaching_style: str | None = None,
    preferred_ai: str | None = None,
    ai_model: str | None = None,
    custom_rules: list[str] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Save or update a user's training preferences."""
    from datetime import datetime

    now = datetime.now(tz=timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_preferences
                (user_id, goals, constraints, experience_level,
                 coaching_style, preferred_ai, ai_model, custom_rules, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                goals = COALESCE(excluded.goals, user_preferences.goals),
                constraints = COALESCE(excluded.constraints, user_preferences.constraints),
                experience_level = COALESCE(excluded.experience_level, user_preferences.experience_level),
                coaching_style = COALESCE(excluded.coaching_style, user_preferences.coaching_style),
                preferred_ai = COALESCE(excluded.preferred_ai, user_preferences.preferred_ai),
                ai_model = COALESCE(excluded.ai_model, user_preferences.ai_model),
                custom_rules = COALESCE(excluded.custom_rules, user_preferences.custom_rules),
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                json.dumps(goals) if goals is not None else None,
                json.dumps(constraints) if constraints is not None else None,
                experience_level,
                coaching_style,
                preferred_ai,
                ai_model,
                json.dumps(custom_rules) if custom_rules is not None else None,
                now,
            ),
        )


def get_user_preferences(
    user_id: str, db_path: str = DEFAULT_DB_PATH
) -> dict[str, Any]:
    """Return the user's training preferences, with defaults for unset fields."""
    defaults: dict[str, Any] = {
        "goals": [],
        "constraints": [],
        "experience_level": "intermediate",
        "coaching_style": "direct",
        "preferred_ai": "gemini",
        "ai_model": None,
        "custom_rules": [],
    }
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT goals, constraints, experience_level, coaching_style,
                   preferred_ai, ai_model, custom_rules
            FROM user_preferences
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    if not row:
        return defaults
    return {
        "goals": json.loads(row[0]) if row[0] else [],
        "constraints": json.loads(row[1]) if row[1] else [],
        "experience_level": row[2] or "intermediate",
        "coaching_style": row[3] or "direct",
        "preferred_ai": row[4] or "gemini",
        "ai_model": row[5],
        "custom_rules": json.loads(row[6]) if row[6] else [],
    }
