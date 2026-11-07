"""SQLite persistence for the workout agent.

Stores the current position in the 6-day cycle and a history of the raw Hevy
payloads so progress can be reviewed over time.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Collection, Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from program import SPLIT_NAME, TOTAL_DAYS

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from hevy_parser import WorkoutSummary


class UserRow(TypedDict):
    id: str
    email: str
    display_name: str | None
    created_at: str
    timezone: str
    units: str


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
            """,
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_state (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                current_day INTEGER NOT NULL DEFAULT 1,
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
        # Bolt: Index for faster grouping and filtering by exercise_name
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_progress_name_id ON exercise_progress(exercise_name, id)"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS hevy_routines (
                user_id TEXT NOT NULL REFERENCES users(id),
                routine_key TEXT NOT NULL,
                routine_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (user_id, routine_key)
            )
            """,
        )
        # Note: hevy_meta is migrated from key PK to (user_id, key) composite
        # PK below (see the migration block after user_preferences). In brand-new
        # databases the migration block creates it from scratch; for existing DBs
        # it was created above and will be migrated when the block runs.
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
                lifestyle TEXT NOT NULL,
                user_id TEXT REFERENCES users(id)
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
            """
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
            """,
        )

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_workout_history_date_id ON workout_history (date DESC, id DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_log_date_id ON daily_log (date DESC, id DESC)"
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

        # Performance optimizations
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_progress_name ON exercise_progress(exercise_name)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_progress_date ON exercise_progress(date)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_body_metrics_date ON body_metrics(date)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_workout_history_date ON workout_history(date)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_log_date ON daily_log(date)"
        )

        # Migration: Add hrv column to body_metrics if it doesn't exist
        cursor.execute("PRAGMA table_info(body_metrics)")
        columns = [col[1] for col in cursor.fetchall()]
        if "hrv" not in columns:
            cursor.execute("ALTER TABLE body_metrics ADD COLUMN hrv REAL")

        # Rate limiting table (replaces in-process dict for multi-replica safety).
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limits_ip_ts ON rate_limits (ip, timestamp)"
        )

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

        # ---- Account access policy (issue #753) ----
        # Additive, idempotent columns. ``status`` and ``session_epoch`` are
        # read on every authenticated request, so they stay on the primary-key
        # row. ``session_epoch`` is bumped to revoke every signed session that
        # was issued under an earlier value (see ``revoke_account_sessions``).
        cursor.execute("PRAGMA table_info(users)")
        user_columns = {col[1] for col in cursor.fetchall()}
        if "status" not in user_columns:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
        if "status_changed_at" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN status_changed_at TEXT")
        if "session_epoch" not in user_columns:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0"
            )
        # Operator-owned access-control records, not tenant data: an invitation
        # exists before the invited person has an account, so there is no owning
        # user_id. ``accepted_user_id`` links the single account it created.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS account_invitations (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                invited_by TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                accepted_at TEXT,
                accepted_user_id TEXT REFERENCES users(id),
                revoked_at TEXT
            )
            """,
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_account_invitations_pending_email "
            "ON account_invitations (email) WHERE status = 'pending'"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_invitations_email_status "
            "ON account_invitations (email, status, expires_at)"
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
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                endpoint TEXT NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user "
            "ON push_subscriptions (user_id)"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                link TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user_created "
            "ON notifications (user_id, created_at DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user_unread "
            "ON notifications (user_id, is_read)"
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

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programmes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                source TEXT NOT NULL,
                template_key TEXT,
                definition TEXT,
                active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, template_key)
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programmes_user_active "
            "ON programmes (user_id, active)"
        )

        # ---- Multi-tenant migration: Add user_id to domain tables ----
        cursor.execute("PRAGMA table_info(daily_log)")
        dl_columns = {col[1] for col in cursor.fetchall()}
        if "user_id" not in dl_columns:
            cursor.execute(
                "ALTER TABLE daily_log ADD COLUMN user_id TEXT REFERENCES users(id)"
            )
        else:
            dl_foreign_keys = cursor.execute(
                "PRAGMA foreign_key_list(daily_log)"
            ).fetchall()
            has_user_foreign_key = any(
                row[2] == "users" and row[3] == "user_id" and row[4] == "id"
                for row in dl_foreign_keys
            )
            if not has_user_foreign_key:
                # Databases created by the previous schema already have user_id,
                # but the column did not declare its users(id) reference. Rebuild
                # the table so upgrades receive the same constraint as fresh DBs.
                cursor.execute("DROP TABLE IF EXISTS daily_log_new")
                cursor.execute(
                    """
                    CREATE TABLE daily_log_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date TEXT NOT NULL,
                        day INTEGER,
                        focus TEXT NOT NULL,
                        carb_tier TEXT NOT NULL,
                        plan TEXT NOT NULL,
                        lifestyle TEXT NOT NULL,
                        user_id TEXT REFERENCES users(id)
                    )
                    """,
                )
                cursor.execute(
                    """
                    INSERT INTO daily_log_new
                        (id, date, day, focus, carb_tier, plan, lifestyle, user_id)
                    SELECT id, date, day, focus, carb_tier, plan, lifestyle, user_id
                    FROM daily_log
                    """,
                )
                cursor.execute("DROP TABLE daily_log")
                cursor.execute("ALTER TABLE daily_log_new RENAME TO daily_log")

        # Backfill legacy rows with a stable legacy tenant.
        legacy = _get_or_create_legacy_user(cursor)
        cursor.execute(
            "UPDATE daily_log SET user_id = ? WHERE user_id IS NULL",
            (legacy,),
        )

        # Composite indexes for user-scoped queries.
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_log_user_date "
            "ON daily_log (user_id, date DESC, id DESC)"
        )
        # Recreate the date-only indexes after the legacy-table rebuild. SQLite
        # drops indexes owned by the replaced table, and these are initially
        # created before the migration block above.
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_log_date_id "
            "ON daily_log (date DESC, id DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_log_date ON daily_log (date)"
        )

        cursor.execute(
            """
            INSERT OR IGNORE INTO hevy_meta (key, value)
            VALUES ('programme_start_date', ?)
            """,
            (datetime.now(tz=timezone.utc).date().isoformat(),),
        )

        # Migration: Add user_id column to workout_history for multi-tenancy
        cursor.execute("PRAGMA table_info(workout_history)")
        woh_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in woh_columns:
            cursor.execute(
                "ALTER TABLE workout_history ADD COLUMN user_id TEXT REFERENCES users(id)"
            )
        cursor.execute(
            "UPDATE workout_history SET user_id = ? WHERE user_id IS NULL",
            (legacy,),
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
            (legacy,),
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
            (legacy,),
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

        # Archive any previously active static template. Existing history stays
        # intact, but the user must explicitly activate a Hevy-native programme.
        cursor.execute(
            """
            UPDATE programmes
            SET active = 0,
                source = 'legacy_static',
                updated_at = ?
            WHERE active = 1
              AND (source = 'template' OR template_key = 'hybrid_powerbuilding')
            """,
            (datetime.now(tz=timezone.utc).isoformat(),),
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
        cursor.execute(
            "UPDATE chat_messages SET user_id = ? WHERE user_id IS NULL",
            (legacy,),
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



        # Migration: Add user_id column to check_ins for multi-tenancy
        cursor.execute("PRAGMA table_info(check_ins)")
        ci_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in ci_columns:
            cursor.execute(
                "ALTER TABLE check_ins ADD COLUMN user_id TEXT REFERENCES users(id)",
            )
        cursor.execute(
            "UPDATE check_ins SET user_id = ? WHERE user_id IS NULL",
            (legacy,),
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_check_ins_user "
            "ON check_ins (user_id, id DESC)",
        )

        # Migration: Migrate programme_state from singleton to user_id-scoped
        cursor.execute("PRAGMA table_info(programme_state)")
        ps_columns = {row[1] for row in cursor.fetchall()}
        if "id" in ps_columns:
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
        cursor.execute(
            "INSERT OR IGNORE INTO programme_state "
            "(user_id, current_day, split_name) VALUES (?, 1, ?)",
            (legacy, SPLIT_NAME),
        )

        # Migration: Migrate hevy_meta from key PK to (user_id, key) composite PK
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
            if old_rows:
                hm_legacy_id = _ensure_legacy_user(cursor)
                for row in old_rows:
                    cursor.execute(
                        "INSERT OR REPLACE INTO hevy_meta_new "
                        "(user_id, key, value) VALUES (?, ?, ?)",
                        (hm_legacy_id, row[0], row[1]),
                    )
            else:
                # Brand-new database: seed programme_start_date for legacy user
                hm_legacy_id = _ensure_legacy_user(cursor)
                cursor.execute(
                    "INSERT OR REPLACE INTO hevy_meta_new (user_id, key, value) "
                    "VALUES (?, 'programme_start_date', ?)",
                    (hm_legacy_id, datetime.now(tz=timezone.utc).date().isoformat()),
                )
            cursor.execute("DROP TABLE hevy_meta")
            cursor.execute("ALTER TABLE hevy_meta_new RENAME TO hevy_meta")

        cursor.execute(
            "INSERT OR IGNORE INTO hevy_meta (user_id, key, value) "
            "VALUES (?, 'programme_start_date', ?)",
            (legacy, datetime.now(tz=timezone.utc).date().isoformat()),
        )

        # Migration: Migrate hevy_routines to a tenant-scoped composite key.
        cursor.execute("PRAGMA table_info(hevy_routines)")
        hr_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in hr_columns:
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
            for row in old_rows:
                cursor.execute(
                    "INSERT OR REPLACE INTO hevy_routines_new "
                    "(user_id, routine_key, routine_id, content_hash) "
                    "VALUES (?, ?, ?, ?)",
                    (legacy, row[0], row[1], row[2]),
                )
            cursor.execute("DROP TABLE hevy_routines")
            cursor.execute(
                "ALTER TABLE hevy_routines_new RENAME TO hevy_routines",
            )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_hevy_routines_user "
            "ON hevy_routines (user_id, routine_key)",
        )

        # Ensure every user_id scoped table has an explicit idx_<table_name>_user
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_dashboard_insights_user "
            "ON dashboard_insights (user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_deep_correlations_user "
            "ON deep_correlations (user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programme_state_user "
            "ON programme_state (user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_hevy_meta_user "
            "ON hevy_meta (user_id, key)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_preferences_user "
            "ON user_preferences (user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_api_keys_user "
            "ON user_api_keys (user_id)"
        )
        _init_credential_vault_schema(cursor)

        # Server-side registry behind the browser session cookie (issue #752).
        ensure_user_sessions_schema(conn)


def _init_credential_vault_schema(cursor: sqlite3.Cursor) -> None:
    """Add the additive credential-vault lifecycle columns and audit trail.

    ``secret_vault`` is the only module that reads or writes these tables; this
    function owns only their schema so the whole database still migrates through
    ``init_db``. Every statement is idempotent: columns are guarded by
    ``PRAGMA table_info``, and the table, indexes and triggers use
    ``IF NOT EXISTS``. Existing credential rows keep working unchanged and are
    upgraded to per-user encryption by ``secret_vault.migrate_legacy_credentials``.
    """
    cursor.execute("PRAGMA table_info(user_api_keys)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    lifecycle_columns = (
        ("status", "ALTER TABLE user_api_keys ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
        ("key_hint", "ALTER TABLE user_api_keys ADD COLUMN key_hint TEXT"),
        ("key_version", "ALTER TABLE user_api_keys ADD COLUMN key_version INTEGER NOT NULL DEFAULT 1"),
        ("rotated_at", "ALTER TABLE user_api_keys ADD COLUMN rotated_at TEXT"),
        ("revoked_at", "ALTER TABLE user_api_keys ADD COLUMN revoked_at TEXT"),
        ("last_tested_at", "ALTER TABLE user_api_keys ADD COLUMN last_tested_at TEXT"),
        ("last_test_status", "ALTER TABLE user_api_keys ADD COLUMN last_test_status TEXT"),
    )
    for column, statement in lifecycle_columns:
        if column not in existing_columns:
            cursor.execute(statement)

    # A credential can never be re-parented to another tenant.
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_user_api_keys_user_immutable
        BEFORE UPDATE OF user_id ON user_api_keys
        WHEN NEW.user_id IS NOT OLD.user_id
        BEGIN
            SELECT RAISE(ABORT, 'user_api_keys.user_id is immutable');
        END
        """
    )

    # Append-only audit trail of credential lifecycle operations. It records who
    # did what to which provider credential and the outcome, never secret
    # material or provider error text.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS credential_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES users(id),
            provider TEXT NOT NULL,
            action TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT,
            actor TEXT NOT NULL,
            source TEXT NOT NULL,
            credential_version INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_credential_audit_events_user "
        "ON credential_audit_events (user_id, created_at DESC, id DESC)"
    )
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_credential_audit_events_append_only
        BEFORE UPDATE ON credential_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'credential audit events are append-only');
        END
        """
    )


def ensure_user_sessions_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the tenant-scoped browser-session registry.

    The registry is what makes a session cookie revocable and bounded: the
    signed cookie only proves *who it claims to be*, while a row here proves the
    server still honours that session. Only a SHA-256 digest of the random
    session identifier is stored (never the identifier itself), every row is
    owned by exactly one user, and ``user_id`` is the leading index column so
    "revoke all of this user's sessions" never scans other tenants.

    ``webapp.session_security.SessionStore`` also calls this on connect so the
    table exists even when a request arrives before ``init_db`` has run.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            session_token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            revoked_at INTEGER,
            revoked_reason TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_user "
        "ON user_sessions (user_id, revoked_at)"
    )


def _get_or_create_legacy_user(cursor: sqlite3.Cursor) -> str:
    """Return the id of a stable legacy tenant used for pre-migration data."""
    import uuid

    row = cursor.execute(
        "SELECT id FROM users WHERE email = ?", ("legacy@local",)
    ).fetchone()
    if row:
        return row[0]

    user_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc).isoformat()
    cursor.execute(
        "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
        (user_id, "legacy@local", "Legacy Data", now),
    )
    return user_id


def get_legacy_user_id(db_path: str = DEFAULT_DB_PATH) -> str:
    """Return the id of the legacy tenant user."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email = ?", ("legacy@local",)
        ).fetchone()
    if not row:
        raise RuntimeError("Legacy user not found — run init_db first.")
    return row[0]


def get_current_day(
    db_path: str = DEFAULT_DB_PATH, *, user_id: str | None = None
) -> int:
    """Return the current day in the cycle (1-6)."""
    uid = user_id or get_legacy_user_id(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT current_day FROM programme_state WHERE user_id = ?",
            (uid,),
        ).fetchone()
    return int(row[0]) if row else 1


def advance_day(db_path: str = DEFAULT_DB_PATH, *, user_id: str | None = None) -> int:
    """Move to the next day, wrapping from TOTAL_DAYS back to 1."""
    uid = user_id or get_legacy_user_id(db_path)
    current = get_current_day(db_path, user_id=uid)
    nxt = current + 1 if current < TOTAL_DAYS else 1
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO programme_state (user_id, current_day, split_name) "
            "VALUES (?, ?, COALESCE((SELECT split_name FROM programme_state WHERE user_id = ?), ?))",
            (uid, nxt, uid, SPLIT_NAME),
        )
    return nxt


def save_workout(
    payload: Any,
    db_path: str = DEFAULT_DB_PATH,
    when: str | None = None,
    *,
    user_id: str | None = None,
) -> None:
    """Persist a raw Hevy payload for historical reference."""
    if payload is None:
        return
    today = when or datetime.now(tz=timezone.utc).date().isoformat()
    uid = user_id or get_legacy_user_id(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO workout_history (date, hevy_payload, user_id) "
            "VALUES (?, ?, ?)",
            (today, json.dumps(payload), uid),
        )


def get_recent_hevy_logs(
    limit: int = 14, db_path: str = DEFAULT_DB_PATH, *, user_id: str | None = None
) -> list[dict[str, Any]]:
    """Return recent raw Hevy payloads for autonomous analysis."""
    uid = user_id or get_legacy_user_id(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT hevy_payload FROM workout_history WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT ?",
            (
                uid,
                limit,
            ),
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
        except Exception:  # noqa: BLE001
            logger.debug("Skipping unparseable log row")
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
    uid = user_id or get_legacy_user_id(db_path)
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
                    uid,
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

    When *user_id* is provided, the lookup is scoped to that user.
    When None, returns a legacy match (first row found)."""
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

    When *user_id* is provided, the value is scoped to that user.
    When None, falls back to the legacy user."""
    with _connect(db_path) as conn:
        uid = user_id
        if uid is None:
            row = conn.execute(
                "SELECT id FROM users WHERE email = ?",
                ("legacy@local",),
            ).fetchone()
            if row:
                uid = row[0]
            else:
                from uuid import uuid4

                uid = str(uuid4())
                conn.execute(
                    "INSERT INTO users (id, email, display_name, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        uid,
                        "legacy@local",
                        "Legacy Data",
                        datetime.now(tz=timezone.utc).isoformat(),
                    ),
                )
        conn.execute(
            """
            INSERT INTO hevy_meta (user_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (uid, key, value),
        )


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

    When *user_id* is provided, the lookup is scoped to that user.
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
    user_id: str | None = None,
) -> None:
    """Log the full plan and lifestyle guidance issued for a day.

    One row per date per user: a re-run on the same day replaces the earlier
    entry so the log always holds the latest guidance that was sent.

    If *user_id* is provided, the log is scoped to that user.
    """
    uid = user_id or get_legacy_user_id(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM daily_log WHERE date = ? AND user_id = ?", (when, uid)
        )
        conn.execute(
            """
            INSERT INTO daily_log (date, day, focus, carb_tier, plan, lifestyle, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (when, day, focus, carb_tier, plan, lifestyle, uid),
        )


def get_daily_logs(
    limit: int = 30,
    db_path: str = DEFAULT_DB_PATH,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent daily logs, most recent first."""
    uid = user_id or get_legacy_user_id(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT date, day, focus, carb_tier, plan, lifestyle
            FROM daily_log
            WHERE user_id = ?
            ORDER BY date DESC, id DESC
            LIMIT ?
            """,
            (uid, limit),
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
        cursor = conn.cursor()
        uid = user_id or _get_or_create_legacy_user(cursor)
        conn.execute(
            """
            INSERT INTO dashboard_insights (user_id, date, insight_json)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                date = excluded.date,
                insight_json = excluded.insight_json
            """,
            (uid, datetime.now(tz=timezone.utc).date().isoformat(), insight_json),
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
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(dashboard_insights)")
        cols = {r[1] for r in cursor.fetchall()}
        if "user_id" in cols:
            if user_id is not None:
                row = conn.execute(
                    "SELECT insight_json FROM dashboard_insights WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            else:
                row = None
        else:
            row = conn.execute(
                "SELECT insight_json FROM dashboard_insights WHERE id = 1"
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
        cursor = conn.cursor()
        uid = user_id or _get_or_create_legacy_user(cursor)
        cursor.execute(
            """
            INSERT INTO reasoning_logs (user_id, context_id, date, exercise_name, reasoning)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, context_id) DO UPDATE SET
                reasoning = excluded.reasoning
            """,
            (
                uid,
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
                """
                SELECT reasoning FROM reasoning_logs
                WHERE context_id = ? AND user_id = ?
                """,
                (context_id, user_id),
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
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        uid = user_id or _get_or_create_legacy_user(cursor)
        conn.execute(
            """
            INSERT INTO deep_correlations (user_id, date, insight_markdown)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                date = excluded.date,
                insight_markdown = excluded.insight_markdown
            """,
            (uid, datetime.now(tz=timezone.utc).date().isoformat(), insight_markdown),
        )


def get_deep_correlation(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> str | None:
    """Return the latest deep correlation for *user_id*."""
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(deep_correlations)")
        cols = {r[1] for r in cursor.fetchall()}
        if "user_id" in cols:
            if user_id is not None:
                row = conn.execute(
                    "SELECT insight_markdown FROM deep_correlations WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            else:
                row = None
        else:
            row = conn.execute(
                "SELECT insight_markdown FROM deep_correlations WHERE id = 1"
            ).fetchone()
    return row[0] if row else None


def save_chat_message(
    role: str,
    content: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> None:
    """Persist a chat message (role is 'user' or 'assistant')."""
    from datetime import datetime, timezone

    uid = user_id or get_legacy_user_id(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (role, content, created_at, user_id)
            VALUES (?, ?, ?, ?)
            """,
            (role, content, datetime.now(tz=timezone.utc).isoformat(), uid),
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
    from datetime import datetime, timezone

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, email, display_name, created_at, timezone, units "
            "FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if row:
            return UserRow(
                id=row[0],
                email=row[1],
                display_name=row[2],
                created_at=row[3],
                timezone=row[4],
                units=row[5],
            )

        user_id = str(uuid.uuid4())
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO users (id, email, display_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, email, display_name, now),
        )
        return UserRow(
            id=user_id,
            email=email,
            display_name=display_name,
            created_at=now,
            timezone="UTC",
            units="metric",
        )


def get_user_by_id(user_id: str, db_path: str = DEFAULT_DB_PATH) -> UserRow | None:
    """Return the user row for a user_id, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, email, display_name, created_at, timezone, units "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return UserRow(
        id=row[0],
        email=row[1],
        display_name=row[2],
        created_at=row[3],
        timezone=row[4],
        units=row[5],
    )


def get_all_users(db_path: str = DEFAULT_DB_PATH) -> list[UserRow]:
    """Return all user rows, keyed by user_id."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, email, display_name, created_at, timezone, units FROM users"
        ).fetchall()
    return [
        UserRow(
            id=row[0],
            email=row[1],
            display_name=row[2],
            created_at=row[3],
            timezone=row[4],
            units=row[5],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Account access policy (issue #753)
#
# These functions are the persistence half of ``account_policy``: they read and
# change the server-side account state that login completion and every
# authenticated request are checked against. They never trust a client-supplied
# identifier; callers pass an id taken from a verified session or an operator.
# ---------------------------------------------------------------------------

ACCOUNT_STATUS_ACTIVE = "active"
ACCOUNT_STATUS_SUSPENDED = "suspended"
ACCOUNT_STATUS_DELETED = "deleted"
ACCOUNT_STATUSES = frozenset(
    {ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_SUSPENDED, ACCOUNT_STATUS_DELETED}
)

# Email of the pre-tenant data owner seeded by ``_get_or_create_legacy_user``.
# It is an artefact of the single-user era, not a person, and holds no browser
# session; policy bookkeeping that counts "accounts" skips it.
LEGACY_TENANT_EMAIL = "legacy@local"

INVITATION_PENDING = "pending"
INVITATION_ACCEPTED = "accepted"
INVITATION_REVOKED = "revoked"

ADMIT_EXISTING = "existing"
ADMIT_CREATED = "created"
ADMIT_SUSPENDED = "suspended"
ADMIT_DELETED = "deleted"
ADMIT_INVALID_STATUS = "invalid_status"
ADMIT_NOT_INVITED = "not_invited"


class AccountState(TypedDict):
    """The minimal server-side facts an access decision is made from."""

    id: str
    email: str
    status: str
    session_epoch: int


class AccountInvitation(TypedDict):
    """One invitation record.

    ``status`` and the two timestamps that always exist are plain strings; the
    acceptance and revocation timestamps are ``None`` until they happen.
    """

    status: str
    created_at: str
    expires_at: str
    accepted_at: str | None
    revoked_at: str | None


class AccountTransitionError(ValueError):
    """Raised when an account status change is not permitted."""


def _utc_seconds(moment: datetime | None) -> str:
    """Return a timezone-aware moment as a sortable UTC ISO-8601 string."""
    if moment is None:
        moment = datetime.now(tz=timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("A timezone-aware datetime is required.")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _account_state(row: Any) -> AccountState:
    return AccountState(
        id=str(row[0]),
        email=str(row[1]),
        status=str(row[2]),
        session_epoch=int(row[3]),
    )


def _find_account_row(conn: sqlite3.Connection, email: str) -> Any:
    """Find an account by canonical email, tolerating legacy mixed-case rows.

    An exact match wins so an existing account keeps resolving to the same
    tenant it always did; otherwise match case-insensitively so a differently
    cased address cannot fork a second account for the same mailbox.
    """
    row = conn.execute(
        "SELECT id, email, status, session_epoch FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT id, email, status, session_epoch FROM users "
        "WHERE lower(email) = ? ORDER BY created_at, id LIMIT 1",
        (email,),
    ).fetchone()


def get_account_state(
    user_id: str, db_path: str = DEFAULT_DB_PATH
) -> AccountState | None:
    """Return the access-relevant state of one account, or None if unknown."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, email, status, session_epoch FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _account_state(row) if row is not None else None


def find_account_state(
    email: str, db_path: str = DEFAULT_DB_PATH
) -> AccountState | None:
    """Return the access-relevant state for a canonical email, or None."""
    with _connect(db_path) as conn:
        row = _find_account_row(conn, email)
    return _account_state(row) if row is not None else None


def admit_account(
    email: str,
    *,
    display_name: str | None,
    require_invitation: bool,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> tuple[str, AccountState | None]:
    """Resolve, and if permitted create, the account for a verified login.

    Everything happens in one ``BEGIN IMMEDIATE`` transaction so concurrent or
    retried callbacks cannot create two accounts or spend one invitation twice:

    * an existing account is returned with its current status and is never
      modified, whatever the status;
    * a missing account is created only when no invitation is required, or when
      exactly one pending, unexpired invitation for the address is consumed in
      the same transaction.

    ``email`` must already be canonical (see ``account_policy.canonical_email``).
    """
    moment = _utc_seconds(now)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _find_account_row(conn, email)
        if row is not None:
            state = _account_state(row)
            outcome = {
                ACCOUNT_STATUS_ACTIVE: ADMIT_EXISTING,
                ACCOUNT_STATUS_SUSPENDED: ADMIT_SUSPENDED,
                ACCOUNT_STATUS_DELETED: ADMIT_DELETED,
            }.get(state["status"], ADMIT_INVALID_STATUS)
            return outcome, state

        invitation_id: str | None = None
        if require_invitation:
            invitation = conn.execute(
                "SELECT id FROM account_invitations "
                "WHERE email = ? AND status = ? AND expires_at > ? "
                "ORDER BY created_at DESC, id LIMIT 1",
                (email, INVITATION_PENDING, moment),
            ).fetchone()
            if invitation is None:
                return ADMIT_NOT_INVITED, None
            invitation_id = str(invitation[0])

        user_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, email, display_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, email, display_name, datetime.now(tz=timezone.utc).isoformat()),
        )
        if invitation_id is not None:
            consumed = conn.execute(
                "UPDATE account_invitations "
                "SET status = ?, accepted_at = ?, accepted_user_id = ? "
                "WHERE id = ? AND status = ?",
                (INVITATION_ACCEPTED, moment, user_id, invitation_id, INVITATION_PENDING),
            )
            if consumed.rowcount != 1:
                # Unreachable under BEGIN IMMEDIATE; fail closed and roll back
                # the account insert rather than admit without the invitation.
                raise sqlite3.IntegrityError("invitation was not consumable")
        state = AccountState(
            id=user_id,
            email=email,
            status=ACCOUNT_STATUS_ACTIVE,
            session_epoch=0,
        )
    return ADMIT_CREATED, state


def set_account_status(
    user_id: str,
    status: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> AccountState | None:
    """Set an account's status and revoke every session issued before now.

    The session epoch is bumped on *every* call, including one that re-asserts
    the current status: a session issued before a suspension must never become
    valid again because the account was later reactivated. ``deleted`` is
    terminal. Returns the new state, or None if the account does not exist.
    """
    if status not in ACCOUNT_STATUSES:
        raise ValueError(f"Unsupported account status: {status!r}")
    moment = _utc_seconds(now)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, email, status, session_epoch FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        current = _account_state(row)
        if current["status"] == ACCOUNT_STATUS_DELETED and status != ACCOUNT_STATUS_DELETED:
            raise AccountTransitionError("A deleted account cannot be restored.")
        conn.execute(
            "UPDATE users SET status = ?, status_changed_at = ?, "
            "session_epoch = session_epoch + 1 WHERE id = ?",
            (status, moment, user_id),
        )
        updated = conn.execute(
            "SELECT id, email, status, session_epoch FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _account_state(updated)


def revoke_account_sessions(
    user_id: str, db_path: str = DEFAULT_DB_PATH
) -> AccountState | None:
    """Invalidate every signed session issued for an account before now."""
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE users SET session_epoch = session_epoch + 1 WHERE id = ?",
            (user_id,),
        )
        row = conn.execute(
            "SELECT id, email, status, session_epoch FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _account_state(row) if row is not None else None


def revoke_sessions_outside_allowlist(
    allowed_emails: Collection[str], db_path: str = DEFAULT_DB_PATH
) -> int:
    """Revoke sessions of every account whose email is not allowlisted.

    Called at startup when an allowlist is configured so that removing an
    address from ``ALLOWED_EMAILS`` is a durable revocation: re-adding the
    address later cannot resurrect a cookie issued before the removal. The
    pre-tenant ``legacy@local`` owner is not a person and is skipped. Returns the
    number of accounts whose epoch was advanced.
    """
    allowed = {email.strip().lower() for email in allowed_emails}
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id, email FROM users").fetchall()
        excluded = [
            (str(user_id),)
            for user_id, email in rows
            if str(email).strip().lower() not in allowed
            and str(email).strip().lower() != LEGACY_TENANT_EMAIL
        ]
        conn.executemany(
            "UPDATE users SET session_epoch = session_epoch + 1 WHERE id = ?",
            excluded,
        )
    return len(excluded)


def create_account_invitation(
    email: str,
    *,
    expires_at: datetime,
    invited_by: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> str:
    """Create a pending invitation, superseding any earlier one for the email.

    ``email`` must already be canonical. Returns the new invitation id.
    """
    created = _utc_seconds(now)
    expiry = _utc_seconds(expires_at)
    if expiry <= created:
        raise ValueError("An invitation must expire in the future.")
    invitation_id = str(uuid.uuid4())
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE account_invitations SET status = ?, revoked_at = ? "
            "WHERE email = ? AND status = ?",
            (INVITATION_REVOKED, created, email, INVITATION_PENDING),
        )
        conn.execute(
            "INSERT INTO account_invitations "
            "(id, email, status, invited_by, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (invitation_id, email, INVITATION_PENDING, invited_by, created, expiry),
        )
    return invitation_id


def revoke_account_invitations(
    email: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> int:
    """Revoke every pending invitation for a canonical email."""
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE account_invitations SET status = ?, revoked_at = ? "
            "WHERE email = ? AND status = ?",
            (INVITATION_REVOKED, _utc_seconds(now), email, INVITATION_PENDING),
        )
    return cursor.rowcount


def list_account_invitations(
    email: str, db_path: str = DEFAULT_DB_PATH
) -> list[AccountInvitation]:
    """Return the invitation history for a canonical email, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, created_at, expires_at, accepted_at, revoked_at "
            "FROM account_invitations WHERE email = ? "
            "ORDER BY created_at DESC, id",
            (email,),
        ).fetchall()
    return [
        {
            "status": row[0],
            "created_at": row[1],
            "expires_at": row[2],
            "accepted_at": row[3],
            "revoked_at": row[4],
        }
        for row in rows
    ]


# ---- Credentials ----
#
# Per-user credentials (AI provider keys, connector API keys and OAuth tokens)
# are stored in ``user_api_keys`` but are intentionally NOT readable or writable
# from this module. ``secret_vault.SecretVault`` is the only supported interface:
# it encrypts under the owning user, audits every lifecycle operation and never
# returns plaintext to the browser. This module only owns the schema
# (see ``_init_credential_vault_schema``).


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
    from datetime import datetime, timezone

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


def save_push_subscription(
    user_id: str, endpoint: str, p256dh: str, auth: str, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Save a push subscription for a user."""
    now = datetime.now(tz=timezone.utc).isoformat()
    with _connect(db_path) as conn:
        # Avoid duplicate endpoints for the same user
        conn.execute(
            """
            DELETE FROM push_subscriptions
            WHERE user_id = ? AND endpoint = ?
            """,
            (user_id, endpoint),
        )
        conn.execute(
            """
            INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, endpoint, p256dh, auth, now),
        )


def get_push_subscriptions(
    user_id: str, db_path: str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Get all push subscriptions for a user."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT endpoint, p256dh, auth
            FROM push_subscriptions
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
    return [
        {"endpoint": row[0], "keys": {"p256dh": row[1], "auth": row[2]}} for row in rows
    ]


def delete_push_subscription(
    user_id: str, endpoint: str, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Delete a specific push subscription."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
            (user_id, endpoint),
        )


# ---------------------------------------------------------------------------
# Programme management & templates
# ---------------------------------------------------------------------------


def get_programme_templates() -> list[dict[str, Any]]:
    """Deprecated compatibility shim.

    Programme creation is exclusively Hevy-native.  Returning an empty list
    prevents older clients from rendering or activating built-in templates
    while they migrate to the preview/activate contract.
    """

    return []


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
        "definition": json.loads(row[4]) if row[4] else {},
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
    """Activate a programme for a user, deactivating any previous active one."""
    now = datetime.now(tz=timezone.utc).isoformat()
    if source == "template":
        raise ValueError(
            "Static programme templates have been removed; activate a Hevy-derived definition."
        )
    if not isinstance(definition, dict) or not definition:
        raise ValueError("A non-empty Hevy-derived programme definition is required.")

    with _connect(db_path) as conn:
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


def save_notification(
    user_id: str,
    title: str,
    message: str,
    type: str = "system",
    link: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Save an in-app notification for a user.

    Returns the inserted notification id.
    """
    now = datetime.now(tz=timezone.utc).isoformat()
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO notifications (user_id, type, title, message, link, is_read, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (user_id, type, title, message, link, now),
        )
        notification_id = cursor.lastrowid
        if notification_id is None:
            raise RuntimeError("Failed to create notification")
        return notification_id


def get_notifications(
    user_id: str,
    limit: int = 50,
    unread_only: bool = False,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Retrieve notifications for a user, ordered from newest to oldest."""
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        if unread_only:
            cursor.execute(
                """
                SELECT id, user_id, type, title, message, link, is_read, created_at
                FROM notifications
                WHERE user_id = ? AND is_read = 0
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
        else:
            cursor.execute(
                """
                SELECT id, user_id, type, title, message, link, is_read, created_at
                FROM notifications
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
        rows = cursor.fetchall()
        return [
            {
                "id": row[0],
                "user_id": row[1],
                "type": row[2],
                "title": row[3],
                "message": row[4],
                "link": row[5],
                "is_read": bool(row[6]),
                "created_at": row[7],
            }
            for row in rows
        ]


def mark_notification_read(
    notification_id: int,
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Mark a specific notification as read."""
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        )
        return cursor.rowcount > 0


def mark_all_notifications_read(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Mark all notifications as read for a given user. Returns count updated."""
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0",
            (user_id,),
        )
        return cursor.rowcount


def delete_notification(
    notification_id: int,
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Delete a single notification."""
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM notifications WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        )
        return cursor.rowcount > 0


def clear_all_notifications(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Delete all notifications for a user. Returns count deleted."""
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM notifications WHERE user_id = ?",
            (user_id,),
        )
        return cursor.rowcount


def get_unread_notification_count(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Count unread notifications for a user."""
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
            (user_id,),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0
