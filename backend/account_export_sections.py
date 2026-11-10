"""Reviewed, tenant-scoped collections that make up the account data export.

This module is the export's disclosure boundary. Nothing reaches an export unless
it is selected by one of the explicit queries below and mapped through a reviewed
record builder, so a new table or column is never exported by accident.

Rules every collection follows (enforced at import time and by tests):

* the query is a literal string whose first parameter is the owning account's id;
  the runner always supplies that id, never a caller-chosen value;
* credential columns are never selected - a connector row reports only whether a
  credential is configured, computed inside SQLite so the secret never reaches
  Python;
* free-form JSON blobs are copied with credential-class keys removed at any depth;
* derived data (AI output, computed progress, generated plans) declares its
  provenance - where it came from, what produced it and what it was derived
  from - and every collection carries a schema version.

``docs/ACCOUNT_DATA_EXPORT.md`` describes how to add a collection.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from data_classification import DataClass, classify_field

ORIGIN_USER_ENTERED = "user_entered"
ORIGIN_CONNECTOR_IMPORT = "connector_import"
ORIGIN_SYSTEM_GENERATED = "system_generated"
ORIGIN_DERIVED = "derived_computation"
ORIGIN_AI_GENERATED = "ai_generated"
ORIGIN_MIXED = "mixed"

_ORIGINS = frozenset(
    {
        ORIGIN_USER_ENTERED,
        ORIGIN_CONNECTOR_IMPORT,
        ORIGIN_SYSTEM_GENERATED,
        ORIGIN_DERIVED,
        ORIGIN_AI_GENERATED,
        ORIGIN_MIXED,
    }
)
# Origins whose records were produced from other data and therefore must name
# the generator and the inputs they were derived from.
DERIVED_ORIGINS = frozenset({ORIGIN_DERIVED, ORIGIN_AI_GENERATED})

_FETCH_BATCH = 500
_MAX_BLOB_DEPTH = 64

# Connector sync-state keys that may be exported. ``hevy_meta`` also stores OAuth
# state and a Google Health refresh token, so unknown keys fail closed.
SYNC_STATE_KEYS = (
    "programme_start_date",
    "routine_folder_id",
    "checkin_number",
    "last_checkin_date",
    "last_checkin_workout_count",
)

EXCLUSIONS: tuple[dict[str, str], ...] = (
    {
        "category": "credentials",
        "description": (
            "API keys, OAuth client secrets and refresh tokens, push-subscription "
            "keys, session secrets and encryption keys are never exported. "
            "Connector records only state whether a credential is configured."
        ),
    },
    {
        "category": "internal_security_data",
        "description": (
            "CSRF tokens, rate-limit counters, session data, system prompts and "
            "operator diagnostics are not account data and are not exported."
        ),
    },
    {
        "category": "other_accounts",
        "description": (
            "Records that belong to any other account are never included. Every "
            "collection is read with the authenticated account's identifier."
        ),
    },
)

# Tables that are deliberately not exported, with the reason. A test fails when a
# table exists that is neither read by a collection below nor listed here, so a
# new user-owned table cannot silently be missed from the export.
NOT_EXPORTED_TABLES: dict[str, str] = {
    "account_exports": (
        "Bookkeeping for the export itself; each export is recorded in "
        "account_audit_events instead."
    ),
    "csrf_tokens": "Internal security data: hashed, single-use CSRF nonces.",
    "rate_limits": "Internal security data: per-address request counters, not account data.",
}

SECTIONS: tuple[tuple[str, str], ...] = (
    ("profile", "Profile"),
    ("programmes", "Programmes"),
    ("workouts", "Workouts"),
    ("metrics", "Metrics"),
    ("check_ins", "Check-ins"),
    ("connectors", "Connector metadata"),
    ("notifications", "Notifications"),
    ("coaching", "Coaching history"),
    ("audit", "Audit events"),
)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a collection's records came from."""

    origin: str
    source: str
    generator: str | None = None
    derived_from: tuple[str, ...] = ()
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.origin not in _ORIGINS:
            raise ValueError(f"unknown provenance origin: {self.origin}")
        if not self.source.strip():
            raise ValueError("provenance requires a source description")
        if self.origin in DERIVED_ORIGINS and (
            not self.generator or not self.derived_from
        ):
            raise ValueError("derived data must name its generator and inputs")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"origin": self.origin, "source": self.source}
        if self.generator:
            result["generator"] = self.generator
        if self.derived_from:
            result["derived_from"] = list(self.derived_from)
        if self.notes:
            result["notes"] = self.notes
        return result


@dataclass(frozen=True, slots=True)
class Collection:
    """One exported record list: a reviewed query plus a reviewed record builder."""

    section: str
    name: str
    description: str
    schema_version: int
    provenance: Provenance
    sql: str
    to_record: Callable[[tuple[Any, ...]], dict[str, Any] | None]
    params_after_owner: tuple[Any, ...] = ()
    owner_column: str = "user_id"

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("collection schema versions start at 1")
        if f"{self.owner_column} = ?" not in self.sql:
            raise ValueError(f"{self.section}/{self.name} is not owner-scoped")
        if self.sql.count("?") != 1 + len(self.params_after_owner):
            raise ValueError(f"{self.section}/{self.name} parameter count mismatch")

    @property
    def path(self) -> str:
        return f"{self.section}/{self.name}.json"

    @property
    def key(self) -> str:
        return f"{self.section}.{self.name}"


# ---------------------------------------------------------------------------
# Free-form data helpers
# ---------------------------------------------------------------------------


def scrub_credentials(value: Any, _depth: int = 0) -> Any:
    """Return a copy of JSON-like *value* without credential-class keys.

    Keys are classified by the central data-classification policy at every depth,
    so a provider payload or programme definition that embeds a token-like field
    cannot carry it into an export.
    """

    if _depth > _MAX_BLOB_DEPTH:
        raise ValueError("document is nested too deeply to export")
    if isinstance(value, Mapping):
        return {
            str(key): scrub_credentials(item, _depth + 1)
            for key, item in value.items()
            if classify_field(str(key)) is not DataClass.CREDENTIAL
        }
    if isinstance(value, (list, tuple)):
        return [scrub_credentials(item, _depth + 1) for item in value]
    return value


def find_credential_keys(value: Any, path: str = "$", _depth: int = 0) -> list[str]:
    """Return paths of credential-class keys still present in *value*."""

    if _depth > _MAX_BLOB_DEPTH:
        return [f"{path} (nested too deeply)"]
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}"
            if classify_field(str(key)) is DataClass.CREDENTIAL:
                found.append(child)
            found.extend(find_credential_keys(item, child, _depth + 1))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(find_credential_keys(item, f"{path}[{index}]", _depth + 1))
    return found


def _decode_blob(text: Any) -> tuple[Any, str | None]:
    """Decode a stored JSON document, scrubbed; ``(None, code)`` if unreadable."""

    if text is None or text == "":
        return None, None
    try:
        return scrub_credentials(json.loads(text)), None
    except (ValueError, RecursionError):
        return None, "unreadable_json"


def _decode_list(text: Any) -> list[Any]:
    value, _error = _decode_blob(text)
    return value if isinstance(value, list) else []


def _endpoint_origin(endpoint: Any) -> str | None:
    """Reduce a push endpoint to scheme and host; its path is a bearer capability."""

    if not isinstance(endpoint, str):
        return None
    try:
        parts = urlsplit(endpoint)
        hostname = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not hostname:
        return None
    return f"{parts.scheme}://{hostname}"


def _configured_model(text: Any) -> str | None:
    """Return only the model name from a connector's free-form extra settings."""

    if not text:
        return None
    try:
        extra = json.loads(text)
    except (ValueError, RecursionError):
        return None
    model = extra.get("model") if isinstance(extra, Mapping) else None
    return model if isinstance(model, str) and model else None


# ---------------------------------------------------------------------------
# Record builders (one per collection; the explicit field list is the review)
# ---------------------------------------------------------------------------


def _account(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "user_id": row[0],
        "email": row[1],
        "display_name": row[2],
        "created_at": row[3],
        "timezone": row[4],
        "units": row[5],
    }


def _preferences(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "goals": _decode_list(row[0]),
        "constraints": _decode_list(row[1]),
        "experience_level": row[2],
        "coaching_style": row[3],
        "preferred_ai": row[4],
        "ai_model": row[5],
        "custom_rules": _decode_list(row[6]),
        "updated_at": row[7],
    }


def _programme(row: tuple[Any, ...]) -> dict[str, Any]:
    definition, error = _decode_blob(row[3])
    record: dict[str, Any] = {
        "programme_id": row[0],
        "source": row[1],
        "active": bool(row[2]),
        "created_at": row[4],
        "updated_at": row[5],
        "engine_version": (
            definition.get("engine_version") if isinstance(definition, Mapping) else None
        ),
        "definition": definition,
    }
    if error:
        record["definition_error"] = error
    return record


def _programme_state(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"current_day": row[0], "split_name": row[1]}


def _hevy_workout(row: tuple[Any, ...]) -> dict[str, Any]:
    payload, error = _decode_blob(row[2])
    record: dict[str, Any] = {"record_id": row[0], "date": row[1], "payload": payload}
    if error:
        record["payload_error"] = error
    return record


def _exercise_progress(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "record_id": row[0],
        "date": row[1],
        "exercise_name": row[2],
        "top_weight_kg": row[3],
        "top_reps": row[4],
        "sets": row[5],
    }


def _body_metric(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "record_id": row[0],
        "date": row[1],
        "weight_kg": row[2],
        "body_fat_pct": row[3],
        "muscle_pct": row[4],
        "resting_hr": row[5],
        "hrv": row[6],
    }


def _check_in(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "record_id": row[0],
        "number": row[1],
        "date": row[2],
        "workouts_done": row[3],
        "weeks": row[4],
        "message": row[5],
    }


def _connection(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "provider": row[0],
        "credential_configured": bool(row[1]),
        "model": _configured_model(row[2]),
        "created_at": row[3],
        "updated_at": row[4],
    }


def _google_health(_row: tuple[Any, ...]) -> dict[str, Any]:
    return {"provider": "google_health", "connected": True}


def _hevy_routine(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"routine_key": row[0], "routine_id": row[1], "content_hash": row[2]}


def _sync_state(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"key": row[0], "value": row[1]}


def _notification(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "record_id": row[0],
        "type": row[1],
        "title": row[2],
        "message": row[3],
        "link": row[4],
        "is_read": bool(row[5]),
        "created_at": row[6],
    }


def _push_subscription(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "record_id": row[0],
        "endpoint_origin": _endpoint_origin(row[1]),
        "created_at": row[2],
    }


def _chat_message(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "record_id": row[0],
        "role": row[1],
        "content": row[2],
        "created_at": row[3],
    }


def _reasoning_log(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "context_id": row[0],
        "date": row[1],
        "exercise_name": row[2],
        "reasoning": row[3],
    }


def _dashboard_insight(row: tuple[Any, ...]) -> dict[str, Any]:
    insight, error = _decode_blob(row[1])
    record: dict[str, Any] = {"generated_on": row[0], "insight": insight}
    if error:
        record["insight_error"] = error
    return record


def _correlation_report(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"generated_on": row[0], "report_markdown": row[1]}


def _daily_plan(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "record_id": row[0],
        "date": row[1],
        "day": row[2],
        "focus": row[3],
        "carb_tier": row[4],
        "plan": row[5],
        "lifestyle": row[6],
    }


def _audit_event(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "event_id": row[0],
        "event_type": row[1],
        "outcome": row[2],
        "resource_type": row[3],
        "resource_id": row[4],
        "occurred_at": row[5],
    }


_NO_MODEL_NOTE = "The AI provider and model are not stored with this record."

COLLECTIONS: tuple[Collection, ...] = (
    Collection(
        section="profile",
        name="account",
        description="Account identity and display settings.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_MIXED,
            "Sign-in identity (email, display name) from the identity provider; "
            "timezone and units are account settings.",
        ),
        sql=(
            "SELECT id, email, display_name, created_at, timezone, units "
            "FROM users WHERE id = ?"
        ),
        owner_column="id",
        to_record=_account,
    ),
    Collection(
        section="profile",
        name="preferences",
        description="Coaching goals, constraints and AI provider preferences.",
        schema_version=1,
        provenance=Provenance(ORIGIN_USER_ENTERED, "Settings: Profile and AI coach."),
        sql=(
            "SELECT goals, constraints, experience_level, coaching_style, "
            "preferred_ai, ai_model, custom_rules, updated_at "
            "FROM user_preferences WHERE user_id = ?"
        ),
        to_record=_preferences,
    ),
    Collection(
        section="programmes",
        name="programmes",
        description="Saved and active training programmes with their definitions.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_DERIVED,
            "Programme builder preview that the account activated.",
            generator="dynamic_programme.build_programme_preview",
            derived_from=(
                "Hevy routine snapshots embedded in each definition (source_snapshots)",
                "the goal, duration and routine selection made in the programme builder",
            ),
            notes="engine_version records the generator version used per programme.",
        ),
        sql=(
            "SELECT id, source, active, definition, created_at, updated_at "
            "FROM programmes WHERE user_id = ? ORDER BY id"
        ),
        to_record=_programme,
    ),
    Collection(
        section="programmes",
        name="state",
        description="Current position within a programme cycle.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_SYSTEM_GENERATED,
            "Cycle position advanced by the daily coaching run.",
        ),
        sql="SELECT current_day, split_name FROM programme_state WHERE user_id = ?",
        to_record=_programme_state,
    ),
    Collection(
        section="workouts",
        name="hevy_workouts",
        description="Raw workout payloads imported from Hevy.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_CONNECTOR_IMPORT,
            "Hevy API, stored as received for coaching analysis.",
            notes="Credential-class keys are removed from payloads.",
        ),
        sql=(
            "SELECT id, date, hevy_payload FROM workout_history "
            "WHERE user_id = ? ORDER BY id"
        ),
        to_record=_hevy_workout,
    ),
    Collection(
        section="workouts",
        name="exercise_progress",
        description="Top set and set count per exercise per session.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_DERIVED,
            "Computed when a workout is imported.",
            generator="hevy_parser.parse_workout + database.save_progress",
            derived_from=("workouts/hevy_workouts",),
        ),
        sql=(
            "SELECT id, date, exercise_name, top_weight_kg, top_reps, sets "
            "FROM exercise_progress WHERE user_id = ? ORDER BY id"
        ),
        to_record=_exercise_progress,
    ),
    Collection(
        section="metrics",
        name="body_metrics",
        description="Body composition and recovery measurements by date.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_CONNECTOR_IMPORT,
            "Google Health or a Health Connect file imported by the daily run.",
            notes="The import source is not stored per measurement.",
        ),
        sql=(
            "SELECT id, date, weight_kg, body_fat_pct, muscle_pct, resting_hr, hrv "
            "FROM body_metrics WHERE user_id = ? ORDER BY id"
        ),
        to_record=_body_metric,
    ),
    Collection(
        section="check_ins",
        name="check_ins",
        description="Periodic programme check-in summaries.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_AI_GENERATED,
            "Routine check-in run by the coaching agent.",
            generator="checkin.run_checkin",
            derived_from=(
                "Hevy exercise history for the check-in window",
                "workouts/hevy_workouts",
            ),
            notes=(
                "A deterministic summary replaces the AI message when generation "
                "fails. " + _NO_MODEL_NOTE
            ),
        ),
        sql=(
            "SELECT id, number, date, workouts_done, weeks, message "
            "FROM check_ins WHERE user_id = ? ORDER BY id"
        ),
        to_record=_check_in,
    ),
    Collection(
        section="connectors",
        name="connections",
        description="Configured connectors and AI providers, without credentials.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_USER_ENTERED,
            "Settings: connections and AI coach.",
            notes=(
                "credential_configured reports presence only; keys, secrets and "
                "tokens are never exported."
            ),
        ),
        sql=(
            "SELECT provider, length(api_key) > 0, extra_json, created_at, "
            "updated_at FROM user_api_keys WHERE user_id = ? ORDER BY provider"
        ),
        to_record=_connection,
    ),
    Collection(
        section="connectors",
        name="google_health",
        description="Whether Google Health is linked. The token is never exported.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_SYSTEM_GENERATED,
            "Recorded when the Google Health authorisation flow completes.",
            notes="No record means Google Health is not linked.",
        ),
        sql=(
            "SELECT 1 FROM hevy_meta WHERE user_id = ? "
            "AND key = 'google_health_refresh_token' AND value != '' LIMIT 1"
        ),
        to_record=_google_health,
    ),
    Collection(
        section="connectors",
        name="hevy_routines",
        description="Hevy routines created or tracked by the agent.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_SYSTEM_GENERATED,
            "Routine sync between the agent and Hevy.",
        ),
        sql=(
            "SELECT routine_key, routine_id, content_hash FROM hevy_routines "
            "WHERE user_id = ? ORDER BY routine_key"
        ),
        to_record=_hevy_routine,
    ),
    Collection(
        section="connectors",
        name="sync_state",
        description="Non-sensitive connector and programme bookkeeping values.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_SYSTEM_GENERATED,
            "Written by connector sync and check-in jobs.",
            notes="Only an allow-listed set of keys is exported.",
        ),
        sql=(
            "SELECT key, value FROM hevy_meta WHERE user_id = ? "
            "AND key IN (?, ?, ?, ?, ?) ORDER BY key"
        ),
        params_after_owner=SYNC_STATE_KEYS,
        to_record=_sync_state,
    ),
    Collection(
        section="notifications",
        name="notifications",
        description="In-app notification history.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_SYSTEM_GENERATED,
            "Created by coaching, sync and insight jobs.",
        ),
        sql=(
            "SELECT id, type, title, message, link, is_read, created_at "
            "FROM notifications WHERE user_id = ? ORDER BY id"
        ),
        to_record=_notification,
    ),
    Collection(
        section="notifications",
        name="push_subscriptions",
        description="Browser push registrations, reduced to the push service host.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_USER_ENTERED,
            "Settings: web push notifications.",
            notes=(
                "The endpoint path and the encryption keys are credentials and "
                "are not exported."
            ),
        ),
        sql=(
            "SELECT id, endpoint, created_at FROM push_subscriptions "
            "WHERE user_id = ? ORDER BY id"
        ),
        to_record=_push_subscription,
    ),
    Collection(
        section="coaching",
        name="chat_messages",
        description="Coach conversation history.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_MIXED,
            "Messages typed by the account (role user) and replies generated by "
            "the selected AI provider (role assistant).",
            notes=_NO_MODEL_NOTE,
        ),
        sql=(
            "SELECT id, role, content, created_at FROM chat_messages "
            "WHERE user_id = ? ORDER BY id"
        ),
        to_record=_chat_message,
    ),
    Collection(
        section="coaching",
        name="reasoning_logs",
        description="Explanations of why exercise performance changed.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_AI_GENERATED,
            "On-demand explanation requested from the Progress page.",
            generator="webapp.app.xai_reasoning",
            derived_from=("workouts/exercise_progress",),
            notes=_NO_MODEL_NOTE,
        ),
        sql=(
            "SELECT context_id, date, exercise_name, reasoning FROM reasoning_logs "
            "WHERE user_id = ? ORDER BY date, context_id"
        ),
        to_record=_reasoning_log,
    ),
    Collection(
        section="coaching",
        name="dashboard_insight",
        description="The latest daily dashboard insight.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_AI_GENERATED,
            "Daily insight job.",
            generator="insight_cron.generate_daily_header",
            derived_from=("metrics/body_metrics", "coaching/daily_plans"),
            notes=(
                "Only the latest insight is retained. insight has the fields "
                "fatigue, wins_stalls and advice. " + _NO_MODEL_NOTE
            ),
        ),
        sql="SELECT date, insight_json FROM dashboard_insights WHERE user_id = ?",
        to_record=_dashboard_insight,
    ),
    Collection(
        section="coaching",
        name="correlation_report",
        description="The latest weekly correlation report.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_AI_GENERATED,
            "Weekly deep-correlation job over a 60-day window.",
            generator="insight_cron.generate_weekly_correlations",
            derived_from=(
                "metrics/body_metrics",
                "coaching/daily_plans",
                "workouts/exercise_progress",
            ),
            notes="Only the latest report is retained. " + _NO_MODEL_NOTE,
        ),
        sql="SELECT date, insight_markdown FROM deep_correlations WHERE user_id = ?",
        to_record=_correlation_report,
    ),
    Collection(
        section="coaching",
        name="daily_plans",
        description="Daily workout plans and lifestyle guidance that were sent.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_AI_GENERATED,
            "Daily coaching run.",
            generator="main.run (gemini_engine.generate_next_workout, "
            "lifestyle.daily_guidance)",
            derived_from=(
                "workouts/hevy_workouts",
                "workouts/exercise_progress",
                "metrics/body_metrics",
                "coaching/daily_plans (the previous plan)",
            ),
            notes=(
                "plan is AI-generated; lifestyle is deterministic guidance. "
                + _NO_MODEL_NOTE
            ),
        ),
        sql=(
            "SELECT id, date, day, focus, carb_tier, plan, lifestyle "
            "FROM daily_log WHERE user_id = ? ORDER BY id"
        ),
        to_record=_daily_plan,
    ),
    Collection(
        section="audit",
        name="events",
        description="Account audit events, including exports of this data.",
        schema_version=1,
        provenance=Provenance(
            ORIGIN_SYSTEM_GENERATED,
            "Recorded by the application when an audited account action occurs.",
            notes="Events carry identifiers and outcomes only, never content.",
        ),
        sql=(
            "SELECT id, event_type, outcome, resource_type, resource_id, occurred_at "
            "FROM account_audit_events WHERE user_id = ? ORDER BY id"
        ),
        to_record=_audit_event,
    ),
)


_FROM_TABLE = re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE)


def exported_tables() -> frozenset[str]:
    """Tables read by at least one collection, derived from the reviewed queries."""

    return frozenset(
        match.group(1)
        for collection in COLLECTIONS
        if (match := _FROM_TABLE.search(collection.sql))
    )


def iter_records(
    conn: sqlite3.Connection, collection: Collection, owner_id: str
) -> Iterator[dict[str, Any]]:
    """Stream the owner's records for *collection* in bounded batches."""

    if not owner_id:
        raise ValueError("an export requires an authenticated owner")
    cursor = conn.execute(collection.sql, (owner_id, *collection.params_after_owner))
    while True:
        rows = cursor.fetchmany(_FETCH_BATCH)
        if not rows:
            return
        for row in rows:
            record = collection.to_record(row)
            if record is not None:
                yield record
