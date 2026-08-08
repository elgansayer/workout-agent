"""SQLite persistence for the workout agent.

Stores the current position in the 6-day cycle and a history of the raw Hevy
payloads so progress can be reviewed over time.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from encryption import decrypt, encrypt
from program import SPLIT_NAME, TOTAL_DAYS

if TYPE_CHECKING:
    from hevy_parser import WorkoutSummary

DEFAULT_DB_PATH = "workout_agent.db"


class UserRow(TypedDict):
    id: str
    email: str
    display_name: str | None
    created_at: str
    timezone: str
    units: str


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
            """,
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_day INTEGER NOT NULL,
                split_name TEXT NOT NULL
            )
            """,
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
            """,
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS hevy_routines (
                routine_key TEXT PRIMARY KEY,
                routine_id TEXT NOT NULL,
                content_hash TEXT NOT NULL
            )
            """,
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS hevy_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
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
            """,
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
            """,
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
            """,
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_insights (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                date TEXT NOT NULL,
                insight_json TEXT NOT NULL
            )
            """,
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS deep_correlations (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                date TEXT NOT NULL,
                insight_markdown TEXT NOT NULL
            )
            """,
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reasoning_logs (
                context_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                exercise_name TEXT NOT NULL,
                reasoning TEXT NOT NULL
            )
            """,
        )

        # Note: For new databases the initial programme_state row is
        # inserted by the migration block below (after table recreation).

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        )

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_workout_history_date_id ON workout_history (date DESC, id DESC)",
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_body_metrics_date_id ON body_metrics (date DESC, id DESC)",
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_log_date_id ON daily_log (date DESC, id DESC)",
        )
        # ⚡ Bolt Optimization: Add indexes to eliminate slow TEMP B-TREE sorts on large progress tables.
        # - idx_exercise_progress_name_id optimizes get_progress_history, get_recent_bests, and get_exercise_volumes
        # - idx_exercise_progress_date optimizes get_session_volumes
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_progress_name_id ON exercise_progress (exercise_name, id)",
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_progress_date ON exercise_progress (date)",
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
            """,
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
            """,
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limits_ip_ts ON rate_limits (ip, timestamp)",
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
            """,
        )

        # Migration: Add user_id column to workout_history for multi-tenancy
        cursor.execute("PRAGMA table_info(workout_history)")
        woh_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in woh_columns:
            cursor.execute(
                "ALTER TABLE workout_history ADD COLUMN user_id TEXT REFERENCES users(id)",
            )
            # Backfill existing rows with a synthesised legacy user
            from uuid import uuid4

            now = datetime.now(tz=timezone.utc).isoformat()
            # Check if legacy user exists, create if not
            legacy_row = cursor.execute(
                "SELECT id FROM users WHERE email = ?",
                ("legacy@local",),
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
            "ON workout_history (user_id, date DESC, id DESC)",
        )

        # Migration: Add user_id column to exercise_progress for multi-tenancy
        cursor.execute("PRAGMA table_info(exercise_progress)")
        ep_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in ep_columns:
            cursor.execute(
                "ALTER TABLE exercise_progress ADD COLUMN user_id TEXT REFERENCES users(id)",
            )
            cursor.execute(
                "UPDATE exercise_progress SET user_id = ? WHERE user_id IS NULL",
                (legacy_id,),
            )
        cursor.execute("DROP INDEX IF EXISTS idx_exercise_progress_user")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_progress_user_name_id "
            "ON exercise_progress (user_id, exercise_name, id DESC)",
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_progress_user_date "
            "ON exercise_progress (user_id, date ASC)",
        )

        # Migration: Add user_id column to body_metrics for multi-tenancy
        cursor.execute("PRAGMA table_info(body_metrics)")
        bm_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in bm_columns:
            cursor.execute(
                "ALTER TABLE body_metrics ADD COLUMN user_id TEXT REFERENCES users(id)",
            )
            cursor.execute(
                "UPDATE body_metrics SET user_id = ? WHERE user_id IS NULL",
                (legacy_id,),
            )
        cursor.execute("DROP INDEX IF EXISTS idx_body_metrics_user")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_body_metrics_user_date_id "
            "ON body_metrics (user_id, date DESC, id DESC)",
        )

        # ---- Programme templates table (multi-user) ----
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programmes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                source TEXT NOT NULL,  -- 'template' | 'inferred' | 'custom'
                template_key TEXT,
                definition TEXT NOT NULL,  -- JSON blob for day/exercise definitions
                active INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, template_key)
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programmes_user_active "
            "ON programmes (user_id, active)",
        )

        # ---- Multi-tenant migration helper ----
        def _ensure_legacy_user(cur: sqlite3.Cursor) -> str:
            """Return the stable legacy user id, creating the user row if needed."""
            from uuid import uuid4

            row = cur.execute(
                "SELECT id FROM users WHERE email = ?",
                ("legacy@local",),
            ).fetchone()
            if row:
                return str(row[0])
            legacy_id2 = str(uuid4())
            cur.execute(
                "INSERT INTO users (id, email, display_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    legacy_id2,
                    "legacy@local",
                    "Legacy Data",
                    datetime.now(tz=timezone.utc).isoformat(),
                ),
            )
            return legacy_id2

        # Migration: Add user_id column to chat_messages
        cursor.execute("PRAGMA table_info(chat_messages)")
        cm_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in cm_columns:
            cursor.execute(
                "ALTER TABLE chat_messages ADD COLUMN user_id TEXT REFERENCES users(id)",
            )
            chat_legacy_id = _ensure_legacy_user(cursor)
            cursor.execute(
                "UPDATE chat_messages SET user_id = ? WHERE user_id IS NULL",
                (chat_legacy_id,),
            )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_user "
            "ON chat_messages (user_id, id DESC)",
        )

        # Migration: Migrate reasoning_logs to user_id-scoped composite PK
        cursor.execute("PRAGMA table_info(reasoning_logs)")
        rl_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in rl_columns:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS reasoning_logs_new (
                    user_id TEXT NOT NULL REFERENCES users(id),
                    context_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    exercise_name TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    PRIMARY KEY (user_id, context_id)
                )
                """,
            )
            old_rows = cursor.execute(
                "SELECT context_id, date, exercise_name, reasoning FROM reasoning_logs",
            ).fetchall()
            if old_rows:
                rl_legacy_id = _ensure_legacy_user(cursor)
                for row in old_rows:
                    cursor.execute(
                        "INSERT OR REPLACE INTO reasoning_logs_new "
                        "(user_id, context_id, date, exercise_name, reasoning) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (rl_legacy_id, row[0], row[1], row[2], row[3]),
                    )
            cursor.execute("DROP TABLE reasoning_logs")
            cursor.execute(
                "ALTER TABLE reasoning_logs_new RENAME TO reasoning_logs",
            )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_reasoning_logs_user "
            "ON reasoning_logs (user_id, context_id)",
        )

        # Migration: Migrate dashboard_insights from singleton to user_id-scoped
        cursor.execute("PRAGMA table_info(dashboard_insights)")
        di_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in di_columns:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS dashboard_insights_new (
                    user_id TEXT NOT NULL REFERENCES users(id),
                    date TEXT NOT NULL,
                    insight_json TEXT NOT NULL,
                    PRIMARY KEY (user_id)
                )
                """,
            )
            old_row = cursor.execute(
                "SELECT date, insight_json FROM dashboard_insights WHERE id = 1",
            ).fetchone()
            if old_row:
                di_legacy_id = _ensure_legacy_user(cursor)
                cursor.execute(
                    "INSERT OR REPLACE INTO dashboard_insights_new "
                    "(user_id, date, insight_json) VALUES (?, ?, ?)",
                    (di_legacy_id, old_row[0], old_row[1]),
                )
            cursor.execute("DROP TABLE dashboard_insights")
            cursor.execute(
                "ALTER TABLE dashboard_insights_new RENAME TO dashboard_insights",
            )

        # Migration: Migrate deep_correlations from singleton to user_id-scoped
        cursor.execute("PRAGMA table_info(deep_correlations)")
        dc_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in dc_columns:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS deep_correlations_new (
                    user_id TEXT NOT NULL REFERENCES users(id),
                    date TEXT NOT NULL,
                    insight_markdown TEXT NOT NULL,
                    PRIMARY KEY (user_id)
                )
                """,
            )
            old_row = cursor.execute(
                "SELECT date, insight_markdown FROM deep_correlations WHERE id = 1",
            ).fetchone()
            if old_row:
                dc_legacy_id = _ensure_legacy_user(cursor)
                cursor.execute(
                    "INSERT OR REPLACE INTO deep_correlations_new "
                    "(user_id, date, insight_markdown) VALUES (?, ?, ?)",
                    (dc_legacy_id, old_row[0], old_row[1]),
                )
            cursor.execute("DROP TABLE deep_correlations")
            cursor.execute(
                "ALTER TABLE deep_correlations_new RENAME TO deep_correlations",
            )

        # Migration: Add user_id column to daily_log for multi-tenancy
        cursor.execute("PRAGMA table_info(daily_log)")
        dl_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in dl_columns:
            cursor.execute(
                "ALTER TABLE daily_log ADD COLUMN user_id TEXT REFERENCES users(id)",
            )
            dl_legacy_id = _ensure_legacy_user(cursor)
            cursor.execute(
                "UPDATE daily_log SET user_id = ? WHERE user_id IS NULL",
                (dl_legacy_id,),
            )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_log_user_date "
            "ON daily_log (user_id, date DESC, id DESC)",
        )

        # Migration: Add user_id column to check_ins for multi-tenancy
        cursor.execute("PRAGMA table_info(check_ins)")
        ci_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in ci_columns:
            cursor.execute(
                "ALTER TABLE check_ins ADD COLUMN user_id TEXT REFERENCES users(id)",
            )
            ci_legacy_id = _ensure_legacy_user(cursor)
            cursor.execute(
                "UPDATE check_ins SET user_id = ? WHERE user_id IS NULL",
                (ci_legacy_id,),
            )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_check_ins_user "
            "ON check_ins (user_id, id DESC)",
        )

        # Migration: Migrate programme_state from singleton to user_id-scoped
        cursor.execute("PRAGMA table_info(programme_state)")
        ps_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in ps_columns:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS programme_state_new (
                    user_id TEXT NOT NULL REFERENCES users(id),
                    current_day INTEGER NOT NULL DEFAULT 1,
                    split_name TEXT NOT NULL,
                    PRIMARY KEY (user_id)
                )
                """,
            )
            old_row = cursor.execute(
                "SELECT current_day, split_name FROM programme_state WHERE id = 1",
            ).fetchone()
            if old_row:
                ps_legacy_id = _ensure_legacy_user(cursor)
                cursor.execute(
                    "INSERT OR REPLACE INTO programme_state_new "
                    "(user_id, current_day, split_name) VALUES (?, ?, ?)",
                    (ps_legacy_id, old_row[0], old_row[1]),
                )
            else:
                # Brand-new database: seed a default row for the legacy user
                ps_legacy_id = _ensure_legacy_user(cursor)
                cursor.execute(
                    "INSERT OR REPLACE INTO programme_state_new "
                    "(user_id, current_day, split_name) VALUES (?, 1, ?)",
                    (ps_legacy_id, SPLIT_NAME),
                )
            cursor.execute("DROP TABLE programme_state")
            cursor.execute("ALTER TABLE programme_state_new RENAME TO programme_state")

        # Migration: Migrate hevy_meta from singleton key-value to (user_id, key) composite PK
        cursor.execute("PRAGMA table_info(hevy_meta)")
        hm_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in hm_columns:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS hevy_meta_new (
                    user_id TEXT NOT NULL REFERENCES users(id),
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (user_id, key)
                )
                """,
            )
            old_rows = cursor.execute(
                "SELECT key, value FROM hevy_meta",
            ).fetchall()
            hm_legacy_id = _ensure_legacy_user(cursor)
            for row in old_rows:
                cursor.execute(
                    "INSERT OR REPLACE INTO hevy_meta_new "
                    "(user_id, key, value) VALUES (?, ?, ?)",
                    (hm_legacy_id, row[0], row[1]),
                )
            # Seed programme_start_date for the legacy user if no row exists
            cursor.execute(
                "INSERT OR IGNORE INTO hevy_meta_new (user_id, key, value) "
                "VALUES (?, 'programme_start_date', ?)",
                (hm_legacy_id, datetime.now(tz=timezone.utc).date().isoformat()),
            )
            cursor.execute("DROP TABLE hevy_meta")
            cursor.execute("ALTER TABLE hevy_meta_new RENAME TO hevy_meta")

        # Migration: Add user_id column to hevy_routines for multi-tenancy
        cursor.execute("PRAGMA table_info(hevy_routines)")
        hr_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in hr_columns:
            # Need to recreate with composite PK since routine_key alone won't be unique across users
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS hevy_routines_new (
                    user_id TEXT NOT NULL REFERENCES users(id),
                    routine_key TEXT NOT NULL,
                    routine_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (user_id, routine_key)
                )
                """,
            )
            old_rows = cursor.execute(
                "SELECT routine_key, routine_id, content_hash FROM hevy_routines",
            ).fetchall()
            hr_legacy_id = _ensure_legacy_user(cursor)
            for row in old_rows:
                cursor.execute(
                    "INSERT OR REPLACE INTO hevy_routines_new "
                    "(user_id, routine_key, routine_id, content_hash) "
                    "VALUES (?, ?, ?, ?)",
                    (hr_legacy_id, row[0], row[1], row[2]),
                )
            cursor.execute("DROP TABLE hevy_routines")
            cursor.execute("ALTER TABLE hevy_routines_new RENAME TO hevy_routines")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_hevy_routines_user "
            "ON hevy_routines (user_id, routine_key)",
        )


def get_current_day(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> int:
    """Return the current day in the cycle (1-6).

    When *user_id* is provided, the day is scoped to that user.
    When None, returns the first row found (backward-compatible single-tenant path).
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            row = conn.execute(
                "SELECT current_day FROM programme_state WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT current_day FROM programme_state LIMIT 1",
            ).fetchone()
    return int(row[0]) if row else 1


def advance_day(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> int:
    """Move to the next day, wrapping from TOTAL_DAYS back to 1.

    When *user_id* is provided, scoped to that user.
    """
    current = get_current_day(db_path, user_id=user_id)
    nxt = current + 1 if current < TOTAL_DAYS else 1
    with _connect(db_path) as conn:
        if user_id is not None:
            conn.execute(
                "UPDATE programme_state SET current_day = ? WHERE user_id = ?",
                (nxt, user_id),
            )
        else:
            conn.execute(
                "UPDATE programme_state SET current_day = ?",
                (nxt,),
            )

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
    summary: WorkoutSummary | None,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Persist the per-exercise top sets from a parsed workout summary.

    If *user_id* is provided, the progress is scoped to that user.
    """
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
                    (date, exercise_name, top_weight_kg, top_reps, sets, user_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    today,
                    exercise.name,
                    exercise.top_weight_kg,
                    exercise.top_reps,
                    exercise.sets,
                    user_id,
                ),
            )


def get_recent_bests(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the most recently logged top set for each exercise by name.

    If *user_id* is provided, results are scoped to that user.
    """
    # Performance Optimization (Bolt ⚡): Delegate the MAX(id) operation directly
    # into the main SELECT clause. SQLite guarantees un-aggregated columns correspond
    # to the MAX value, allowing direct use of idx_exercise_progress_user_name_id index
    # without slow subquery lookups.
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT exercise_name, top_weight_kg, top_reps, sets, date, MAX(id)
                FROM exercise_progress
                WHERE user_id = ?
                GROUP BY exercise_name
                ORDER BY exercise_name
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT exercise_name, top_weight_kg, top_reps, sets, date, MAX(id)
                FROM exercise_progress
                GROUP BY exercise_name
                ORDER BY exercise_name
                """,
            ).fetchall()

    bests: dict[str, dict[str, Any]] = {}
    for name, weight, reps, sets, when, _ in rows:
        bests[name] = {
            "top_weight_kg": weight,
            "top_reps": reps,
            "sets": sets,
            "date": when,
        }
    return bests


def get_progress_history(
    limit_per_exercise: int = 12,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return recent logged top sets per exercise, oldest first within each.

    If *user_id* is provided, results are scoped to that user.
    """
    # Performance Optimization (Bolt ⚡): Use a window function to limit the
    # rows returned per exercise at the database level, preventing memory
    # exhaustion and reducing processing time as the database grows.
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT exercise_name, top_weight_kg, top_reps, sets, date
                FROM (
                    SELECT exercise_name, top_weight_kg, top_reps, sets, date, id,
                           ROW_NUMBER() OVER (PARTITION BY exercise_name ORDER BY id DESC) as rn
                    FROM exercise_progress
                    WHERE user_id = ?
                )
                WHERE rn <= ?
                ORDER BY exercise_name, id ASC
                """,
                (user_id, limit_per_exercise),
            ).fetchall()
        else:
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
            },
        )
    return series


def get_session_volumes(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return a per-session training-load proxy, oldest first.

    Only the top set of each exercise is stored, so this is an estimate of
    relative session load (sum of top_weight x top_reps x sets), useful for
    spotting volume trends rather than an exact tonnage figure.

    If *user_id* is provided, results are scoped to that user.
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT date,
                       SUM(COALESCE(top_weight_kg, 0) * COALESCE(top_reps, 0) * sets) AS volume,
                       COUNT(*) AS exercises
                FROM exercise_progress
                WHERE user_id = ?
                GROUP BY date
                ORDER BY date ASC
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT date,
                       SUM(COALESCE(top_weight_kg, 0) * COALESCE(top_reps, 0) * sets) AS volume,
                       COUNT(*) AS exercises
                FROM exercise_progress
                GROUP BY date
                ORDER BY date ASC
                """,
            ).fetchall()
    return [
        {"date": when, "volume": float(volume or 0), "exercises": int(exercises)}
        for when, volume, exercises in rows
    ]


def get_exercise_volumes(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return total logged training-load per exercise, biggest first.

    Like ``get_session_volumes`` this is a top-set proxy (weight x reps x sets),
    useful for breaking volume down by exercise or muscle group.

    If *user_id* is provided, results are scoped to that user.
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT exercise_name,
                       SUM(COALESCE(top_weight_kg, 0) * COALESCE(top_reps, 0) * sets) AS volume,
                       COUNT(*) AS sessions
                FROM exercise_progress
                WHERE user_id = ?
                GROUP BY exercise_name
                ORDER BY volume DESC
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT exercise_name,
                       SUM(COALESCE(top_weight_kg, 0) * COALESCE(top_reps, 0) * sets) AS volume,
                       COUNT(*) AS sessions
                FROM exercise_progress
                GROUP BY exercise_name
                ORDER BY volume DESC
                """,
            ).fetchall()
    return [
        {"exercise": name, "volume": float(volume or 0), "sessions": int(sessions)}
        for name, volume, sessions in rows
    ]


def get_personal_records(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the all-time best estimated 1RM for each exercise.

    Uses the Epley estimate (weight x (1 + reps / 30)) across every logged top
    set, so personal records surface even as the rep targets change by block.

    If *user_id* is provided, results are scoped to that user.
    """
    # Performance Optimization (Bolt ⚡): Delegate the O(N) iteration and PR calculation
    # to the SQLite engine to eliminate retrieving every historical record into Python
    # memory. SQLite ensures bare columns align with the row containing the MAX value.
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT exercise_name,
                       top_weight_kg,
                       top_reps,
                       date,
                       MAX(top_weight_kg * (1.0 + top_reps / 30.0)) AS e1rm
                FROM exercise_progress
                WHERE top_weight_kg IS NOT NULL AND top_reps IS NOT NULL
                  AND user_id = ?
                GROUP BY exercise_name
                ORDER BY e1rm DESC
                """,
                (user_id,),
            ).fetchall()
        else:
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
                """,
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
    routine_key: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> tuple[str, str] | None:
    """Return (routine_id, content_hash) for a synced routine, or None.

    If *user_id* is provided, the lookup is scoped to that user.
    When None, returns the first matching row (backward-compatible path).
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            row = conn.execute(
                "SELECT routine_id, content_hash FROM hevy_routines "
                "WHERE user_id = ? AND routine_key = ?",
                (user_id, routine_key),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT routine_id, content_hash FROM hevy_routines "
                "WHERE routine_key = ? LIMIT 1",
                (routine_key,),
            ).fetchone()
    return (row[0], row[1]) if row else None


def save_routine_record(
    routine_key: str,
    routine_id: str,
    content_hash: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Persist the Hevy routine id and content hash for a routine key.

    If *user_id* is provided, the record is scoped to that user.
    When None, uses the legacy user for backward compatibility.
    """
    with _connect(db_path) as conn:
        if user_id is None:
            # Resolve the legacy user for backward-compat writes
            row = conn.execute(
                "SELECT id FROM users WHERE email = ?", ("legacy@local",)
            ).fetchone()
            user_id = row[0] if row else None
        if user_id is None:
            return  # no legacy user, nothing to do
        conn.execute(
            """
            INSERT INTO hevy_routines
                (user_id, routine_key, routine_id, content_hash)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, routine_key) DO UPDATE SET
                routine_id = excluded.routine_id,
                content_hash = excluded.content_hash
            """,
            (user_id, routine_key, routine_id, content_hash),
        )


def get_meta(
    key: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> str | None:
    """Return a stored metadata value, or None if absent.

    If *user_id* is provided, the lookup is scoped to that user.
    When None, returns the first matching row (backward-compatible path).
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            row = conn.execute(
                "SELECT value FROM hevy_meta WHERE user_id = ? AND key = ?",
                (user_id, key),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT value FROM hevy_meta WHERE key = ? LIMIT 1",
                (key,),
            ).fetchone()
    return row[0] if row else None


def set_meta(
    key: str,
    value: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Store a metadata value under the given key.

    If *user_id* is provided, the value is scoped to that user.
    When None, uses the legacy user for backward compatibility.
    """
    with _connect(db_path) as conn:
        if user_id is None:
            # Resolve the legacy user for backward-compat writes
            row = conn.execute(
                "SELECT id FROM users WHERE email = ?", ("legacy@local",)
            ).fetchone()
            user_id = row[0] if row else None
        if user_id is None:
            return  # no legacy user, nothing to do
        conn.execute(
            """
            INSERT INTO hevy_meta (user_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (user_id, key, value),
        )


def check_rate_limit(
    ip: str,
    limit: int = 10,
    window: int = 60,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Return True if the IP is within the rate-limit window, inserting a new entry.

    Old entries outside the window are cleaned up on each call. When the number
    of remaining entries for this IP within *window* seconds exceeds *limit*,
    the function returns True (rate-limited) without inserting a new row.
    """
    now = time.time()
    cutoff = now - window
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM rate_limits WHERE timestamp < ?", (cutoff,))
        count = conn.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE ip = ?",
            (ip,),
        ).fetchone()[0]
        if count >= limit:
            return True
        conn.execute("INSERT INTO rate_limits (ip, timestamp) VALUES (?, ?)", (ip, now))
        return False


def delete_routine_record(
    routine_key: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Remove a tracked routine record (used when a routine is renamed).

    If *user_id* is provided, the delete is scoped to that user.
    When None, deletes any matching row (backward-compatible).
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            conn.execute(
                "DELETE FROM hevy_routines WHERE user_id = ? AND routine_key = ?",
                (user_id, routine_key),
            )
        else:
            conn.execute(
                "DELETE FROM hevy_routines WHERE routine_key = ?",
                (routine_key,),
            )


def get_programme_start_date(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> date:
    """Return the programme start date, defaulting to today if unset.

    If *user_id* is provided, the lookup is scoped to that user.
    """
    value = get_meta("programme_start_date", db_path, user_id=user_id)
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
    *,
    user_id: str | None = None,
) -> None:
    """Persist a completed programme check-in for later review.

    If *user_id* is provided, the check-in is scoped to that user.
    """
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO check_ins (number, date, workouts_done, weeks, message, user_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (number, when, workouts_done, weeks, message, user_id),
        )


def get_checkins(
    limit: int = 20,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent check-ins, most recent first.

    If *user_id* is provided, results are scoped to that user.
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT number, date, workouts_done, weeks, message
                FROM check_ins
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        else:
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
    *,
    user_id: str | None = None,
) -> None:
    """Log the full plan and lifestyle guidance issued for a day.

    One row per date per user: a re-run on the same day replaces the earlier
    entry so the log always holds the latest guidance that was sent.

    If *user_id* is provided, the log is scoped to that user.
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            conn.execute(
                "DELETE FROM daily_log WHERE date = ? AND user_id = ?",
                (when, user_id),
            )
        else:
            conn.execute("DELETE FROM daily_log WHERE date = ?", (when,))
        conn.execute(
            """
            INSERT INTO daily_log (date, day, focus, carb_tier, plan, lifestyle, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (when, day, focus, carb_tier, plan, lifestyle, user_id),
        )


def get_daily_logs(
    limit: int = 30,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent daily logs, most recent first.

    If *user_id* is provided, results are scoped to that user.
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT date, day, focus, carb_tier, plan, lifestyle
                FROM daily_log
                WHERE user_id = ?
                ORDER BY date DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        else:
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
    *,
    user_id: str | None = None,
) -> None:
    """Persist a body-composition reading (weight, body fat, muscle, resting HR).

    Accepts the dict produced by `health_connect.body_metrics_from_recovery`, or
    None (a no-op). One row per date per user: a later reading on the same day
    replaces the earlier one, so the morning weigh-in is what gets stored.

    If *user_id* is provided, the metrics are scoped to that user.
    """
    if not metrics:
        return
    when = when or datetime.now(tz=timezone.utc).date().isoformat()
    with _connect(db_path) as conn:
        if user_id is not None:
            conn.execute(
                "DELETE FROM body_metrics WHERE date = ? AND user_id = ?",
                (when, user_id),
            )
        else:
            conn.execute("DELETE FROM body_metrics WHERE date = ?", (when,))
        conn.execute(
            """
            INSERT INTO body_metrics
                (date, weight_kg, body_fat_pct, muscle_pct, resting_hr, hrv, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                when,
                metrics.get("weight_kg"),
                metrics.get("body_fat_pct"),
                metrics.get("muscle_pct"),
                metrics.get("resting_hr"),
                metrics.get("hrv"),
                user_id,
            ),
        )


def get_body_metrics(
    limit: int = 60,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent body-composition readings, oldest first for charting.

    If *user_id* is provided, results are scoped to that user.
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT date, weight_kg, body_fat_pct, muscle_pct, resting_hr, hrv
                FROM (
                    SELECT id, date, weight_kg, body_fat_pct, muscle_pct, resting_hr, hrv
                    FROM body_metrics
                    WHERE user_id = ?
                    ORDER BY date DESC, id DESC
                    LIMIT ?
                )
                ORDER BY date ASC, id ASC
                """,
                (user_id, limit),
            ).fetchall()
        else:
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


def save_dashboard_insight(
    insight_json: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Save the daily dashboard insight JSON.

    If *user_id* is provided, the insight is scoped to that user.
    """
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO dashboard_insights (user_id, date, insight_json)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                date = excluded.date,
                insight_json = excluded.insight_json
            """,
            (user_id, datetime.now(tz=timezone.utc).date().isoformat(), insight_json),
        )


def get_dashboard_insight(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Get the latest dashboard insight JSON as a dict.

    If *user_id* is provided, returns only that user's insight.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT insight_json FROM dashboard_insights WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row:
        try:
            result = json.loads(row[0])
            if isinstance(result, dict):
                return result
            return None
        except json.JSONDecodeError:
            pass
    return None


def save_reasoning_log(
    context_id: str,
    exercise_name: str,
    reasoning: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Save an AI reasoning log for an exercise change.

    If *user_id* is provided, the log is scoped to that user.
    """
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO reasoning_logs (user_id, context_id, date, exercise_name, reasoning)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, context_id) DO UPDATE SET
                reasoning = excluded.reasoning
            """,
            (
                user_id,
                context_id,
                datetime.now(tz=timezone.utc).date().isoformat(),
                exercise_name,
                reasoning,
            ),
        )


def get_reasoning_log(
    context_id: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> str | None:
    """Get the reasoning log by context_id.

    If *user_id* is provided, results are scoped to that user.
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            row = conn.execute(
                "SELECT reasoning FROM reasoning_logs "
                "WHERE user_id = ? AND context_id = ?",
                (user_id, context_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT reasoning FROM reasoning_logs WHERE context_id = ?",
                (context_id,),
            ).fetchone()
    return row[0] if row else None


def save_deep_correlation(
    insight_markdown: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Save a deep correlation insight.

    If *user_id* is provided, the insight is scoped to that user.
    """
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO deep_correlations (user_id, date, insight_markdown)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                date = excluded.date,
                insight_markdown = excluded.insight_markdown
            """,
            (
                user_id,
                datetime.now(tz=timezone.utc).date().isoformat(),
                insight_markdown,
            ),
        )


def get_deep_correlation(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> str | None:
    """Return the latest deep correlation for *user_id*."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT insight_markdown FROM deep_correlations WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row[0] if row else None


def save_chat_message(
    role: str,
    content: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Persist a chat message (role is 'user' or 'assistant').

    If *user_id* is provided, the message is scoped to that user.
    """
    from datetime import datetime

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (role, content, created_at, user_id)
            VALUES (?, ?, ?, ?)
            """,
            (role, content, datetime.now(tz=timezone.utc).isoformat(), user_id),
        )


def get_chat_messages(
    limit: int = 50,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return chat messages, oldest first.

    If *user_id* is provided, results are scoped to that user.
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT role, content, created_at FROM (
                    SELECT role, content, created_at, id
                    FROM chat_messages
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT role, content, created_at FROM (
                    SELECT role, content, created_at, id
                    FROM chat_messages
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (limit,),
            ).fetchall()
    return [
        {"role": role, "content": content, "created_at": created_at}
        for role, content, created_at in rows
    ]


def clear_chat_messages(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Delete all chat messages for *user_id*."""
    with _connect(db_path) as conn:
        if user_id is not None:
            conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
        else:
            conn.execute("DELETE FROM chat_messages")


# ---------------------------------------------------------------------------
# Multi-user management (Sprint 1)
# ---------------------------------------------------------------------------


def get_or_create_user(
    email: str,
    display_name: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> UserRow:
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
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> UserRow | None:
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


def get_all_users(db_path: str = DEFAULT_DB_PATH) -> list[UserRow]:
    """Return all user rows, keyed by user_id."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, email, display_name, created_at, timezone, units FROM users",
        ).fetchall()
    return [
        {
            "id": row[0],
            "email": row[1],
            "display_name": row[2],
            "created_at": row[3],
            "timezone": row[4],
            "units": row[5],
        }
        for row in rows
    ]


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
    user_id: str,
    provider: str,
    db_path: str = DEFAULT_DB_PATH,
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
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
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
    user_id: str,
    provider: str,
    db_path: str = DEFAULT_DB_PATH,
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
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
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


# ---- Programme management ----


def _build_template_definition() -> dict[str, Any]:
    """Build the JSON definition of the default 'hybrid_powerbuilding' template from program.py."""
    from program import BLOCKS, COACHING_RULES, SPLIT_NAME, day_exercises, day_focus

    blocks: list[dict[str, Any]] = []
    for num in sorted(BLOCKS):
        block = BLOCKS[num]
        blocks.append(
            {
                "number": block.number,
                "name": block.name,
                "weeks": block.weeks,
                "focus": block.focus,
                "deadlift": {
                    "sets": block.deadlift.sets,
                    "rep_range": block.deadlift.rep_range,
                    "note": block.deadlift.note,
                    "template_id": block.deadlift.template_id,
                },
                "pullups": {
                    "sets": block.pullups.sets,
                    "rep_range": block.pullups.rep_range,
                    "note": block.pullups.note,
                    "template_id": block.pullups.template_id,
                },
                "accessory_emphasis": block.accessory_emphasis,
            },
        )

    days: list[dict[str, Any]] = []
    block1 = BLOCKS[1]
    for day_num in range(1, 7):
        exercises = []
        for ex in day_exercises(day_num, block1):
            exercises.append(
                {
                    "name": ex.name,
                    "sets": ex.sets,
                    "rep_range": ex.rep_range,
                    "note": ex.note,
                    "template_id": ex.template_id,
                },
            )
        days.append(
            {"number": day_num, "focus": day_focus(day_num), "exercises": exercises},
        )

    return {
        "name": SPLIT_NAME,
        "cycle_weeks": 12,
        "total_days": 6,
        "blocks": blocks,
        "days": days,
        "rules": COACHING_RULES,
    }


AVAILABLE_TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "hybrid_powerbuilding",
        "name": "Hybrid Powerbuilding",
        "description": (
            "A 12-week block-periodised 6-day split focused on deadlift and pull-up "
            "strength with bodybuilding accessories. Accumulation, Intensification, "
            "Peaking blocks with periodised main-lift intensity."
        ),
        "source": "template",
    },
    {
        "key": "infer_from_hevy",
        "name": "Infer from my Hevy history",
        "description": (
            "Analyse your existing Hevy routines and workout history to detect "
            "your real split (PPL, upper/lower, bro split, full body, custom), "
            "frequency, and muscle-group emphasis."
        ),
        "source": "inferred",
    },
]


def get_programme_templates() -> list[dict[str, Any]]:
    """Return the list of available programme templates."""
    return list(AVAILABLE_TEMPLATES)


def get_active_programme(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Return the currently active programme for a user, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, user_id, source, template_key, definition, active,
                   created_at, updated_at
            FROM programmes
            WHERE user_id = ? AND active = 1
            """,
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "source": row[2],
        "template_key": row[3],
        "definition": (json.loads(row[4]) if row[4] else {}) or {},
        "active": bool(row[5]),
        "created_at": row[6],
        "updated_at": row[7],
    }


def set_active_programme(
    user_id: str,
    source: str,
    template_key: str,
    definition: dict[str, Any] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Activate a programme for a user, deactivating any previous active one.

    For template selections (not 'inferred'), definition is auto-built from
    program.py if not provided.
    """
    now = datetime.now(tz=timezone.utc).isoformat()
    if definition is None:
        if source == "template":
            definition = _build_template_definition()
        else:
            definition = {}

    with _connect(db_path) as conn:
        # Deactivate all existing programmes for this user.
        conn.execute(
            "UPDATE programmes SET active = 0 WHERE user_id = ?",
            (user_id,),
        )
        conn.execute(
            """
            INSERT INTO programmes
                (user_id, source, template_key, definition, active,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id, template_key) DO UPDATE SET
                source = excluded.source,
                definition = excluded.definition,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                source,
                template_key,
                json.dumps(definition, default=str),
                now,
                now,
            ),
        )
        # Reset current_day to 1 for the new programme.

        conn.execute(
            "UPDATE programme_state SET current_day = 1 WHERE user_id = ?",
            (user_id,),
        )
