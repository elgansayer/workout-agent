"""SQLite persistence for the workout agent.

Stores tenant-scoped programme revisions, workout history, progress and the
supporting coaching records so progress can be reviewed over time.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import sqlite3
import uuid
from collections.abc import Collection, Iterable, Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import encryption
import provenance_ledger as ledger
from canonical_programme import (
    CANONICAL_SPLIT_NAME,
    CANONICAL_TEMPLATE_KEY,
    get_canonical_programme_definition,
)
from hevy_parser import HEVY_PARSER_VERSION, workout_identity
from localisation import derive_local_day, local_today, resolve_timezone
from provenance import (
    MANUAL_SOURCE,
    UNATTRIBUTED_SOURCE,
    Provenance,
    RecordKind,
    RecordType,
    content_hash,
    unattributed,
)
from security_audit import ensure_schema as ensure_security_audit_schema
from tenancy import (
    LEGACY_TENANT_EMAIL,
    TenantRequiredError,
    implicit_single_user_allowed,
    normalise_email,
)
from training_rules import StructuredTrainingRules

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from hevy_parser import WorkoutSummary


class UserRow(TypedDict):
    """A production account.

    ``id`` and ``created_at`` are immutable (enforced by database triggers).
    ``status`` is one of ``ACCOUNT_STATUSES``; only ``active`` accounts may
    authenticate or be scheduled.
    """

    id: str
    email: str
    display_name: str | None
    created_at: str
    timezone: str
    units: str
    status: str
    status_changed_at: str
    updated_at: str
    last_login_at: str | None


DEFAULT_DB_PATH = "workout_agent.db"

# Legacy rotation length retained only for archived pre-domain state.
LEGACY_ROUTINE_DAYS = 6

# Account lifecycle vocabulary. ``accounts.AccountStatus`` mirrors this tuple and
# ``test_user_model`` asserts the CHECK constraint, the enum and the terminal
# set below stay in agreement.
ACCOUNT_STATUSES = ("active", "suspended", "unclaimed", "retired", "deleted")
# Statuses an account can never leave (enforced by trigger).
TERMINAL_ACCOUNT_STATUSES = ("retired", "deleted")
# Both tuples are code constants (never user input), so rendering them into DDL
# is safe.
_ACCOUNT_STATUS_SQL = ", ".join(f"'{status}'" for status in ACCOUNT_STATUSES)
_TERMINAL_STATUS_SQL = ", ".join(f"'{status}'" for status in TERMINAL_ACCOUNT_STATUSES)

_USER_COLUMNS = (
    "id, email, display_name, created_at, timezone, units, status, "
    "status_changed_at, updated_at, last_login_at"
)

# Every table whose rows belong to exactly one account through ``user_id``.
# ``init_db`` installs the ownerless-write guards on these tables and
# ``legacy_ownership.OWNED_TABLES`` must cover the same set (a drift test asserts
# both, and the live assignment refuses to run if the database holds a
# ``user_id`` table that neither knows about). Names are code constants, never
# user input, so they are safe to render into DDL.
USER_OWNED_TABLES = (
    "workout_history",
    "exercise_progress",
    "body_metrics",
    "daily_log",
    "check_ins",
    "chat_messages",
    "notifications",
    "programme_state",
    "hevy_meta",
    "hevy_routines",
    "reasoning_logs",
    "dashboard_insights",
    "deep_correlations",
    "user_preferences",
    "user_api_keys",
    "push_subscriptions",
    "programmes",
    "programme_revisions",
    "programme_routines",
    "programme_blocks",
    "exercise_prescriptions",
    "programme_week_states",
    "adaptation_decisions",
    "routine_workout_matches",
    "hevy_routine_snapshots",
    "routine_match_overrides",
    "exercise_history_baselines",
    "programme_decisions",
    "hevy_connections",
    "training_rules",
    "health_connections",
    "health_metrics",
    "health_sync_runs",
)

# Tables bound to a real account that never existed in the single-user
# deployment, so the legacy ownership assignment never adopts them (see
# ``legacy_ownership.NOT_TRANSFERRED_TABLES``). Their ``user_id`` is already
# ``NOT NULL`` and user-first indexed; ``init_db`` still gives them the owner
# existence and immutability guards. Deletion-ledger and audit tables are left
# out on purpose: they must outlive, or describe attempts without, an account.
ACCOUNT_BOUND_TABLES = (
    "user_sessions",
    "credential_audit_events",
    "record_provenance",
)

# Account-bound tables whose rows are never updated (their own trigger aborts
# every UPDATE), so they need only the insert-time existence guard.
APPEND_ONLY_OWNED_TABLES = ("credential_audit_events", "record_provenance")

# References between owner-scoped tables that the schema does not declare as
# foreign keys, as ``(child, column, parent, parent_column)``. Declared foreign
# keys between owned tables are discovered from the live schema, so a new
# relationship is covered without editing this list.
_UNDECLARED_OWNED_REFERENCES = (
    ("programmes", "current_revision_id", "programme_revisions", "id"),
    ("routine_workout_matches", "programme_id", "programmes", "id"),
    ("routine_workout_matches", "revision_id", "programme_revisions", "id"),
)


def _user_from_row(row: Any) -> UserRow:
    """Build a :class:`UserRow` from a ``SELECT`` of ``_USER_COLUMNS``."""
    return UserRow(
        id=row[0],
        email=row[1],
        display_name=row[2],
        created_at=row[3],
        timezone=row[4] or "UTC",
        units=row[5] or "metric",
        status=row[6],
        status_changed_at=row[7] or row[3],
        updated_at=row[8] or row[3],
        last_login_at=row[9],
    )


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def require_tenant(user_id: str | None, db_path: str) -> str:
    """Return the tenant a user-scoped operation must act for.

    An explicit identifier is required in every runtime except the explicit
    non-production single-user mode described in :mod:`tenancy`. A missing
    identifier never selects a shared tenant or widens a query to every user:
    it either resolves to the local tenant in that mode or raises
    :class:`TenantRequiredError`.
    """
    if user_id is not None:
        if isinstance(user_id, str) and user_id.strip():
            return user_id
        raise TenantRequiredError("user_id must be a non-empty string.")
    if implicit_single_user_allowed():
        return get_legacy_user_id(db_path)
    raise TenantRequiredError(
        "user_id is required: implicit single-user access is disabled outside "
        "an explicit non-production runtime."
    )


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


def transaction(db_path: str = DEFAULT_DB_PATH) -> contextlib.AbstractContextManager[sqlite3.Connection]:
    """Open a connection whose work commits or rolls back as one unit.

    Public entry point for modules (accounts, ownership migration) that need a
    multi-statement transaction. Callers that must serialise against other
    writers should start it with ``BEGIN IMMEDIATE``.
    """
    return _connect(db_path)


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create or upgrade the schema.

    Production databases never receive a shared fallback tenant: the legacy
    holding account is only created when pre-account rows need an owner, and
    is then held inert (``unclaimed``) until an operator assigns its data to a
    verified owner. The local single-user tenant and its seed rows exist only in
    the explicit non-production mode (see :mod:`tenancy`).
    """
    implicit = implicit_single_user_allowed()
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        # Remove the previous guard before any table rebuild; it is recreated
        # with a deletion exemption once the deletion ledger exists below.
        cursor.execute("DROP TRIGGER IF EXISTS trg_programme_revisions_no_delete")
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
        # Hevy history engine: immutable routine snapshots, deterministic
        # workout matches, tenant-scoped user overrides and derived baselines.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS hevy_routine_snapshots (
                user_id TEXT NOT NULL REFERENCES users(id),
                snapshot_id TEXT NOT NULL,
                routine_id TEXT NOT NULL,
                title TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                updated_at TEXT,
                fetched_at TEXT,
                superseded_snapshot_id TEXT,
                snapshot_json TEXT NOT NULL,
                PRIMARY KEY (user_id, snapshot_id)
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_hevy_routine_snapshots_user_routine "
            "ON hevy_routine_snapshots (user_id, routine_id, content_hash)"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS routine_workout_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                programme_id INTEGER,
                revision_id INTEGER,
                routine_position INTEGER NOT NULL DEFAULT 1,
                source_routine_id TEXT NOT NULL DEFAULT '',
                workout_id TEXT NOT NULL,
                routine_snapshot_id TEXT,
                routine_id TEXT,
                score REAL NOT NULL DEFAULT 0,
                confidence,
                features_json TEXT NOT NULL DEFAULT '{}',
                method TEXT NOT NULL DEFAULT 'unknown',
                matcher_version TEXT NOT NULL DEFAULT 'unknown',
                is_ambiguous INTEGER NOT NULL DEFAULT 0,
                user_confirmed INTEGER NOT NULL DEFAULT 0,
                matched_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                UNIQUE(user_id, workout_id, matcher_version),
                UNIQUE(user_id, programme_id, workout_id)
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_routine_workout_matches_user_routine "
            "ON routine_workout_matches (user_id, routine_snapshot_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_routine_workout_matches_user_confidence "
            "ON routine_workout_matches (user_id, confidence)"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS routine_match_overrides (
                user_id TEXT NOT NULL REFERENCES users(id),
                pattern_type TEXT NOT NULL,
                pattern_value TEXT NOT NULL,
                routine_id TEXT NOT NULL,
                routine_snapshot_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, pattern_type, pattern_value)
            )
            """,
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS exercise_history_baselines (
                user_id TEXT NOT NULL REFERENCES users(id),
                template_id TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                confidence TEXT NOT NULL,
                evidence_start TEXT,
                evidence_end TEXT,
                baseline_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, template_id, rule_version)
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_history_baselines_user "
            "ON exercise_history_baselines (user_id, confidence)"
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
                hrv REAL,
                source TEXT NOT NULL DEFAULT 'legacy'
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
        # ``hevy_meta`` is created here in its pre-tenant shape and rebuilt as
        # (user_id, key) below. It is deliberately not seeded yet: an insert that
        # omits ``user_id`` would be an unowned row (and the owner guard rejects
        # it on every start after the rebuild). The local tenant's
        # ``programme_start_date`` is seeded with its owner after the rebuild.

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
            f"""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                display_name TEXT,
                created_at TEXT NOT NULL,
                timezone TEXT DEFAULT 'UTC',
                units TEXT DEFAULT 'metric',
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ({_ACCOUNT_STATUS_SQL})),
                status_changed_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                last_login_at TEXT
            )
            """,
        )
        # Per-user locale/accessibility columns and the immutable workout
        # instant. Runs before any user row is created so the one-time upgrade
        # step only ever sees accounts that predate per-user settings.
        from user_settings import ensure_settings_schema

        ensure_settings_schema(cursor)
        _migrate_user_model(cursor)

        # ---- Account access policy (issue #753) ----
        # Additive, idempotent columns. ``status`` and ``session_epoch`` are
        # read on every authenticated request, so they stay on the primary-key
        # row. ``session_epoch`` is bumped to revoke every signed session that
        # was issued under an earlier value (see ``revoke_account_sessions``).
        cursor.execute("PRAGMA table_info(users)")
        user_columns = {col[1] for col in cursor.fetchall()}
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

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS training_rules (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                version INTEGER NOT NULL DEFAULT 1,
                rules_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
        )
        # Verified Hevy connection metadata. Holds no credential material,
        # only the connected account identity, the last test time, the API
        # version and the capabilities the credential was proven to have.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS hevy_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                provider TEXT NOT NULL DEFAULT 'hevy',
                state TEXT NOT NULL,
                account_username TEXT,
                account_id TEXT,
                api_version TEXT,
                capabilities_json TEXT NOT NULL DEFAULT '{}',
                tested_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, provider)
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_training_rules_user "
            "ON training_rules (user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_hevy_connections_user "
            "ON hevy_connections (user_id)"
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

        # Rows written before accounts existed need an owner. ``legacy`` is None
        # when there are none (and the explicit local mode is off), so brand-new
        # production databases never gain a shared fallback tenant.
        legacy = _prepare_legacy_tenant(cursor, implicit=implicit)
        _assign_unowned(cursor, "daily_log", legacy)

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

        # Migration: Add user_id column to workout_history for multi-tenancy
        cursor.execute("PRAGMA table_info(workout_history)")
        woh_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in woh_columns:
            cursor.execute(
                "ALTER TABLE workout_history ADD COLUMN user_id TEXT REFERENCES users(id)"
            )
        _assign_unowned(cursor, "workout_history", legacy)
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
        _assign_unowned(cursor, "exercise_progress", legacy)
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
        _assign_unowned(cursor, "body_metrics", legacy)
        cursor.execute("DROP INDEX IF EXISTS idx_body_metrics_user")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_body_metrics_user_date_id "
            "ON body_metrics (user_id, date DESC, id DESC)",
        )

        # Migration: Record which connector imported a body-metric reading so a
        # connector purge can remove only its own data. Existing rows predate
        # provenance and stay as 'legacy' rather than being guessed.
        cursor.execute("PRAGMA table_info(body_metrics)")
        bm_columns = {row[1] for row in cursor.fetchall()}
        if "source" not in bm_columns:
            cursor.execute(
                "ALTER TABLE body_metrics "
                "ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy'",
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

        # ---- Hevy-first programme domain: additive, idempotent migration ----
        # The old programmes table remains the programme identity row so that
        # existing history is preserved.  New normalized revisions and their
        # child rows are the canonical domain; template_key/definition are only
        # a temporary compatibility read path for unmigrated runtime callers.
        # Remember which accounts arrived with several active programmes: the
        # domain migration below collapses them, but the authoritative-programme
        # decision must still record that a reconciliation took place.
        cursor.execute(
            "SELECT user_id FROM programmes WHERE active = 1 "
            "GROUP BY user_id HAVING COUNT(*) > 1"
        )
        reconciled_users = {row[0] for row in cursor.fetchall()}
        cursor.execute("PRAGMA table_info(programmes)")
        programme_columns = {row[1] for row in cursor.fetchall()}
        domain_columns = {
            "name": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'draft'",
            "goal": "TEXT",
            "duration_weeks": "INTEGER",
            "start_date": "TEXT",
            "end_date": "TEXT",
            "current_revision_id": "INTEGER",
            "activated_at": "TEXT",
            "archived_at": "TEXT",
            "predecessor_programme_id": "INTEGER REFERENCES programmes(id)",
        }
        had_status = "status" in programme_columns
        had_name = "name" in programme_columns
        for column, declaration in domain_columns.items():
            if column not in programme_columns:
                cursor.execute(
                    f"ALTER TABLE programmes ADD COLUMN {column} {declaration}"
                )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                programme_id INTEGER NOT NULL REFERENCES programmes(id),
                revision_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}',
                source_summary_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(programme_id, revision_number)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_routines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                programme_id INTEGER NOT NULL REFERENCES programmes(id),
                revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
                position INTEGER NOT NULL,
                provider TEXT NOT NULL DEFAULT 'hevy',
                source_routine_id TEXT NOT NULL,
                title TEXT NOT NULL,
                folder_id TEXT,
                provider_updated_at TEXT,
                snapshot_hash TEXT NOT NULL,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                participation TEXT NOT NULL DEFAULT 'full',
                cadence TEXT NOT NULL DEFAULT 'rotation',
                source_conflict_state TEXT NOT NULL DEFAULT 'unchanged',
                created_at TEXT NOT NULL,
                UNIQUE(revision_id, position),
                UNIQUE(revision_id, source_routine_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                programme_id INTEGER NOT NULL REFERENCES programmes(id),
                revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
                position INTEGER NOT NULL,
                key TEXT NOT NULL,
                name TEXT NOT NULL,
                start_week INTEGER NOT NULL,
                end_week INTEGER NOT NULL,
                objective TEXT NOT NULL DEFAULT '',
                targets_json TEXT NOT NULL DEFAULT '{}',
                adaptation_policy_json TEXT NOT NULL DEFAULT '{}',
                exit_criteria_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(revision_id, position),
                CHECK(start_week >= 1 AND end_week >= start_week)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS exercise_prescriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                programme_id INTEGER NOT NULL REFERENCES programmes(id),
                revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
                routine_position INTEGER,
                block_position INTEGER,
                week_number INTEGER,
                exercise_template_id TEXT NOT NULL,
                exercise_title TEXT NOT NULL,
                role TEXT NOT NULL,
                set_target_json TEXT NOT NULL DEFAULT '{}',
                rep_target_json TEXT NOT NULL DEFAULT '{}',
                effort_target_json TEXT NOT NULL DEFAULT '{}',
                load_anchor_json TEXT NOT NULL DEFAULT '{}',
                rest_seconds INTEGER,
                progression_method TEXT NOT NULL,
                progression_rules_json TEXT NOT NULL DEFAULT '{}',
                confidence REAL NOT NULL DEFAULT 0,
                rule_version TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_week_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                programme_id INTEGER NOT NULL REFERENCES programmes(id),
                revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
                week_number INTEGER NOT NULL,
                planned_json TEXT NOT NULL DEFAULT '{}',
                actual_json TEXT NOT NULL DEFAULT '{}',
                adherence REAL,
                fatigue REAL,
                review_state TEXT NOT NULL DEFAULT 'planned',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(revision_id, week_number)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS adaptation_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                programme_id INTEGER NOT NULL REFERENCES programmes(id),
                from_revision_id INTEGER REFERENCES programme_revisions(id),
                to_revision_id INTEGER NOT NULL REFERENCES programme_revisions(id),
                scope_json TEXT NOT NULL DEFAULT '{}',
                inputs_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT NOT NULL DEFAULT '{}',
                confidence REAL NOT NULL DEFAULT 0,
                rule_version TEXT NOT NULL,
                explanation TEXT NOT NULL DEFAULT '',
                confirmed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )

        # Complete a partially applied domain upgrade without rebuilding rows.
        domain_table_columns: dict[str, dict[str, str]] = {
            "programme_revisions": {
                "user_id": "TEXT",
                "programme_id": "INTEGER",
                "revision_number": "INTEGER NOT NULL DEFAULT 1",
                "reason": "TEXT NOT NULL DEFAULT 'legacy_migration'",
                "engine_version": "TEXT NOT NULL DEFAULT 'legacy-json/v0'",
                "settings_json": "TEXT NOT NULL DEFAULT '{}'",
                "source_summary_json": "TEXT NOT NULL DEFAULT '{}'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
            "programme_routines": {
                "user_id": "TEXT",
                "programme_id": "INTEGER",
                "revision_id": "INTEGER",
                "position": "INTEGER NOT NULL DEFAULT 1",
                "provider": "TEXT NOT NULL DEFAULT 'hevy'",
                "source_routine_id": "TEXT NOT NULL DEFAULT ''",
                "title": "TEXT NOT NULL DEFAULT ''",
                "folder_id": "TEXT",
                "provider_updated_at": "TEXT",
                "snapshot_hash": "TEXT NOT NULL DEFAULT ''",
                "snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "participation": "TEXT NOT NULL DEFAULT 'full'",
                "cadence": "TEXT NOT NULL DEFAULT 'rotation'",
                "source_conflict_state": "TEXT NOT NULL DEFAULT 'unknown'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
            "programme_blocks": {
                "user_id": "TEXT",
                "programme_id": "INTEGER",
                "revision_id": "INTEGER",
                "position": "INTEGER NOT NULL DEFAULT 1",
                "key": "TEXT NOT NULL DEFAULT ''",
                "name": "TEXT NOT NULL DEFAULT ''",
                "start_week": "INTEGER NOT NULL DEFAULT 1",
                "end_week": "INTEGER NOT NULL DEFAULT 1",
                "objective": "TEXT NOT NULL DEFAULT ''",
                "targets_json": "TEXT NOT NULL DEFAULT '{}'",
                "adaptation_policy_json": "TEXT NOT NULL DEFAULT '{}'",
                "exit_criteria_json": "TEXT NOT NULL DEFAULT '{}'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
            "exercise_prescriptions": {
                "user_id": "TEXT",
                "programme_id": "INTEGER",
                "revision_id": "INTEGER",
                "routine_position": "INTEGER",
                "block_position": "INTEGER",
                "week_number": "INTEGER",
                "exercise_template_id": "TEXT NOT NULL DEFAULT ''",
                "exercise_title": "TEXT NOT NULL DEFAULT ''",
                "role": "TEXT NOT NULL DEFAULT ''",
                "set_target_json": "TEXT NOT NULL DEFAULT '{}'",
                "rep_target_json": "TEXT NOT NULL DEFAULT '{}'",
                "effort_target_json": "TEXT NOT NULL DEFAULT '{}'",
                "load_anchor_json": "TEXT NOT NULL DEFAULT '{}'",
                "rest_seconds": "INTEGER",
                "progression_method": "TEXT NOT NULL DEFAULT 'double_progression'",
                "progression_rules_json": "TEXT NOT NULL DEFAULT '{}'",
                "confidence": "REAL NOT NULL DEFAULT 0",
                "rule_version": "TEXT NOT NULL DEFAULT 'legacy-json/v0'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
            "programme_week_states": {
                "user_id": "TEXT",
                "programme_id": "INTEGER",
                "revision_id": "INTEGER",
                "week_number": "INTEGER NOT NULL DEFAULT 1",
                "planned_json": "TEXT NOT NULL DEFAULT '{}'",
                "actual_json": "TEXT NOT NULL DEFAULT '{}'",
                "adherence": "REAL",
                "fatigue": "REAL",
                "review_state": "TEXT NOT NULL DEFAULT 'planned'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
            "adaptation_decisions": {
                "user_id": "TEXT",
                "programme_id": "INTEGER",
                "from_revision_id": "INTEGER",
                "to_revision_id": "INTEGER",
                "scope_json": "TEXT NOT NULL DEFAULT '{}'",
                "inputs_json": "TEXT NOT NULL DEFAULT '{}'",
                "output_json": "TEXT NOT NULL DEFAULT '{}'",
                "confidence": "REAL NOT NULL DEFAULT 0",
                "rule_version": "TEXT NOT NULL DEFAULT 'legacy-json/v0'",
                "explanation": "TEXT NOT NULL DEFAULT ''",
                "confirmed": "INTEGER NOT NULL DEFAULT 0",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
            "routine_workout_matches": {
                "id": "INTEGER",
                "user_id": "TEXT",
                "programme_id": "INTEGER",
                "revision_id": "INTEGER",
                "routine_position": "INTEGER NOT NULL DEFAULT 1",
                "source_routine_id": "TEXT NOT NULL DEFAULT ''",
                "workout_id": "TEXT NOT NULL DEFAULT ''",
                "routine_snapshot_id": "TEXT",
                "routine_id": "TEXT",
                "score": "REAL NOT NULL DEFAULT 0",
                "confidence": "REAL NOT NULL DEFAULT 0",
                "features_json": "TEXT NOT NULL DEFAULT '{}'",
                "method": "TEXT NOT NULL DEFAULT 'unknown'",
                "matcher_version": "TEXT NOT NULL DEFAULT 'unknown'",
                "is_ambiguous": "INTEGER NOT NULL DEFAULT 0",
                "user_confirmed": "INTEGER NOT NULL DEFAULT 0",
                "matched_at": "TEXT NOT NULL DEFAULT ''",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, column_declarations in domain_table_columns.items():
            cursor.execute(f"PRAGMA table_info({table})")
            existing_columns = {row[1] for row in cursor.fetchall()}
            for column, declaration in column_declarations.items():
                if column not in existing_columns:
                    cursor.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                    )

        migration_now = datetime.now(tz=timezone.utc).isoformat()
        if not had_name:
            cursor.execute(
                """
                UPDATE programmes
                SET name = COALESCE(
                    NULLIF(name, ''),
                    NULLIF(template_key, ''),
                    'Programme ' || id
                )
                """
            )
        # Only infer lifecycle state on an upgrade where the column is new.
        if not had_status:
            cursor.execute(
                """
                UPDATE programmes
                SET status = CASE WHEN active = 1 THEN 'active' ELSE 'archived' END
                """
            )

        # Archive any previously active static template.  Existing rows stay
        # inspectable through an immutable legacy_static revision, but no static
        # programme is offered or seeded for new users.
        archived = cursor.execute(
            """
            UPDATE programmes
            SET active = 0,
                source = 'legacy_static',
                status = 'archived',
                archived_at = COALESCE(archived_at, ?),
                updated_at = ?
            WHERE source = 'template'
               OR template_key = 'hybrid_powerbuilding'
               OR source = 'legacy_static'
            """,
            (migration_now, migration_now),
        )
        if archived.rowcount:
            logger.info(
                "Archived %d legacy static programme revision(s); "
                "history is retained and a Hevy-native programme must be activated.",
                archived.rowcount,
            )

        # Keep the domain status and the legacy active flag aligned while both
        # read paths coexist.  Old callers only touched programmes.active, so a
        # row can be inactive while its status still says 'active'.  Retire
        # those stale statuses *before* promoting the newly active row: doing it
        # the other way round collides with the partial one-active unique index
        # on the next startup and blocks database initialisation.
        cursor.execute(
            """
            UPDATE programmes
            SET status = 'archived',
                archived_at = COALESCE(archived_at, ?)
            WHERE active = 0 AND status = 'active'
            """,
            (migration_now,),
        )

        # Collapse duplicate legacy active flags before promotion so the
        # one-active invariant can never be violated by a partially upgraded
        # database.  Prefer rows the domain already considers active, then the
        # most recently updated row.
        active_rows = cursor.execute(
            """
            SELECT id, user_id FROM programmes
            WHERE active = 1
            ORDER BY user_id ASC, (status = 'active') DESC,
                     updated_at DESC, id DESC
            """
        ).fetchall()
        seen_active_users: set[str] = set()
        for programme_id, owner_id in active_rows:
            if owner_id in seen_active_users:
                cursor.execute(
                    """
                    UPDATE programmes
                    SET status = 'archived', active = 0,
                        archived_at = COALESCE(archived_at, ?), updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (migration_now, migration_now, programme_id, owner_id),
                )
            else:
                seen_active_users.add(owner_id)

        cursor.execute(
            """
            UPDATE programmes
            SET status = 'active'
            WHERE active = 1 AND status <> 'active'
              AND source <> 'legacy_static'
            """
        )

        # Preserve every pre-domain programme as an inspectable first revision.
        legacy_programmes = cursor.execute(
            """
            SELECT p.id, p.user_id, p.source, p.template_key, p.definition,
                   p.created_at
            FROM programmes AS p
            WHERE NOT EXISTS (
                SELECT 1 FROM programme_revisions AS r
                WHERE r.programme_id = p.id AND r.user_id = p.user_id
            )
            """
        ).fetchall()
        for programme_id, owner_id, source, template_key, definition, created_at in (
            legacy_programmes
        ):
            is_static = (
                source in {"template", "legacy_static"}
                or template_key == "hybrid_powerbuilding"
            )
            try:
                settings = json.loads(definition) if definition else {}
            except (TypeError, ValueError):
                settings = {"legacy_definition": definition}
            reason = "legacy_static" if is_static else "legacy_migration"
            engine_version = "legacy-static" if is_static else "legacy-json/v0"
            source_summary = {
                "source": source,
                "template_key": template_key,
                "migration": reason,
            }
            revision_cursor = cursor.execute(
                """
                INSERT INTO programme_revisions
                    (user_id, programme_id, revision_number, reason,
                     engine_version, settings_json, source_summary_json,
                     created_at)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    programme_id,
                    reason,
                    engine_version,
                    json.dumps(settings, default=str, sort_keys=True),
                    json.dumps(source_summary, default=str, sort_keys=True),
                    created_at or migration_now,
                ),
            )
            cursor.execute(
                "UPDATE programmes SET current_revision_id = ? "
                "WHERE id = ? AND user_id = ?",
                (revision_cursor.lastrowid, programme_id, owner_id),
            )

        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_programmes_one_active_per_user "
            "ON programmes (user_id) WHERE status = 'active'"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programmes_user_status "
            "ON programmes (user_id, status)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programme_revisions_user_programme "
            "ON programme_revisions (user_id, programme_id, revision_number)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programme_routines_user_revision "
            "ON programme_routines (user_id, revision_id, position)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programme_blocks_user_revision "
            "ON programme_blocks (user_id, revision_id, position)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exercise_prescriptions_user_revision "
            "ON exercise_prescriptions (user_id, revision_id, week_number)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programme_week_states_user_revision "
            "ON programme_week_states (user_id, revision_id, week_number)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_adaptation_decisions_user_programme "
            "ON adaptation_decisions (user_id, programme_id, id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_routine_workout_matches_user_programme "
            "ON routine_workout_matches (user_id, programme_id, id)"
        )
        # Recreate unique constraints as named indexes so partially upgraded
        # databases get the same conflict targets as fresh databases.
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_programme_revisions_programme_number "
            "ON programme_revisions (programme_id, revision_number)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_programme_routines_revision_position "
            "ON programme_routines (revision_id, position)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_programme_routines_revision_source "
            "ON programme_routines (revision_id, source_routine_id)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_programme_blocks_revision_position "
            "ON programme_blocks (revision_id, position)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_programme_week_states_revision_week "
            "ON programme_week_states (revision_id, week_number)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_routine_workout_matches_user_programme_workout "
            "ON routine_workout_matches (user_id, programme_id, workout_id)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_routine_workout_matches_user_workout_version "
            "ON routine_workout_matches (user_id, workout_id, matcher_version)"
        )

        # Activated revisions are immutable at the database boundary, not only
        # through service convention.  New revisions are inserted, never updated.
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_programme_revisions_no_update
            BEFORE UPDATE ON programme_revisions
            BEGIN
                SELECT RAISE(ABORT, 'programme revisions are immutable');
            END
            """
        )
        # Migration: Add user_id column to chat_messages
        cursor.execute("PRAGMA table_info(chat_messages)")
        cm_columns = {row[1] for row in cursor.fetchall()}
        if "user_id" not in cm_columns:
            cursor.execute(
                "ALTER TABLE chat_messages ADD COLUMN user_id TEXT REFERENCES users(id)",
            )
        _assign_unowned(cursor, "chat_messages", legacy)
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
                rl_legacy_id = _legacy_owner(legacy)
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
                di_legacy_id = _legacy_owner(legacy)
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
                dc_legacy_id = _legacy_owner(legacy)
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
        _assign_unowned(cursor, "check_ins", legacy)
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
                ps_legacy_id = _legacy_owner(legacy)
                cursor.execute(
                    "INSERT OR REPLACE INTO programme_state_new "
                    "(user_id, current_day, split_name) VALUES (?, ?, ?)",
                    (ps_legacy_id, old_row[0], old_row[1]),
                )
            elif implicit and legacy is not None:
                # Brand-new local database: seed a default row for the local tenant
                cursor.execute(
                    "INSERT OR REPLACE INTO programme_state_new "
                    "(user_id, current_day, split_name) VALUES (?, 1, ?)",
                    (legacy, CANONICAL_SPLIT_NAME),
                )
            cursor.execute("DROP TABLE programme_state")
            cursor.execute("ALTER TABLE programme_state_new RENAME TO programme_state")
        if implicit and legacy is not None:
            cursor.execute(
                "INSERT OR IGNORE INTO programme_state "
                "(user_id, current_day, split_name) VALUES (?, 1, ?)",
                (legacy, CANONICAL_SPLIT_NAME),
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
                hm_legacy_id = _legacy_owner(legacy)
                for row in old_rows:
                    cursor.execute(
                        "INSERT OR REPLACE INTO hevy_meta_new "
                        "(user_id, key, value) VALUES (?, ?, ?)",
                        (hm_legacy_id, row[0], row[1]),
                    )
            elif implicit and legacy is not None:
                # Brand-new local database: seed programme_start_date for the
                # local tenant
                cursor.execute(
                    "INSERT OR REPLACE INTO hevy_meta_new (user_id, key, value) "
                    "VALUES (?, 'programme_start_date', ?)",
                    (legacy, datetime.now(tz=timezone.utc).date().isoformat()),
                )
            cursor.execute("DROP TABLE hevy_meta")
            cursor.execute("ALTER TABLE hevy_meta_new RENAME TO hevy_meta")

        if implicit and legacy is not None:
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
                    (_legacy_owner(legacy), row[0], row[1], row[2]),
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
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programmes_user "
            "ON programmes (user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programmes_user_active "
            "ON programmes (user_id, active)"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                programme_id INTEGER REFERENCES programmes(id),
                source TEXT NOT NULL,
                template_key TEXT,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_programme_decisions_user "
            "ON programme_decisions (user_id)"
        )

        # Migration: Identify authoritative active programme version and record decision
        cursor.execute("SELECT id FROM users")
        for u_row in cursor.fetchall():
            uid = u_row[0]
            active_progs = cursor.execute(
                """
                SELECT id, source, template_key, definition, active, created_at, updated_at
                FROM programmes
                WHERE user_id = ? AND active = 1
                ORDER BY (CASE WHEN source = 'hevy' THEN 1 ELSE 0 END) DESC, updated_at DESC, id DESC
                """,
                (uid,),
            ).fetchall()

            if active_progs:
                auth_prog = active_progs[0]
                auth_id = auth_prog[0]
                auth_source = auth_prog[1]
                auth_key = auth_prog[2]
                auth_def_raw = auth_prog[3]

                # Archive static templates if no Hevy programme is present
                if auth_source in ("template", "legacy_static") or auth_key == "hybrid_powerbuilding":
                    now_iso = datetime.now(tz=timezone.utc).isoformat()
                    cursor.execute(
                        """
                        UPDATE programmes
                        SET active = 0, source = 'legacy_static', updated_at = ?
                        WHERE user_id = ? AND active = 1
                        """,
                        (now_iso, uid),
                    )
                    cursor.execute(
                        """
                        INSERT INTO programme_decisions
                            (user_id, programme_id, source, template_key, decision, reason, created_at)
                        VALUES (?, ?, 'legacy_static', ?, 'archived', 'retired_static_template', ?)
                        """,
                        (uid, auth_id, auth_key, now_iso),
                    )
                    continue

                if len(active_progs) > 1:
                    cursor.execute(
                        "UPDATE programmes SET active = 0 WHERE user_id = ? AND id != ?",
                        (uid, auth_id),
                    )
                if len(active_progs) > 1 or uid in reconciled_users:
                    reason = "reconciled_multiple_active"
                else:
                    reason = "single_active"

                existing_decision = cursor.execute(
                    "SELECT id FROM programme_decisions WHERE user_id = ? AND programme_id = ?",
                    (uid, auth_id),
                ).fetchone()

                if not existing_decision or len(active_progs) > 1:
                    now_iso = datetime.now(tz=timezone.utc).isoformat()
                    cursor.execute(
                        """
                        INSERT INTO programme_decisions
                            (user_id, programme_id, source, template_key, decision, reason, created_at)
                        VALUES (?, ?, ?, ?, 'authoritative', ?, ?)
                        """,
                        (uid, auth_id, auth_source, auth_key, reason, now_iso),
                    )

                if auth_def_raw:
                    try:
                        def_obj = json.loads(auth_def_raw) if isinstance(auth_def_raw, str) else auth_def_raw
                        if isinstance(def_obj, dict) and def_obj.get("name"):
                            cursor.execute(
                                "UPDATE programme_state SET split_name = ? WHERE user_id = ? AND split_name = ?",
                                (def_obj["name"], uid, CANONICAL_SPLIT_NAME),
                            )
                    except (json.JSONDecodeError, TypeError, KeyError) as exc:
                        logger.debug("Failed to decode programme definition for name sync: %s", exc)

        _init_credential_vault_schema(cursor)

        # Server-side registry behind the browser session cookie (issue #752).
        ensure_user_sessions_schema(conn)

        # Additive canonical health schema. Created before the owner guards so
        # every tenant-scoped health table is covered by them.
        from health_schema import HEALTH_SCHEMA_SQL

        cursor.executescript(HEALTH_SCHEMA_SQL)

        # Last, after every table rebuild above: a drop-and-rename discards the
        # table's triggers, so the guards are installed once the schema is final.
        _install_owner_guards(cursor)
        if legacy is not None and not implicit:
            _warn_if_legacy_data_unclaimed(cursor, legacy)

        # Append-only, hash-chained security audit trail (see security_audit.py).
        ensure_security_audit_schema(conn)

        # ---- Account / health-data deletion ledger (see account_deletion.py) ----
        # These tables deliberately carry no foreign key to users(id): the ledger
        # must outlive the account it describes. They hold opaque identifiers,
        # timestamps and row counts only - never personal or health data.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS account_deletion_requests (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                scope TEXT NOT NULL CHECK (scope IN ('account', 'health_data')),
                state TEXT NOT NULL CHECK (
                    state IN ('pending', 'in_progress', 'failed', 'completed', 'cancelled')
                ),
                requested_at TEXT NOT NULL,
                execute_after TEXT NOT NULL,
                next_attempt_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                cancelled_at TEXT,
                error_code TEXT
            )
            """
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_account_deletion_open_request "
            "ON account_deletion_requests (user_id) "
            "WHERE state IN ('pending', 'in_progress', 'failed')"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_deletion_requests_user "
            "ON account_deletion_requests (user_id, requested_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_deletion_requests_due "
            "ON account_deletion_requests (state, next_attempt_at)"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS account_deletion_steps (
                request_id TEXT NOT NULL REFERENCES account_deletion_requests(id),
                step TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'done')),
                rows_affected INTEGER NOT NULL DEFAULT 0,
                remaining_after INTEGER,
                completed_at TEXT,
                PRIMARY KEY (request_id, step)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS account_tombstones (
                user_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                deleted_at TEXT NOT NULL,
                retain_until TEXT NOT NULL,
                policy_version TEXT NOT NULL
            )
            """
        )

        # Keep revisions immutable for the application, but allow the account
        # deletion engine (and a post-restore reconcile, which writes the
        # tombstone first) to erase them for the account being deleted. Installed
        # after the ledger exists so a table rebuild cannot trip over the trigger.
        cursor.execute("DROP TRIGGER IF EXISTS trg_programme_revisions_no_delete")
        cursor.execute(
            """
            CREATE TRIGGER trg_programme_revisions_no_delete
            BEFORE DELETE ON programme_revisions
            WHEN NOT EXISTS (
                    SELECT 1 FROM account_deletion_requests
                    WHERE user_id = OLD.user_id AND state = 'in_progress')
             AND NOT EXISTS (
                    SELECT 1 FROM account_tombstones WHERE user_id = OLD.user_id)
            BEGIN
                SELECT RAISE(ABORT, 'programme revisions are immutable');
            END
            """
        )

        # Provenance and lineage ledger (additive, append-only, tenant scoped).
        ledger.ensure_schema(cursor)

        # Owner existence, immutability and same-tenant references. Installed
        # after every table exists (including the ledger above) and after the
        # last table rebuild, which would otherwise discard the triggers.
        _install_owner_integrity_guards(cursor)


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
            revoked_reason TEXT,
            device TEXT,
            location TEXT
        )
        """
    )
    # Additive migration for registries created before device visibility existed.
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(user_sessions)").fetchall()
    }
    # A concurrent first request may win the ALTER; the column is then already
    # present, which is the same successful outcome.
    with contextlib.suppress(sqlite3.OperationalError):
        if "device" not in columns:
            conn.execute("ALTER TABLE user_sessions ADD COLUMN device TEXT")
        if "location" not in columns:
            conn.execute("ALTER TABLE user_sessions ADD COLUMN location TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_user "
        "ON user_sessions (user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_user_active "
        "ON user_sessions (user_id, revoked_at)"
    )


def _warn_if_legacy_data_unclaimed(cursor: sqlite3.Cursor, legacy: str) -> None:
    """Make stranded pre-account data visible instead of silently inaccessible.

    In production that data belongs to nobody until an operator assigns it to a
    verified owner. Failing closed is correct, but it must not be silent. The
    message names the remedy and carries no email address or identifier.
    """
    row = cursor.execute("SELECT status FROM users WHERE id = ?", (legacy,)).fetchone()
    if row is None or row[0] != "unclaimed":
        return
    for table in USER_OWNED_TABLES:
        if cursor.execute(
            f"SELECT 1 FROM {table} WHERE user_id = ? LIMIT 1", (legacy,)
        ).fetchone():
            logger.warning(
                "Pre-account data is held by an unclaimed holding account and "
                "is not accessible to any user. Review and assign it to a verified "
                "owner: python scripts/manage_accounts.py plan-legacy-assignment "
                "--owner-email <owner>."
            )
            return


def _install_owner_guards(cursor: sqlite3.Cursor) -> None:
    """Make an ownerless write to a user-owned table impossible.

    ``REFERENCES users(id)`` is only enforced when ``PRAGMA foreign_keys`` is on,
    and it never rejects NULL. Several legacy tables gained ``user_id`` through
    ``ALTER TABLE ... ADD COLUMN`` (so it is nullable) and SQLite admits NULL in a
    non-INTEGER ``PRIMARY KEY``. These triggers reject any INSERT, and any UPDATE
    of ``user_id``, that would leave a row without an owner, whichever code path
    issues it. Whether the owner is the *authenticated* user is decided at the
    boundary (``accounts.require_active_user``); this is the persistence-level
    backstop for "every record has an owner".

    Idempotent (``IF NOT EXISTS``), additive, and carries no data migration.
    """
    _install_owner_required_guards(cursor, USER_OWNED_TABLES)


def _install_owner_required_guards(
    cursor: sqlite3.Cursor, tables: Iterable[str]
) -> None:
    """Install the ``user_id is required`` insert/update guards on *tables*."""
    for table in tables:
        cursor.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_owner_required_insert
            BEFORE INSERT ON {table}
            WHEN NEW.user_id IS NULL OR NEW.user_id = ''
            BEGIN
                SELECT RAISE(ABORT, '{table}.user_id is required');
            END
            """
        )
        cursor.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_owner_required_update
            BEFORE UPDATE OF user_id ON {table}
            WHEN NEW.user_id IS NULL OR NEW.user_id = ''
            BEGIN
                SELECT RAISE(ABORT, '{table}.user_id is required');
            END
            """
        )


def owned_references(cursor: sqlite3.Cursor) -> list[tuple[str, str, str, str]]:
    """Return every reference from one owner-scoped table to another.

    Each item is ``(child, column, parent, parent_column)``. Declared foreign
    keys are read from the live schema; the few references the schema does not
    declare are listed in ``_UNDECLARED_OWNED_REFERENCES``. Every name comes
    from the schema this module creates, never from a request.
    """
    owned = set(USER_OWNED_TABLES)
    found: dict[tuple[str, str, str, str], None] = {}
    for table in USER_OWNED_TABLES:
        for row in cursor.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            parent, column, parent_column = row[2], row[3], row[4]
            if parent in owned and column and parent_column:
                found[(table, column, parent, parent_column)] = None
    for edge in _UNDECLARED_OWNED_REFERENCES:
        found[edge] = None
    for edge in found:
        if not all(name.isidentifier() for name in edge):
            raise ValueError(f"unsafe SQL identifier in owner reference {edge!r}")
    return list(found)


def _install_owner_integrity_guards(cursor: sqlite3.Cursor) -> None:
    """Enforce, inside the database, that an owner is real and cannot change.

    ``REFERENCES users(id)`` is decorative while ``PRAGMA foreign_keys`` is off,
    which is how every runtime connection opens. These triggers make the same
    guarantees unconditional for every user-scoped table, whichever code path
    (repository, migration, ad-hoc SQL) issues the statement:

    * **Owner exists.** An INSERT, or an UPDATE of ``user_id``, must name a row
      of ``users``. A row can never belong to an account that does not exist.
    * **Owner is immutable.** Once a row has an owner it cannot be re-assigned,
      so a bug or a crafted statement cannot hand one user's record to another.
      Taking ownership of a previously unowned row (``NULL`` or empty) stays
      possible for the pre-account backfill. The two audited legacy-adoption
      flows lift this guard inside their own transaction, exactly as they lift
      the programme-revision ``no_update`` guards, and recreate it before commit.
    * **References stay inside the tenant.** A row that points at another
      owner-scoped row (a revision's programme, a routine's revision, ...) must
      point at one with the *same* ``user_id``. Without it an indirect object
      reference can be written that resolves into another user's data.

    The ``WHEN`` clauses are mutually exclusive with the ``owner_required``
    guards, so an ownerless write always reports ``user_id is required``.
    Append-only tables get the insert-time existence guard only: their own
    trigger already aborts every UPDATE.
    Idempotent (``IF NOT EXISTS``), additive, and carries no data migration.
    """
    for table in (*USER_OWNED_TABLES, *ACCOUNT_BOUND_TABLES):
        # The registry tables turn away a NULL or empty owner with their
        # ``owner_required`` guards; the account-bound tables rely on NOT NULL,
        # so for them an empty owner must fail the existence check as well.
        named = (
            "NEW.user_id IS NOT NULL"
            if table in ACCOUNT_BOUND_TABLES
            else "NEW.user_id IS NOT NULL AND NEW.user_id <> ''"
        )
        cursor.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_owner_exists_insert
            BEFORE INSERT ON {table}
            WHEN {named}
                 AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id)
            BEGIN
                SELECT RAISE(ABORT, '{table}.user_id must reference an existing user');
            END
            """
        )
        if table in APPEND_ONLY_OWNED_TABLES:
            continue
        cursor.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_owner_exists_update
            BEFORE UPDATE OF user_id ON {table}
            WHEN {named} AND NEW.user_id IS NOT OLD.user_id
                 AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id)
            BEGIN
                SELECT RAISE(ABORT, '{table}.user_id must reference an existing user');
            END
            """
        )
        cursor.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_user_immutable
            BEFORE UPDATE OF user_id ON {table}
            WHEN OLD.user_id IS NOT NULL AND OLD.user_id <> ''
                 AND NEW.user_id IS NOT NULL AND NEW.user_id <> ''
                 AND NEW.user_id IS NOT OLD.user_id
            BEGIN
                SELECT RAISE(ABORT, '{table}.user_id is immutable');
            END
            """
        )

    for child, column, parent, parent_column in owned_references(cursor):
        message = f"{child}.{column} must reference a row owned by the same user"
        same_owner = (
            f"NEW.{column} IS NOT NULL "
            "AND NEW.user_id IS NOT NULL AND NEW.user_id <> '' "
            f"AND NOT EXISTS (SELECT 1 FROM {parent} "
            f"WHERE {parent_column} = NEW.{column} AND user_id = NEW.user_id)"
        )
        cursor.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{child}_{column}_same_owner_insert
            BEFORE INSERT ON {child}
            WHEN {same_owner}
            BEGIN
                SELECT RAISE(ABORT, '{message}');
            END
            """
        )
        cursor.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{child}_{column}_same_owner_update
            BEFORE UPDATE OF {column} ON {child}
            WHEN NEW.{column} IS NOT OLD.{column} AND {same_owner}
            BEGIN
                SELECT RAISE(ABORT, '{message}');
            END
            """
        )


_USER_UPGRADE_COLUMNS = (
    (
        "status",
        f"TEXT NOT NULL DEFAULT 'active' CHECK (status IN ({_ACCOUNT_STATUS_SQL}))",
    ),
    ("status_changed_at", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
    ("last_login_at", "TEXT"),
)

# Tables whose rows predate accounts and therefore may have no owner. Only
# these names are ever interpolated into the backfill/detection SQL.
_UNOWNED_ROW_TABLES = (
    "workout_history",
    "exercise_progress",
    "body_metrics",
    "daily_log",
    "check_ins",
    "chat_messages",
    "programme_state",
    "hevy_meta",
    "hevy_routines",
    "dashboard_insights",
    "deep_correlations",
    "reasoning_logs",
)


def _migrate_user_model(cursor: sqlite3.Cursor) -> None:
    """Upgrade ``users`` and create the identity and audit tables.

    Additive and idempotent: safe for a brand-new database, a database created
    before accounts had a lifecycle, and a startup that stopped part-way.

    * ``users.id`` and ``users.created_at`` become immutable.
    * ``users.status`` records the account lifecycle; retired/deleted accounts
      can never change status again.
    * ``user_identities`` maps each external ``(provider, subject)`` to exactly
      one account. Identity columns are immutable, so an identity can never be
      re-pointed at another account, and the legacy holding can never gain
      one.
    * ``legacy_ownership_migrations`` is an append-only audit record of the
      explicit assignment of pre-account data to a verified owner.
    """
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    for name, definition in _USER_UPGRADE_COLUMNS:
        if name not in columns:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
    # Accounts that predate the lifecycle columns can only honestly claim the
    # time they were created.
    cursor.execute(
        "UPDATE users SET status_changed_at = created_at WHERE status_changed_at = ''"
    )
    cursor.execute("UPDATE users SET updated_at = created_at WHERE updated_at = ''")

    # ``users.email`` is UNIQUE but case-sensitive, while every lookup compares
    # ``lower(email)``. Make the database agree so two accounts can never claim
    # the same address, which would make an email-based adoption ambiguous. If a
    # pre-existing database already holds such a pair, do not refuse to start:
    # leave the index out and let ``accounts.resolve_oauth_user`` refuse those
    # addresses (fail closed) until an operator resolves the duplicate.
    collisions = cursor.execute(
        "SELECT lower(email) FROM users GROUP BY lower(email) HAVING COUNT(*) > 1"
    ).fetchall()
    if collisions:
        logger.error(
            "%d account email address(es) differ only by letter case; the "
            "case-insensitive unique email index was not created and sign-in "
            "refuses those addresses until the duplicates are resolved.",
            len(collisions),
        )
    else:
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower "
            "ON users (lower(email))"
        )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES users(id),
            provider TEXT NOT NULL
                CHECK (length(provider) > 0 AND provider = lower(provider)),
            subject TEXT NOT NULL CHECK (length(subject) > 0),
            email TEXT,
            email_verified INTEGER NOT NULL DEFAULT 0
                CHECK (email_verified IN (0, 1)),
            created_at TEXT NOT NULL,
            last_login_at TEXT,
            UNIQUE (provider, subject),
            UNIQUE (user_id, provider)
        )
        """,
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_ownership_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_digest TEXT NOT NULL,
            from_user_id TEXT NOT NULL REFERENCES users(id),
            to_user_id TEXT NOT NULL REFERENCES users(id),
            owner_email TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            applied_by TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """,
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_legacy_ownership_migrations_to_user "
        "ON legacy_ownership_migrations (to_user_id, id)"
    )

    triggers = (
        """
        CREATE TRIGGER IF NOT EXISTS trg_users_id_immutable
        BEFORE UPDATE OF id ON users
        WHEN NEW.id IS NOT OLD.id
        BEGIN
            SELECT RAISE(ABORT, 'users.id is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_users_created_at_immutable
        BEFORE UPDATE OF created_at ON users
        WHEN NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'users.created_at is immutable');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_users_status_terminal
        BEFORE UPDATE OF status ON users
        WHEN OLD.status IN ({_TERMINAL_STATUS_SQL})
             AND NEW.status IS NOT OLD.status
        BEGIN
            SELECT RAISE(ABORT, 'a retired or deleted account cannot change status');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_user_identities_user_exists
        BEFORE INSERT ON user_identities
        WHEN NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id)
        BEGIN
            SELECT RAISE(ABORT, 'user_identities.user_id must reference an existing user');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_user_identities_not_for_holding
        BEFORE INSERT ON user_identities
        WHEN EXISTS (
            SELECT 1 FROM users
            WHERE id = NEW.user_id AND email = '{LEGACY_TENANT_EMAIL}'
        )
        BEGIN
            SELECT RAISE(ABORT, 'the legacy holding account cannot have an identity');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_user_identities_immutable
        BEFORE UPDATE OF user_id, provider, subject, created_at ON user_identities
        WHEN NEW.user_id IS NOT OLD.user_id
             OR NEW.provider IS NOT OLD.provider
             OR NEW.subject IS NOT OLD.subject
             OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'user_identities identity columns are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_legacy_ownership_migrations_no_update
        BEFORE UPDATE ON legacy_ownership_migrations
        BEGIN
            SELECT RAISE(ABORT, 'legacy_ownership_migrations is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_legacy_ownership_migrations_no_delete
        BEFORE DELETE ON legacy_ownership_migrations
        BEGIN
            SELECT RAISE(ABORT, 'legacy_ownership_migrations is append-only');
        END
        """,
    )
    for statement in triggers:
        cursor.execute(statement)


def _find_legacy_tenant(cursor: sqlite3.Cursor) -> str | None:
    row = cursor.execute(
        "SELECT id FROM users WHERE email = ?", (LEGACY_TENANT_EMAIL,)
    ).fetchone()
    return str(row[0]) if row else None


def _has_unowned_rows(cursor: sqlite3.Cursor) -> bool:
    """Return whether any pre-account table holds rows without an owner."""
    existing = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table in _UNOWNED_ROW_TABLES:
        if table not in existing:
            continue
        columns = {
            row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "user_id" in columns:
            query = f"SELECT 1 FROM {table} WHERE user_id IS NULL LIMIT 1"
        else:
            query = f"SELECT 1 FROM {table} LIMIT 1"
        if cursor.execute(query).fetchone():
            return True
    return False


def _prepare_legacy_tenant(cursor: sqlite3.Cursor, *, implicit: bool) -> str | None:
    """Resolve the holding owner for pre-account rows, or ``None``.

    * Explicit local mode: the holding account is the (active) local tenant.
    * Production: it is created only when unowned rows exist, and is held
      ``unclaimed`` so it can neither authenticate nor be scheduled. A holding account
      created as a normal account by an older release is demoted the same way.
      Its data is assigned to a verified owner by ``legacy_ownership``.
    """
    legacy = _find_legacy_tenant(cursor)
    now = _now_iso()
    if legacy is None:
        if not (implicit or _has_unowned_rows(cursor)):
            return None
        legacy = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO users "
            "(id, email, display_name, created_at, status, status_changed_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                legacy,
                LEGACY_TENANT_EMAIL,
                "Legacy Data",
                now,
                "active" if implicit else "unclaimed",
                now,
                now,
            ),
        )
    elif not implicit:
        cursor.execute(
            "UPDATE users SET status = 'unclaimed', status_changed_at = ?, "
            "updated_at = ? WHERE id = ? AND status = 'active'",
            (now, now, legacy),
        )
    return legacy


def _legacy_owner(legacy: str | None) -> str:
    if legacy is None:
        raise RuntimeError(
            "Pre-account rows were found but no legacy owner was prepared."
        )
    return legacy


def _assign_unowned(cursor: sqlite3.Cursor, table: str, legacy: str | None) -> None:
    """Give rows with no owner to the holding account (no-op when there is none)."""
    if legacy is None:
        return
    if table not in _UNOWNED_ROW_TABLES:
        raise ValueError(f"{table} is not a pre-account table.")
    cursor.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (legacy,))


def get_legacy_user_id(db_path: str = DEFAULT_DB_PATH) -> str:
    """Return the local single-user tenant id.

    Only available in the explicit non-production single-user mode. Production
    has no shared fallback tenant, so this raises :class:`TenantRequiredError`
    there even if a holding row exists.
    """
    if not implicit_single_user_allowed():
        raise TenantRequiredError(
            "The legacy single-user tenant is disabled outside an explicit "
            "non-production runtime."
        )
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email = ?", (LEGACY_TENANT_EMAIL,)
        ).fetchone()
    if not row:
        raise RuntimeError("Legacy user not found — run init_db first.")
    return row[0]


def get_current_day(
    db_path: str = DEFAULT_DB_PATH, *, user_id: str | None = None
) -> int:
    """Return the current day in the cycle (1-6)."""
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT current_day FROM programme_state WHERE user_id = ?",
            (uid,),
        ).fetchone()
    return int(row[0]) if row else 1


def advance_day(db_path: str = DEFAULT_DB_PATH, *, user_id: str | None = None) -> int:
    """Move to the next legacy rotation day, wrapping after six days."""
    uid = require_tenant(user_id, db_path)
    current = get_current_day(db_path, user_id=uid)
    nxt = current + 1 if current < LEGACY_ROUTINE_DAYS else 1
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO programme_state (user_id, current_day, split_name) "
            "VALUES (?, ?, COALESCE((SELECT split_name FROM programme_state WHERE user_id = ?), ?))",
            (uid, nxt, uid, CANONICAL_SPLIT_NAME),
        )
    return nxt


# ---------------------------------------------------------------------------
# Provenance and lineage (see docs/PROVENANCE.md)
# ---------------------------------------------------------------------------


def _record_derived(
    conn: sqlite3.Connection,
    user_id: str,
    record_type: RecordType,
    record_key: str,
    provenance: Provenance | None,
    content: Any,
) -> None:
    """Record a derived artefact's provenance inside the caller's transaction."""
    ledger.record_revision(
        conn,
        user_id=user_id,
        record_type=record_type,
        record_key=record_key,
        provenance=provenance or unattributed(RecordKind.DERIVED),
        content=content,
    )


def _public_view(entry: ledger.LedgerEntry | None) -> dict[str, Any] | None:
    """Owner-facing provenance view without the revision's retained content."""
    if entry is None:
        return None
    view = entry.view()
    view.pop("content", None)
    return view


def get_record_provenance(
    user_id: str,
    record_type: RecordType | str,
    record_key: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Return the current provenance view of one of the user's records."""
    with _connect(db_path) as conn:
        entry = ledger.current_entry(conn, user_id, record_type, record_key)
    return _public_view(entry)


def get_record_lineage(
    user_id: str,
    record_type: RecordType | str,
    record_key: str,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Return every revision of one record, newest first, with retained values."""
    with _connect(db_path) as conn:
        entries = ledger.lineage(conn, user_id, record_type, record_key)
    return [entry.view() for entry in entries]


def get_provenance_summary(
    user_id: str,
    record_type: RecordType | str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Summarise the current revisions of a record type by kind and source."""
    with _connect(db_path) as conn:
        return ledger.summarise(conn, user_id, record_type)


def get_programme_start_date(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> date:
    """Return the legacy programme start date, defaulting to today if unset."""

    value = get_meta("programme_start_date", db_path, user_id=user_id)
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc).date()


def save_workout(
    payload: Any,
    db_path: str = DEFAULT_DB_PATH,
    when: str | None = None,
    *,
    user_id: str | None = None,
    provenance: Provenance | None = None,
) -> None:
    """Persist a raw Hevy payload for historical reference.

    The Hevy workout id and start time become the record's external identifier
    and source timestamp unless the caller supplies explicit *provenance*.
    """
    if payload is None:
        return
    today = when or datetime.now(tz=timezone.utc).date().isoformat()
    uid = require_tenant(user_id, db_path)
    encoded = json.dumps(payload, sort_keys=True, default=str)
    incoming_ids = _workout_identifiers(payload)
    workout_id, started_at = workout_identity(payload)
    with _connect(db_path) as conn:
        # Repeated scheduled runs must not duplicate the same completed workout.
        # Payloads without a provider workout id keep the legacy append behaviour.
        if incoming_ids:
            rows = conn.execute(
                "SELECT id, hevy_payload FROM workout_history WHERE user_id = ?",
                (uid,),
            ).fetchall()
            for row_id, existing_payload in rows:
                try:
                    existing = _workout_identifiers(json.loads(existing_payload))
                except (TypeError, ValueError):
                    continue
                if existing and incoming_ids <= existing:
                    if existing_payload != encoded:
                        conn.execute(
                            "UPDATE workout_history SET date = ?, hevy_payload = ? "
                            "WHERE id = ? AND user_id = ?",
                            (today, encoded, row_id, uid),
                        )
                        ledger.record_revision(
                            conn,
                            user_id=uid,
                            record_type=RecordType.WORKOUT,
                            record_key=workout_id
                            or f"{today}:{content_hash(payload)[:12]}",
                            provenance=provenance
                            or Provenance.imported(
                                "hevy",
                                HEVY_PARSER_VERSION,
                                external_id=workout_id,
                                source_timestamp=started_at,
                            ),
                            content=payload,
                        )
                    return
        conn.execute(
            "INSERT INTO workout_history (date, hevy_payload, user_id) "
            "VALUES (?, ?, ?)",
            (today, encoded, uid),
        )
        ledger.record_revision(
            conn,
            user_id=uid,
            record_type=RecordType.WORKOUT,
            record_key=workout_id or f"{today}:{content_hash(payload)[:12]}",
            provenance=provenance
            or Provenance.imported(
                "hevy",
                HEVY_PARSER_VERSION,
                external_id=workout_id,
                source_timestamp=started_at,
            ),
            content=payload,
        )


def _workout_identifiers(payload: Any) -> set[str]:
    """Return Hevy workout ids from a single, list, or wrapped payload."""

    items: list[Any]
    if isinstance(payload, dict):
        if isinstance(payload.get("workouts"), list):
            items = payload["workouts"]
        elif payload.get("id") is not None:
            items = [payload]
        else:
            items = []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    identifiers: set[str] = set()
    for item in items:
        if isinstance(item, dict) and item.get("id") is not None:
            identifiers.add(str(item["id"]))
    return identifiers


def get_recent_hevy_logs(
    limit: int = 14, db_path: str = DEFAULT_DB_PATH, *, user_id: str | None = None
) -> list[dict[str, Any]]:
    """Return recent raw Hevy payloads for autonomous analysis."""
    uid = require_tenant(user_id, db_path)
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
    provenance: Provenance | None = None,
) -> None:
    """Persist the per-exercise top sets from a parsed workout summary.

    The progress is owned by *user_id* (required outside the explicit local mode).
    One provenance revision is recorded per workout (keyed by the Hevy workout id).

    ``date`` is the workout's calendar day in the owner's time zone, derived
    from the source instant that is persisted untouched as ``performed_at``.
    A later time-zone change re-derives ``date`` from that instant.
    """
    if summary is None:
        return
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        zone_row = conn.execute(
            "SELECT timezone FROM users WHERE id = ?", (uid,)
        ).fetchone()
        zone_name = zone_row[0] if zone_row else None
        day, performed_at = derive_local_day(summary.date, zone_name)
        today = day or local_today(resolve_timezone(zone_name)).isoformat()
        for exercise in summary.exercises:
            conn.execute(
                """
                INSERT INTO exercise_progress
                    (date, exercise_name, top_weight_kg, top_reps, sets, user_id,
                     performed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    today,
                    exercise.name,
                    exercise.top_weight_kg,
                    exercise.top_reps,
                    exercise.sets,
                    uid,
                    performed_at,
                ),
            )
        if summary.exercises:
            top_sets = [
                [e.name, e.top_weight_kg, e.top_reps, e.sets] for e in summary.exercises
            ]
            ledger.record_revision(
                conn,
                user_id=uid,
                record_type=RecordType.EXERCISE_PROGRESS,
                record_key=summary.workout_id
                or f"{today}:{content_hash(top_sets)[:12]}",
                provenance=provenance
                or Provenance.imported(
                    "hevy",
                    HEVY_PARSER_VERSION,
                    external_id=summary.workout_id,
                    source_timestamp=summary.date,
                ),
                content=top_sets,
            )


def get_recent_bests(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the most recently logged top set for each exercise by name.

    Results are always scoped to *user_id* (required outside the explicit local
    mode); there is no cross-tenant aggregate.
    """
    uid = require_tenant(user_id, db_path)
    # Performance Optimization (Bolt ⚡): Delegate the MAX(id) operation directly
    # into the main SELECT clause. SQLite guarantees un-aggregated columns correspond
    # to the MAX value, allowing direct use of idx_exercise_progress_user_name_id index
    # without slow subquery lookups.
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT exercise_name, top_weight_kg, top_reps, sets, date, MAX(id)
            FROM exercise_progress
            WHERE user_id = ?
            GROUP BY exercise_name
            ORDER BY exercise_name
            """,
            (uid,),
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

    Results are always scoped to *user_id* (required outside the explicit local
    mode).
    """
    uid = require_tenant(user_id, db_path)
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
                WHERE user_id = ?
            )
            WHERE rn <= ?
            ORDER BY exercise_name, id ASC
            """,
            (uid, limit_per_exercise),
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

    Results are always scoped to *user_id* (required outside the explicit local
    mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
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
            (uid,),
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

    Results are always scoped to *user_id* (required outside the explicit local
    mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
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
            (uid,),
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

    Results are always scoped to *user_id* (required outside the explicit local
    mode).
    """
    uid = require_tenant(user_id, db_path)
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
              AND user_id = ?
            GROUP BY exercise_name
            ORDER BY e1rm DESC
            """,
            (uid,),
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


def _require_history_user_id(user_id: str | None) -> str:
    """Return a non-empty tenant identifier or fail closed."""

    value = str(user_id or "").strip()
    if not value:
        raise ValueError("A tenant user_id is required for history records.")
    return value


def _fresh_matches_payload(matches: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in matches:
        if isinstance(raw, Mapping):
            rows.append(dict(raw))
            continue
        to_dict = getattr(raw, "to_dict", None)
        if callable(to_dict):
            rows.append(dict(to_dict()))
        else:
            raise TypeError("History matches must be mappings or typed records.")
    return rows


def save_routine_snapshots(
    user_id: str,
    snapshots: Iterable[Mapping[str, Any]],
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Persist immutable routine snapshots for one tenant.

    Existing snapshots are never mutated; a changed routine creates a new
    content-addressed snapshot ID instead.
    """

    uid = _require_history_user_id(user_id)
    saved = 0
    with _connect(db_path) as conn:
        for raw in snapshots:
            snapshot = dict(raw)
            snapshot_id = str(snapshot.get("id") or "").strip()
            routine_id = str(snapshot.get("routine_id") or "").strip()
            content_hash = str(snapshot.get("content_hash") or "").strip()
            if not snapshot_id or not routine_id or not content_hash:
                raise ValueError("Snapshot requires id, routine_id and content_hash.")
            cursor = conn.execute(
                """
                INSERT INTO hevy_routine_snapshots
                    (user_id, snapshot_id, routine_id, title, content_hash,
                     schema_version, updated_at, fetched_at,
                     superseded_snapshot_id, snapshot_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, snapshot_id) DO NOTHING
                """,
                (
                    uid,
                    snapshot_id,
                    routine_id,
                    str(snapshot.get("title") or ""),
                    content_hash,
                    int(snapshot.get("schema_version") or 1),
                    snapshot.get("updated_at"),
                    snapshot.get("fetched_at"),
                    snapshot.get("superseded_snapshot_id"),
                    json.dumps(snapshot, sort_keys=True, default=str),
                ),
            )
            saved += max(0, cursor.rowcount)
    return saved


def get_routine_snapshots(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    routine_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return only the requesting tenant's routine snapshots."""

    uid = _require_history_user_id(user_id)
    query = "SELECT snapshot_json FROM hevy_routine_snapshots WHERE user_id = ?"
    params: list[Any] = [uid]
    if routine_id is not None:
        query += " AND routine_id = ?"
        params.append(routine_id)
    query += " ORDER BY fetched_at, snapshot_id"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            logger.debug("Skipping unparseable routine snapshot row")
            continue
        if isinstance(payload, dict):
            snapshots.append(payload)
    return snapshots


def save_routine_workout_matches(
    user_id: str,
    matches: Iterable[Any],
    db_path: str = DEFAULT_DB_PATH,
    *,
    matched_at: str | None = None,
) -> int:
    """Persist deterministic match results, including user confirmation state."""

    uid = _require_history_user_id(user_id)
    timestamp = matched_at or datetime.now(tz=timezone.utc).isoformat()
    rows = _fresh_matches_payload(matches)
    saved = 0
    with _connect(db_path) as conn:
        for match in rows:
            workout_id = str(match.get("workout_id") or "").strip()
            if not workout_id:
                raise ValueError("A routine match requires a workout_id.")
            cursor = conn.execute(
                """
                INSERT INTO routine_workout_matches
                    (user_id, workout_id, routine_snapshot_id, routine_id,
                     score, confidence, features_json, method, matcher_version,
                     is_ambiguous, user_confirmed, matched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, workout_id, matcher_version) DO UPDATE SET
                    routine_snapshot_id = excluded.routine_snapshot_id,
                    routine_id = excluded.routine_id,
                    score = excluded.score,
                    confidence = excluded.confidence,
                    features_json = excluded.features_json,
                    method = excluded.method,
                    is_ambiguous = excluded.is_ambiguous,
                    user_confirmed = excluded.user_confirmed,
                    matched_at = excluded.matched_at
                """,
                (
                    uid,
                    workout_id,
                    match.get("routine_snapshot_id"),
                    match.get("routine_id"),
                    float(match.get("score") or 0.0),
                    str(match.get("confidence") or "unmatched"),
                    json.dumps(
                        match.get("features") or {},
                        sort_keys=True,
                        default=str,
                    ),
                    str(match.get("method") or "feature_similarity"),
                    str(match.get("matcher_version") or "unknown"),
                    1 if match.get("is_ambiguous") else 0,
                    1 if match.get("user_confirmed") else 0,
                    timestamp,
                ),
            )
            saved += max(0, cursor.rowcount)
    return saved


def get_routine_workout_matches(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    workout_id: str | None = None,
    routine_snapshot_id: str | None = None,
    confidence: str | None = None,
) -> list[dict[str, Any]]:
    """Return tenant-scoped workout matches with parsed feature scores."""

    uid = _require_history_user_id(user_id)
    query = "SELECT * FROM routine_workout_matches WHERE user_id = ?"
    params: list[Any] = [uid]
    if workout_id is not None:
        query += " AND workout_id = ?"
        params.append(workout_id)
    if routine_snapshot_id is not None:
        query += " AND routine_snapshot_id = ?"
        params.append(routine_snapshot_id)
    if confidence is not None:
        query += " AND confidence = ?"
        params.append(confidence)
    query += " ORDER BY matched_at DESC, workout_id"
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["features"] = json.loads(item.pop("features_json", "{}"))
        except (TypeError, ValueError):
            item["features"] = {}
        item["is_ambiguous"] = bool(item.get("is_ambiguous"))
        item["user_confirmed"] = bool(item.get("user_confirmed"))
        results.append(item)
    return results


def save_routine_match_override(
    user_id: str,
    *,
    pattern_type: str,
    pattern_value: str,
    routine_id: str,
    routine_snapshot_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Persist a user correction without mutating historical raw workouts."""

    uid = _require_history_user_id(user_id)
    allowed = {"workout", "title", "exercise_signature"}
    kind = pattern_type.strip().lower()
    if kind not in allowed:
        raise ValueError(f"Unsupported override pattern: {pattern_type}")
    value = pattern_value.strip()
    if kind == "title":
        from hevy_parser import normalise_name

        value = normalise_name(value)
    elif kind == "exercise_signature":
        value = "|".join(
            sorted(part.strip() for part in value.split("|") if part.strip())
        )
    if not value or not routine_id or not routine_snapshot_id:
        raise ValueError("Override requires a pattern value, routine and snapshot.")
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO routine_match_overrides
                (user_id, pattern_type, pattern_value, routine_id,
                 routine_snapshot_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, pattern_type, pattern_value) DO UPDATE SET
                routine_id = excluded.routine_id,
                routine_snapshot_id = excluded.routine_snapshot_id,
                created_at = excluded.created_at
            """,
            (
                uid,
                kind,
                value,
                routine_id,
                routine_snapshot_id,
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )


def get_routine_match_overrides(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Return tenant-scoped user matching corrections."""

    uid = _require_history_user_id(user_id)
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM routine_match_overrides WHERE user_id = ? "
            "ORDER BY created_at, pattern_type, pattern_value",
            (uid,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_routine_match_override(
    user_id: str,
    *,
    pattern_type: str,
    pattern_value: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Remove one tenant-scoped matching correction."""

    uid = _require_history_user_id(user_id)
    kind = pattern_type.strip().lower()
    value = pattern_value.strip()
    if kind == "title":
        from hevy_parser import normalise_name

        value = normalise_name(value)
    elif kind == "exercise_signature":
        value = "|".join(
            sorted(part.strip() for part in value.split("|") if part.strip())
        )
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM routine_match_overrides "
            "WHERE user_id = ? AND pattern_type = ? AND pattern_value = ?",
            (uid, kind, value),
        )


def save_exercise_baselines(
    user_id: str,
    baselines: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    db_path: str = DEFAULT_DB_PATH,
    *,
    created_at: str | None = None,
) -> int:
    """Persist derived exercise baselines for one tenant and rule version."""

    uid = _require_history_user_id(user_id)
    timestamp = created_at or datetime.now(tz=timezone.utc).isoformat()
    if isinstance(baselines, Mapping):
        rows = [dict(value) for value in baselines.values()]
    else:
        rows = [dict(value) for value in baselines]
    saved = 0
    with _connect(db_path) as conn:
        for baseline in rows:
            template_id = str(baseline.get("template_id") or "").strip()
            rule_version = str(baseline.get("rule_version") or "unknown")
            if not template_id:
                raise ValueError("A baseline requires a template_id.")
            window = baseline.get("evidence_window") or {}
            cursor = conn.execute(
                """
                INSERT INTO exercise_history_baselines
                    (user_id, template_id, rule_version, confidence,
                     evidence_start, evidence_end, baseline_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, template_id, rule_version) DO UPDATE SET
                    confidence = excluded.confidence,
                    evidence_start = excluded.evidence_start,
                    evidence_end = excluded.evidence_end,
                    baseline_json = excluded.baseline_json,
                    created_at = excluded.created_at
                """,
                (
                    uid,
                    template_id,
                    rule_version,
                    str(baseline.get("confidence") or "ambiguous"),
                    window.get("start") if isinstance(window, Mapping) else None,
                    window.get("end") if isinstance(window, Mapping) else None,
                    json.dumps(baseline, sort_keys=True, default=str),
                    timestamp,
                ),
            )
            saved += max(0, cursor.rowcount)
    return saved


def get_exercise_baselines(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    rule_version: str | None = None,
    template_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return tenant-scoped exercise baselines."""

    uid = _require_history_user_id(user_id)
    query = "SELECT baseline_json FROM exercise_history_baselines WHERE user_id = ?"
    params: list[Any] = [uid]
    if rule_version is not None:
        query += " AND rule_version = ?"
        params.append(rule_version)
    if template_id is not None:
        query += " AND template_id = ?"
        params.append(template_id)
    query += " ORDER BY template_id"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    baselines: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            logger.debug("Skipping unparseable exercise baseline row")
            continue
        if isinstance(payload, dict):
            baselines.append(payload)
    return baselines


def get_cached_workout_payloads(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    since: str | None = None,
) -> list[Any]:
    """Return the tenant's incremental local Hevy workout cache payloads."""

    uid = _require_history_user_id(user_id)
    query = "SELECT hevy_payload FROM workout_history WHERE user_id = ?"
    params: list[Any] = [uid]
    if since is not None:
        query += " AND date >= ?"
        params.append(since)
    query += " ORDER BY date ASC, id ASC"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    payloads: list[Any] = []
    for row in rows:
        try:
            payloads.append(json.loads(row[0]))
        except (TypeError, ValueError):
            logger.debug("Skipping unparseable cached workout row")
            continue
    return payloads


def get_routine_record(
    routine_key: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> tuple[str, str] | None:
    """Return (routine_id, content_hash) for a synced routine, or None.

    The lookup is always scoped to *user_id* (required outside the explicit local
    mode); another tenant's routine is never returned.
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT routine_id, content_hash FROM hevy_routines "
            "WHERE user_id = ? AND routine_key = ?",
            (uid, routine_key),
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

    The record is owned by *user_id* (required outside the explicit local mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO hevy_routines
                (user_id, routine_key, routine_id, content_hash)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, routine_key) DO UPDATE SET
                routine_id = excluded.routine_id,
                content_hash = excluded.content_hash
            """,
            (uid, routine_key, routine_id, content_hash),
        )


def get_meta(
    key: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> str | None:
    """Return a stored metadata value, or None if absent.

    The lookup is always scoped to *user_id* (required outside the explicit local
    mode); another tenant's value is never returned."""
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM hevy_meta WHERE user_id = ? AND key = ?",
            (uid, key),
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

    The value is owned by *user_id* (required outside the explicit local mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
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

    The delete is always scoped to *user_id* (required outside the explicit local
    mode); it can never remove another tenant's record.
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM hevy_routines WHERE user_id = ? AND routine_key = ?",
            (uid, routine_key),
        )


def save_checkin(
    number: int,
    when: str,
    workouts_done: int,
    weeks: int,
    message: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
    provenance: Provenance | None = None,
) -> None:
    """Persist a completed programme check-in for later review.

    The check-in is owned by *user_id* (required outside the explicit local mode);
    a row is never written without an owner. A check-in is a derived coaching
    summary, so it is recorded with derived provenance.
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO check_ins (number, date, workouts_done, weeks, message, user_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (number, when, workouts_done, weeks, message, uid),
        )
        _record_derived(
            conn,
            uid,
            RecordType.CHECK_IN,
            str(number),
            provenance,
            [number, when, workouts_done, weeks, message],
        )


def get_checkins(
    limit: int = 20,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent check-ins, most recent first.

    Results are always scoped to *user_id* (required outside the explicit local
    mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT number, date, workouts_done, weeks, message
            FROM check_ins
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (uid, limit),
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
    provenance: Provenance | None = None,
) -> None:
    """Log the full plan and lifestyle guidance issued for a day.

    One row per date per user: a re-run on the same day replaces the earlier
    entry so the log always holds the latest guidance that was sent. Each
    generation is a derived-provenance revision, so replacements stay in the
    record's lineage even though the superseded text is not retained.

    The log is owned by *user_id* (required outside the explicit local mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        _record_derived(
            conn,
            uid,
            RecordType.DAILY_PLAN,
            when,
            provenance,
            [day, focus, carb_tier, plan, lifestyle],
        )
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
    include_provenance: bool = False,
) -> list[dict[str, Any]]:
    """Return recent daily logs, most recent first.

    With *include_provenance* each log carries the ``provenance`` view of its
    current revision (``None`` for logs stored before provenance tracking).
    """
    uid = require_tenant(user_id, db_path)
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
        entries = (
            ledger.current_entries(
                conn, uid, RecordType.DAILY_PLAN, [row[0] for row in rows]
            )
            if include_provenance
            else {}
        )
    logs = [
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
    if include_provenance:
        for log in logs:
            log["provenance"] = _public_view(entries.get(log["date"]))
    return logs


BODY_METRIC_FIELDS = ("weight_kg", "body_fat_pct", "muscle_pct", "resting_hr", "hrv")
# Plausibility bounds for user-entered corrections (inclusive).
BODY_METRIC_LIMITS: dict[str, tuple[float, float]] = {
    "weight_kg": (20.0, 400.0),
    "body_fat_pct": (1.0, 75.0),
    "muscle_pct": (1.0, 90.0),
    "resting_hr": (25.0, 220.0),
    "hrv": (1.0, 400.0),
}


def validate_body_metric_changes(changes: Mapping[str, Any]) -> dict[str, float]:
    """Return clean, in-range numeric corrections or raise ``ValueError``."""
    clean: dict[str, float] = {}
    for field, raw in changes.items():
        if field not in BODY_METRIC_LIMITS:
            raise ValueError(f"{field} is not a correctable body metric")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{field} must be a number")  # noqa: TRY004 - API maps ValueError to 422
        value = float(raw)
        low, high = BODY_METRIC_LIMITS[field]
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{field} must be between {low:g} and {high:g}")
        clean[field] = round(value) if field == "resting_hr" else value
    if not clean:
        raise ValueError("at least one body metric is required")
    return clean


def _body_content(
    values: Mapping[str, Any], sources: Mapping[str, str]
) -> dict[str, Any]:
    """Logical value of a body-metric revision, retained for lineage."""
    return {
        "values": {k: values[k] for k in BODY_METRIC_FIELDS if values.get(k) is not None},
        "field_sources": {k: sources[k] for k in BODY_METRIC_FIELDS if k in sources},
    }


def _replace_body_metrics_row(
    conn: sqlite3.Connection,
    user_id: str | None,
    when: str,
    values: Mapping[str, Any],
    *,
    source: str = "legacy",
) -> None:
    """One current row per date and owner; superseded values live in the ledger.

    *source* records the connector that supplied the current reading so a
    connector-scoped purge can remove only its own imported rows.
    """
    conn.execute(
        "DELETE FROM body_metrics WHERE date = ? AND user_id IS ?", (when, user_id)
    )
    conn.execute(
        """
        INSERT INTO body_metrics
            (date, weight_kg, body_fat_pct, muscle_pct, resting_hr, hrv, user_id,
             source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            when,
            *(values.get(field) for field in BODY_METRIC_FIELDS),
            user_id,
            source.strip() or "legacy",
        ),
    )


def _current_body_values(
    conn: sqlite3.Connection, user_id: str | None, when: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT weight_kg, body_fat_pct, muscle_pct, resting_hr, hrv
        FROM body_metrics WHERE date = ? AND user_id IS ?
        ORDER BY id DESC LIMIT 1
        """,
        (when, user_id),
    ).fetchone()
    if row is None:
        return None
    return {f: v for f, v in zip(BODY_METRIC_FIELDS, row) if v is not None}


def save_body_metrics(
    metrics: dict[str, Any] | None,
    when: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
    provenance: Provenance | None = None,
    field_sources: Mapping[str, str] | None = None,
) -> None:
    """Persist a body-composition reading (weight, body fat, muscle, resting HR).

    Accepts the dict produced by `health_connect.body_metrics_from_recovery`, or
    None (a no-op). One row per date per owner: a later reading on the same day
    replaces the earlier one, so the morning weigh-in is what gets stored. The
    replaced values are never lost: each reading is a provenance revision that
    retains its values, and a user-entered correction for the date takes
    precedence over a later automated import (the import is kept as a held
    revision and the stored row is left untouched).

    *provenance* names the source of the reading and *field_sources* optionally
    attributes individual fields when a row merges several sources. The
    declared source is also recorded on the current row so a connector-specific
    purge can remove only that connector's imported readings.

    The metrics are owned by *user_id* (required outside the explicit local
    mode). A same-day replacement only ever removes that user's own reading.
    """
    if not metrics:
        return
    uid = require_tenant(user_id, db_path)
    when = when or datetime.now(tz=timezone.utc).date().isoformat()
    declared = provenance or unattributed(RecordKind.IMPORTED)
    values = {f: metrics[f] for f in BODY_METRIC_FIELDS if metrics.get(f) is not None}
    sources = {f: (field_sources or {}).get(f, declared.source) for f in values}
    with _connect(db_path) as conn:
        ledger.begin_write(conn)
        existing = _current_body_values(conn, uid, when)
        if existing is not None:
            ledger.ensure_baseline(
                conn,
                user_id=uid,
                record_type=RecordType.BODY_METRIC,
                record_key=when,
                content=_body_content(
                    existing, {f: UNATTRIBUTED_SOURCE for f in existing}
                ),
                retain_content=True,
            )
        outcome = ledger.record_revision(
            conn,
            user_id=uid,
            record_type=RecordType.BODY_METRIC,
            record_key=when,
            provenance=declared,
            content=_body_content(values, sources),
            retain_content=True,
        )
        if not outcome.applied:
            logger.info(
                "Kept the user-entered body metrics for %s; automated import held.", when
            )
            return
        _replace_body_metrics_row(conn, uid, when, values, source=declared.source)


def correct_body_metrics(
    user_id: str,
    when: str,
    changes: Mapping[str, Any],
    *,
    reason: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Apply a user correction (or manual entry) to the reading for *when*.

    The changed fields are merged into the current reading, the result becomes
    a user-entered revision that supersedes the previous one, and the previous
    values stay in the lineage. Raises ``ValueError`` for invalid input or a
    correction that would not change the stored reading.
    """
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id is required")
    try:
        day = date.fromisoformat(when)
    except (TypeError, ValueError) as exc:
        raise ValueError("date must be a YYYY-MM-DD calendar date") from exc
    if day > datetime.now(tz=timezone.utc).date() + timedelta(days=1):
        raise ValueError("date cannot be in the future")
    when = day.isoformat()
    clean = validate_body_metric_changes(changes)

    with _connect(db_path) as conn:
        ledger.begin_write(conn)
        existing = _current_body_values(conn, user_id, when)
        previous = existing or {}
        merged = {**previous, **clean}
        if existing is not None and merged == previous:
            raise ValueError("The correction does not change the stored reading.")
        current = ledger.current_entry(conn, user_id, RecordType.BODY_METRIC, when)
        if existing is not None and current is None:
            ledger.ensure_baseline(
                conn,
                user_id=user_id,
                record_type=RecordType.BODY_METRIC,
                record_key=when,
                content=_body_content(
                    previous, {f: UNATTRIBUTED_SOURCE for f in previous}
                ),
                retain_content=True,
            )
            current = ledger.current_entry(conn, user_id, RecordType.BODY_METRIC, when)
        carried = (
            dict((current.content or {}).get("field_sources") or {})
            if current is not None
            else {}
        )
        fallback = current.provenance.source if current is not None else MANUAL_SOURCE
        sources = {f: carried.get(f, fallback) for f in previous}
        sources.update({f: MANUAL_SOURCE for f in clean})
        content = _body_content(merged, sources)
        content["corrected_fields"] = sorted(clean)
        outcome = ledger.record_revision(
            conn,
            user_id=user_id,
            record_type=RecordType.BODY_METRIC,
            record_key=when,
            provenance=Provenance.user_entered(),
            content=content,
            retain_content=True,
            note=reason,
        )
        _replace_body_metrics_row(conn, user_id, when, merged)
        entry = ledger.current_entry(conn, user_id, RecordType.BODY_METRIC, when)
    return {
        "date": when,
        "reading": {f: merged.get(f) for f in BODY_METRIC_FIELDS},
        "revision": outcome.revision,
        "provenance": entry.view() if entry is not None else None,
    }


def get_body_metrics(
    limit: int = 60,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
    include_provenance: bool = False,
) -> list[dict[str, Any]]:
    """Return recent body-composition readings, oldest first for charting.

    Results are always scoped to *user_id* (required outside the explicit local
    mode). With *include_provenance* every reading carries a ``provenance`` view
    of its current revision, or ``None`` when it predates provenance tracking.
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
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
            (uid, limit),
        ).fetchall()
        entries = (
            ledger.current_entries(
                conn, uid, RecordType.BODY_METRIC, [row[0] for row in rows]
            )
            if include_provenance
            else {}
        )
    readings = [
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
    if include_provenance:
        for reading in readings:
            entry = entries.get(reading["date"])
            reading["provenance"] = _public_view(entry)
    return readings


def save_dashboard_insight(
    insight_json: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
    provenance: Provenance | None = None,
) -> None:
    """Save the daily dashboard insight JSON.

    The insight is owned by *user_id* (required outside the explicit local mode);
    it is a derived recommendation, so each regeneration is a new provenance
    revision.
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        _record_derived(
            conn, uid, RecordType.DASHBOARD_INSIGHT, "current", provenance, insight_json
        )
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

    Only *user_id*'s insight is ever returned (required outside the explicit
    local mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT insight_json FROM dashboard_insights WHERE user_id = ?",
            (uid,),
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
    provenance: Provenance | None = None,
) -> None:
    """Save an AI reasoning log for an exercise change.

    The log is owned by *user_id* (required outside the explicit local mode). The
    explanation is a derived record; it is never evidence of what the user did.
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        _record_derived(
            conn, uid, RecordType.REASONING, context_id, provenance, reasoning
        )
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

    Only *user_id*'s log is ever returned (required outside the explicit local
    mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT reasoning FROM reasoning_logs
            WHERE context_id = ? AND user_id = ?
            """,
            (context_id, uid),
        ).fetchone()
    return row[0] if row else None


def save_deep_correlation(
    insight_markdown: str,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
    provenance: Provenance | None = None,
) -> None:
    """Save the weekly deep-correlation report, owned by *user_id*."""
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        _record_derived(
            conn,
            uid,
            RecordType.DEEP_CORRELATION,
            "current",
            provenance,
            insight_markdown,
        )
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
    """Return the latest deep correlation for *user_id* only."""
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT insight_markdown FROM deep_correlations WHERE user_id = ?",
            (uid,),
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

    The message is owned by *user_id* (required outside the explicit local mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (role, content, created_at, user_id)
            VALUES (?, ?, ?, ?)
            """,
            (role, content, _now_iso(), uid),
        )


def get_chat_messages(
    limit: int = 50,
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return chat messages, oldest first.

    Results are always scoped to *user_id* (required outside the explicit local
    mode).
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
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
            (uid, limit),
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
    """Delete all chat messages for *user_id*.

    There is no clear-everything mode: the delete is always tenant scoped.
    """
    uid = require_tenant(user_id, db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (uid,))


# ---------------------------------------------------------------------------
# Multi-user management (Sprint 1)
# ---------------------------------------------------------------------------


def get_or_create_user(
    email: str,
    display_name: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> UserRow:
    """Return the account for an email, creating an ``active`` one if absent.

    This is a provisioning helper: the account it creates has no external
    identity. Interactive sign-in must go through
    :func:`accounts.resolve_oauth_user`, which binds the provider's immutable
    subject to the account. The returned row is returned regardless of its
    ``status``; callers that act for the user must check it. The reserved
    legacy holding address can never be provisioned this way.
    """
    canonical = normalise_email(email)
    if not canonical:
        raise ValueError("email is required.")
    if canonical == LEGACY_TENANT_EMAIL:
        raise ValueError("The legacy holding address cannot be provisioned.")

    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE lower(email) = ?",
            (canonical,),
        ).fetchone()
        if row:
            return _user_from_row(row)

        user_id = str(uuid.uuid4())
        now = _now_iso()
        conn.execute(
            """
            INSERT INTO users
                (id, email, display_name, created_at, status_changed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, canonical, display_name, now, now, now),
        )
        created = conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _user_from_row(created)


def get_user_by_id(user_id: str, db_path: str = DEFAULT_DB_PATH) -> UserRow | None:
    """Return the user row for a user_id, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _user_from_row(row) if row else None


def get_all_users(
    db_path: str = DEFAULT_DB_PATH,
    *,
    include_closing: bool = False,
    include_inactive: bool = False,
) -> list[UserRow]:
    """Return the accounts a background job may act for, oldest first.

    Only ``active`` accounts are returned by default, so the scheduler never
    processes a suspended, deleted, retired or unclaimed holding account.
    Accounts with an open deletion request (pending, in progress or failed) are
    also excluded by default so no coaching run, insight job or sync is
    dispatched for a user who has asked for their data to be erased. Pass
    ``include_inactive`` or ``include_closing`` for administrative listings.
    """
    query = f"SELECT {_USER_COLUMNS} FROM users"
    conditions: list[str] = []
    if not include_inactive:
        conditions.append("status = 'active'")
    if not include_closing:
        conditions.append(
            "id NOT IN (SELECT user_id FROM account_deletion_requests "
            "WHERE state IN ('pending', 'in_progress', 'failed'))"
        )
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at, id"
    with _connect(db_path) as conn:
        rows = conn.execute(query).fetchall()
    return [_user_from_row(row) for row in rows]


# ---- API key management ----


_CREDENTIAL_FIELDS = ("api_key", "client_secret", "refresh_token")


def _read_credential_field(
    master: encryption.MasterKey,
    *,
    user_id: str,
    provider: str,
    field_name: str,
    value: str,
) -> str:
    """Decrypt a vaulted field, tolerating a pre-vault Fernet/plain value."""
    if encryption.is_sealed(value):
        return encryption.open_sealed(
            master,
            user_id=user_id,
            provider=provider,
            field_name=field_name,
            token=value,
        )
    return encryption.read_legacy_value(master, value)


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
    """Store (or update) a per-user encrypted API key for a provider pair."""
    provider_key = provider.lower()
    master = encryption.load_master_key()
    now = _now_iso()
    sealed = {
        "api_key": (
            encryption.seal(
                master,
                user_id=user_id,
                provider=provider_key,
                field_name="api_key",
                plaintext=api_key,
            )
            if api_key
            else ""
        ),
        "client_secret": (
            encryption.seal(
                master,
                user_id=user_id,
                provider=provider_key,
                field_name="client_secret",
                plaintext=client_secret,
            )
            if client_secret
            else None
        ),
        "refresh_token": (
            encryption.seal(
                master,
                user_id=user_id,
                provider=provider_key,
                field_name="refresh_token",
                plaintext=refresh_token,
            )
            if refresh_token
            else None
        ),
    }
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
                provider_key,
                sealed["api_key"],
                client_id,
                sealed["client_secret"],
                sealed["refresh_token"],
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
    provider_key = provider.lower()
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT api_key, client_id, client_secret, refresh_token,
                   extra_json, updated_at
            FROM user_api_keys
            WHERE user_id = ? AND provider = ?
            """,
            (user_id, provider_key),
        ).fetchone()
    if not row:
        return None
    master = encryption.load_master_key()
    return {
        "api_key": (
            _read_credential_field(
                master,
                user_id=user_id,
                provider=provider_key,
                field_name="api_key",
                value=row[0],
            )
            if row[0]
            else ""
        ),
        "client_id": row[1],
        "client_secret": (
            _read_credential_field(
                master,
                user_id=user_id,
                provider=provider_key,
                field_name="client_secret",
                value=row[2],
            )
            if row[2]
            else None
        ),
        "refresh_token": (
            _read_credential_field(
                master,
                user_id=user_id,
                provider=provider_key,
                field_name="refresh_token",
                value=row[3],
            )
            if row[3]
            else None
        ),
        "extra": json.loads(row[4]) if row[4] else None,
        "updated_at": row[5],
    }


def get_user_api_keys(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, dict[str, Any]]:
    """Return all decrypted API key records for a user, keyed by provider."""
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

    master = encryption.load_master_key() if rows else None
    result: dict[str, dict[str, Any]] = {}
    for provider, key, cid, csecret, rtoken, extra, updated in rows:
        assert master is not None
        result[provider] = {
            "api_key": (
                _read_credential_field(
                    master,
                    user_id=user_id,
                    provider=provider,
                    field_name="api_key",
                    value=key,
                )
                if key
                else ""
            ),
            "client_id": cid,
            "client_secret": (
                _read_credential_field(
                    master,
                    user_id=user_id,
                    provider=provider,
                    field_name="client_secret",
                    value=csecret,
                )
                if csecret
                else None
            ),
            "refresh_token": (
                _read_credential_field(
                    master,
                    user_id=user_id,
                    provider=provider,
                    field_name="refresh_token",
                    value=rtoken,
                )
                if rtoken
                else None
            ),
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
ACCOUNT_STATUS_UNCLAIMED = "unclaimed"
ACCOUNT_STATUS_RETIRED = "retired"
ACCOUNT_STATUS_DELETED = "deleted"

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
            "INSERT INTO users "
            "(id, email, display_name, created_at, status_changed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, email, display_name, moment, moment, moment),
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
            "updated_at = ?, session_epoch = session_epoch + 1 WHERE id = ?",
            (status, moment, moment, user_id),
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


# ---- Verified Hevy connection metadata ----


def save_hevy_connection(
    user_id: str,
    *,
    state: str,
    api_version: str,
    capabilities: dict[str, Any],
    tested_at: str,
    account_username: str | None = None,
    account_id: str | None = None,
    provider: str = "hevy",
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Record the outcome of a Hevy credential verification for one user.

    Only safe metadata is stored: the connected account identity, the test
    time, the API version and the verified capability map. Credential material
    is never written here.
    """
    now = datetime.now(tz=timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO hevy_connections
                (user_id, provider, state, account_username, account_id,
                 api_version, capabilities_json, tested_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                state = excluded.state,
                account_username = excluded.account_username,
                account_id = excluded.account_id,
                api_version = excluded.api_version,
                capabilities_json = excluded.capabilities_json,
                tested_at = excluded.tested_at,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                provider.lower(),
                state,
                account_username,
                account_id,
                api_version,
                json.dumps(capabilities),
                tested_at,
                now,
                now,
            ),
        )


def get_hevy_connection(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
    provider: str = "hevy",
) -> dict[str, Any] | None:
    """Return the stored Hevy connection metadata for one user, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT state, account_username, account_id, api_version,
                   capabilities_json, tested_at, updated_at
            FROM hevy_connections
            WHERE user_id = ? AND provider = ?
            """,
            (user_id, provider.lower()),
        ).fetchone()
    if not row:
        return None
    try:
        capabilities = json.loads(row[4]) if row[4] else {}
    except json.JSONDecodeError:
        capabilities = {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    return {
        "state": row[0],
        "account_username": row[1],
        "account_id": row[2],
        "api_version": row[3],
        "capabilities": capabilities,
        "tested_at": row[5],
        "updated_at": row[6],
    }


def delete_hevy_connection(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
    provider: str = "hevy",
) -> None:
    """Remove the stored Hevy connection metadata for one user."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM hevy_connections WHERE user_id = ? AND provider = ?",
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
    training_rules: dict[str, Any] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Save or update a user's training preferences."""
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
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
        if training_rules is not None:
            rules_obj = StructuredTrainingRules.from_raw(training_rules)
            _save_training_rules_with_connection(conn, user_id, rules_obj)
        if constraints is not None or training_rules is not None:
            _deactivate_invalid_active_programme(conn, user_id)


def _training_rules_from_connection(
    conn: sqlite3.Connection,
    user_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT version, rules_json, updated_at
        FROM training_rules
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    if row:
        try:
            data = json.loads(row[1]) if row[1] else {}
            data["version"] = row[0]
            data["updated_at"] = row[2]
            return StructuredTrainingRules.model_validate(data).model_dump()
        except (ValueError, TypeError, json.JSONDecodeError):
            logger.warning(
                "Failed to deserialize training_rules for %s; regenerating default.",
                user_id,
            )

    pref_row = conn.execute(
        "SELECT constraints FROM user_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if pref_row and pref_row[0]:
        try:
            legacy = json.loads(pref_row[0])
            if isinstance(legacy, list):
                return StructuredTrainingRules.from_raw(legacy).model_dump()
        except (ValueError, TypeError, json.JSONDecodeError):
            logger.debug("Could not parse legacy constraints for %s", user_id)
    return StructuredTrainingRules(version=1).model_dump()


def _profile_constraints_from_connection(
    conn: sqlite3.Connection,
    user_id: str,
) -> Any:
    row = conn.execute(
        "SELECT constraints FROM user_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return []


def _save_training_rules_with_connection(
    conn: sqlite3.Connection,
    user_id: str,
    rules_obj: StructuredTrainingRules,
) -> dict[str, Any]:
    from datetime import UTC, datetime

    stored_rules = rules_obj.model_copy(deep=True)
    existing = conn.execute(
        "SELECT version FROM training_rules WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    next_version = (existing[0] + 1) if existing else max(1, stored_rules.version)
    stored_rules.version = next_version
    now = datetime.now(tz=UTC).isoformat()
    dump = stored_rules.model_dump()
    conn.execute(
        """
        INSERT INTO training_rules (user_id, version, rules_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            version = excluded.version,
            rules_json = excluded.rules_json,
            updated_at = excluded.updated_at
        """,
        (user_id, next_version, json.dumps(dump), now),
    )
    dump["updated_at"] = now
    return dump


def _deactivate_invalid_active_programme(
    conn: sqlite3.Connection,
    user_id: str,
) -> None:
    row = conn.execute(
        "SELECT definition FROM programmes WHERE user_id = ? AND active = 1",
        (user_id,),
    ).fetchone()
    if not row:
        return

    from programme_validator import validate_programme

    try:
        definition = json.loads(row[0]) if row[0] else {}
    except (TypeError, json.JSONDecodeError):
        definition = {}
    validation = validate_programme(
        definition,
        profile_rules=_profile_constraints_from_connection(conn, user_id),
        training_rules=_training_rules_from_connection(conn, user_id),
    )
    if not definition or validation.blocking_violations:
        conn.execute(
            "UPDATE programmes SET active = 0 WHERE user_id = ? AND active = 1",
            (user_id,),
        )


def get_training_rules(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return the user's structured training rules, migrating legacy constraints if needed."""
    try:
        with _connect(db_path) as conn:
            return _training_rules_from_connection(conn, user_id)
    except sqlite3.OperationalError:
        return StructuredTrainingRules(version=1).model_dump()


def save_training_rules(
    user_id: str,
    rules_input: dict[str, Any] | StructuredTrainingRules,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Save or update structured training rules for a user with version bump."""
    rules_obj = (
        rules_input
        if isinstance(rules_input, StructuredTrainingRules)
        else StructuredTrainingRules.model_validate(rules_input)
    )
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        dump = _save_training_rules_with_connection(conn, user_id, rules_obj)
        _deactivate_invalid_active_programme(conn, user_id)
        return dump


def get_user_preferences(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return the user's training preferences, with defaults for unset fields."""
    defaults: dict[str, Any] = {
        "goals": ["hypertrophy"],
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
        defaults["training_rules"] = get_training_rules(user_id, db_path)
        return defaults
    loaded_goals = json.loads(row[0]) if row[0] else []
    res = {
        "goals": loaded_goals if loaded_goals else ["hypertrophy"],
        "constraints": json.loads(row[1]) if row[1] else [],
        "experience_level": row[2] or "intermediate",
        "coaching_style": row[3] or "direct",
        "preferred_ai": row[4] or "gemini",
        "ai_model": row[5],
        "custom_rules": json.loads(row[6]) if row[6] else [],
    }
    res["training_rules"] = get_training_rules(user_id, db_path)
    return res


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

    from programme_validator import assert_programme_activatable

    if definition.get("blocking_violations"):
        first = definition["blocking_violations"][0]
        raise ValueError(
            f"Cannot activate programme with blocking violations: {first.get('exercise')} "
            f"violates '{first.get('rule')}'. Permitted remediation: {first.get('permitted_remediation')}"
        )

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert_programme_activatable(
            definition,
            profile_rules=_profile_constraints_from_connection(conn, user_id),
            training_rules=_training_rules_from_connection(conn, user_id),
        )
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
        row = conn.execute(
            "SELECT id FROM programmes WHERE user_id = ? AND template_key = ?",
            (user_id, template_key),
        ).fetchone()
        new_id = row[0] if row else None
        if new_id is not None:
            conn.execute(
                """
                INSERT INTO programme_decisions
                    (user_id, programme_id, source, template_key, decision, reason, created_at)
                VALUES (?, ?, ?, ?, 'authoritative', 'user_activation', ?)
                """,
                (user_id, new_id, source, template_key, now),
            )
            if isinstance(definition, dict) and definition.get("name"):
                conn.execute(
                    "UPDATE programme_state SET split_name = ? WHERE user_id = ?",
                    (definition["name"], user_id),
                )


def get_authoritative_programme_decision(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Return the recorded authoritative programme decision for a user."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, user_id, programme_id, source, template_key, decision, reason, created_at
            FROM programme_decisions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "authoritative_programme_id": row[2],
        "source": row[3],
        "template_key": row[4],
        "decision": row[5],
        "reason": row[6],
        "decided_at": row[7],
    }


def seed_canonical_programme(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
    start_date: date | None = None,
) -> int:
    """Seed the canonical programme definition for a user and record authoritative decision."""
    canonical_def = get_canonical_programme_definition(start_date=start_date)
    set_active_programme(
        user_id=user_id,
        source="hevy",
        template_key=CANONICAL_TEMPLATE_KEY,
        definition=canonical_def,
        db_path=db_path,
    )
    if start_date:
        set_meta("programme_start_date", start_date.isoformat(), db_path, user_id=user_id)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM programmes WHERE user_id = ? AND template_key = ?",
            (user_id, CANONICAL_TEMPLATE_KEY),
        ).fetchone()
        prog_id = row[0] if row else 0
        conn.execute(
            """
            INSERT INTO programme_decisions
                (user_id, programme_id, source, template_key, decision, reason, created_at)
            VALUES (?, ?, 'hevy', ?, 'authoritative', 'canonical_seed', ?)
            """,
            (user_id, prog_id, CANONICAL_TEMPLATE_KEY, datetime.now(tz=timezone.utc).isoformat()),
        )
    return prog_id


def activate_programme_definition(
    user_id: str,
    source: str,
    template_key: str,
    definition: dict[str, Any],
    *,
    start_date: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Activate a programme definition and its start date atomically.

    The programme row and the ``programme_start_date`` metadata are written in
    a single transaction so a failed activation never leaves a programme
    without its start date (or the other way round).
    """
    if source == "template":
        raise ValueError(
            "Static programme templates have been removed; activate a Hevy-derived definition."
        )
    if not isinstance(definition, dict) or not definition:
        raise ValueError("A non-empty Hevy-derived programme definition is required.")

    now = datetime.now(tz=timezone.utc).isoformat()
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
        if start_date:
            conn.execute(
                """
                INSERT INTO hevy_meta (user_id, key, value) VALUES (?, ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
                """,
                (user_id, "programme_start_date", start_date),
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
