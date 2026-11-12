"""Complete, asynchronous, tenant-scoped export of one account's data.

Lifecycle: a signed-in user asks for an export -> the request is queued -> the
scheduler process claims it and streams a ZIP archive of JSON files into the
owner's export directory -> the owner downloads it until it expires -> the file
and its ledger row are removed. ``docs/ACCOUNT_EXPORT.md`` is the authoritative
description of what is exported, what is withheld and why.

Safety properties:

* Every statement is scoped to the owner (``WHERE <owner column> = ?``). The
  owner always comes from the authenticated session or from the persisted request
  row, never from a client-supplied field, and a worker re-authorizes it through
  ``tenant_isolation.run_owned_job`` immediately before building.
* Exports are *allow-listed*, never ``SELECT *``. Every exported table names the
  columns it releases and the columns it withholds with a reason; a new column is
  withheld until a person reviews it (``test_every_column_is_exported_or_withheld``).
  Credential-class columns can never be listed, and JSON blobs are scrubbed of
  credential-class keys as defence in depth.
* A table that owns rows but is neither exported nor deliberately excluded is
  reported in the manifest as unreviewed instead of being silently dropped.
* The archive is built in one read snapshot, in bounded batches, under a byte
  budget, into a private ``.partial`` file that is published only inside the
  database write lock after re-checking the request and the account gate.
* Archives live under ``<database directory>/exports/<user id>/`` - the location
  ``account_deletion`` erases - with owner-only permissions, and expire.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, TypedDict
from uuid import uuid4

import account_deletion
import provenance_ledger as ledger
from account_deletion import AccountGate
from data_classification import DataClass, classify_field
from provenance import (
    PROVENANCE_CONTRACT_VERSION,
    RecordKind,
    RecordType,
    describe,
    programme_view,
    source_label,
)
from tenancy import LEGACY_TENANT_EMAIL
from tenant_isolation import (
    JobStatus,
    OwnedJob,
    OwnerIdentity,
    OwnershipError,
    run_owned_job,
)
from user_settings import export_preferences, get_user_settings_or_defaults

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "workout-agent.account-export"
EXPORT_FORMAT_VERSION = 1
ARCHIVE_ROOT = "workout-agent-export"
MANIFEST_NAME = "manifest.json"
README_NAME = "README.txt"

DEFAULT_RETENTION_HOURS = 24
MIN_RETENTION_HOURS = 1
MAX_RETENTION_HOURS = 168
DEFAULT_MAX_MEGABYTES = 512
MIN_MAX_MEGABYTES = 1
MAX_MAX_MEGABYTES = 4096
DEFAULT_DAILY_LIMIT = 5
MAX_DAILY_LIMIT = 50

LEASE = timedelta(minutes=15)
#: Seconds between lease renewals while a build runs (well inside ``LEASE``).
HEARTBEAT_INTERVAL = 30.0
RETRY_BACKOFF = timedelta(minutes=5)
MAX_ATTEMPTS = 3
_DAY = timedelta(hours=24)
#: How long a finished request stays listed before it is forgotten.
HISTORY_RETENTION = timedelta(days=30)
_BATCH_SIZE = 500
_HEARTBEAT_EVERY = 2000
_JOBS_PER_TICK = 5
_EXPIRY_PER_TICK = 100
_LIST_LIMIT = 10
_COPY_CHUNK = 1024 * 1024
_ZIP64_LIMIT = (1 << 31) - 1

_EXPORT_ID = re.compile(r"^[0-9a-f]{32}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ExportState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


class ExportError(Exception):
    """A refused or failed request. ``code`` is a stable, non-sensitive identifier."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class _Aborted(Exception):
    """The request is no longer ours to finish (account closing, lease lost)."""


# ---------------------------------------------------------------------------
# What is exported
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, str] = {
    "profile": "Profile, preferences and sign-in methods",
    "programmes": "Training programmes, revisions and adaptation decisions",
    "workouts": "Workouts, exercise progress and Hevy routines",
    "metrics": "Body and health metrics",
    "check_ins": "Check-ins",
    "connectors": "Connector metadata (never credentials)",
    "notifications": "Notifications",
    "coaching": "Coach chat history, daily plans, insights and explanations",
    "audit": "Security events, credential activity and deletion requests",
    "provenance": "Provenance ledger and correction history",
}

#: Disclosed in the manifest and in Settings so the archive never claims more than
#: it holds. Every entry corresponds to a deliberate decision documented in
#: ``docs/ACCOUNT_EXPORT.md``.
EXCLUDED_DATA: tuple[tuple[str, str], ...] = (
    (
        "credentials",
        (
            "API keys, OAuth and refresh tokens, client secrets, credential hints and "
            "web-push endpoints and keys. Secrets are never exported; only metadata "
            "such as the provider, status and dates is."
        ),
    ),
    (
        "sessions",
        "The browser-session registry and anti-forgery tokens (internal security data).",
    ),
    (
        "audit_internals",
        (
            "Hash-chain digests, sequence numbers, key identifiers, client-address "
            "pseudonyms and alerts raised by the system for operators."
        ),
    ),
    (
        "identity_provider_ids",
        "The sign-in provider's stable subject identifiers.",
    ),
    (
        "other_accounts",
        "Records that belong to other accounts, including operator identities.",
    ),
    (
        "operational_ledgers",
        (
            "Job, deletion-evidence and ownership-migration ledgers kept by the "
            "service itself."
        ),
    ),
)

_OWNER_KEY = "owner key; the archive is already scoped to the requester"
_ROW_ID = "internal row identifier"
_CHAIN = "audit hash-chain internals; withheld so the trail stays tamper-evident"

#: Tables that carry an account reference but are deliberately not exported.
EXCLUDED_TABLES: dict[str, str] = {
    "user_sessions": (
        "session registry; rows hold one-way digests of session identifiers "
        "(credential class)"
    ),
    "csrf_tokens": "single-use anti-forgery nonces (internal security data)",
    "account_tombstones": (
        "deletion evidence that exists only after an account is closed, never "
        "for an account that can still request an export"
    ),
    "legacy_ownership_migrations": (
        "operator-run ownership assignment ledger; names the operator"
    ),
    "account_export_requests": (
        "this feature's own job ledger; each delivery is audited as data.exported"
    ),
}


@dataclass(frozen=True, slots=True)
class LedgerProvenance:
    """Attach the current provenance-ledger revision, looked up by record key."""

    record_type: RecordType
    key: Callable[[Mapping[str, Any]], str]


RowProvenance = Callable[[Mapping[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class ExportTable:
    """One exported table: an allow-list of columns and how to read them."""

    path: str
    table: str
    description: str
    columns: tuple[str, ...]
    order_by: str
    withheld: Mapping[str, str] = field(default_factory=dict)
    #: ``alias -> SQL expression`` over the table alias ``t`` for values that must
    #: be resolved in SQL (never user input).
    computed: Mapping[str, str] = field(default_factory=dict)
    owner: str = "user_id"
    #: Constant extra predicate over alias ``t`` and its bound parameters.
    where: str = ""
    params: tuple[str, ...] = ()
    json_columns: frozenset[str] = frozenset()
    #: JSON columns that are only decoded when they hold an object or array.
    container_json_columns: frozenset[str] = frozenset()
    bool_columns: frozenset[str] = frozenset()
    derived: bool = False
    optional: bool = False
    provenance: LedgerProvenance | RowProvenance | None = None
    schema_version: int = 1

    @property
    def category(self) -> str:
        return self.path.split("/", 1)[0]

    @property
    def output_names(self) -> tuple[str, ...]:
        return (*self.columns, *self.computed)

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES or not self.path.endswith(".json"):
            raise ValueError(f"unknown export path {self.path!r}")
        overlap = set(self.columns) & set(self.withheld)
        if overlap or len(set(self.columns)) != len(self.columns):
            raise ValueError(f"{self.path}: columns must be unique and not withheld")
        for column in self.columns:
            if classify_field(column) is DataClass.CREDENTIAL:
                raise ValueError(
                    f"{self.path}: credential-class column {column!r} can never be exported"
                )
        unknown = (self.json_columns | self.container_json_columns | self.bool_columns) - set(
            self.columns
        )
        if unknown:
            raise ValueError(f"{self.path}: decoders for unexported columns {sorted(unknown)}")


def _derived(
    source: str,
    *,
    version: str,
    confidence: str | None = None,
    method: str | None = None,
    stamp: str = "created_at",
) -> RowProvenance:
    """Provenance for a derived row from the version columns it already stores."""

    def build(record: Mapping[str, Any]) -> dict[str, Any]:
        raw_version = record.get(version)
        view: dict[str, Any] = {
            "contract_version": PROVENANCE_CONTRACT_VERSION,
            "kind": RecordKind.DERIVED.value,
            "source": source,
            "source_label": source_label(source),
            "label": describe(RecordKind.DERIVED, source),
            "authoritative": False,
            "transformation_version": None if raw_version is None else str(raw_version),
            "ingested_at": record.get(stamp),
            "confidence": record.get(confidence) if confidence else None,
        }
        if method:
            view["method"] = record.get(method)
        return view

    return build


def _imported(source_column: str, *, version: str, stamp: str) -> RowProvenance:
    """Provenance for an imported row that names its own provider and version."""

    def build(record: Mapping[str, Any]) -> dict[str, Any]:
        provider = str(record.get(source_column) or "unattributed").lower()
        raw_version = record.get(version)
        return {
            "contract_version": PROVENANCE_CONTRACT_VERSION,
            "kind": RecordKind.IMPORTED.value,
            "source": provider,
            "source_label": source_label(provider),
            "label": describe(RecordKind.IMPORTED, provider),
            "authoritative": True,
            "transformation_version": None if raw_version is None else str(raw_version),
            "ingested_at": record.get(stamp),
            "confidence": None,
        }

    return build


def _programme_provenance(record: Mapping[str, Any]) -> dict[str, Any] | None:
    return programme_view(record)


def _chat_provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    """Chat messages carry no ledger revision; the role says who wrote them."""
    typed = record.get("role") == "user"
    kind = RecordKind.USER_ENTERED if typed else RecordKind.DERIVED
    source = "manual" if typed else "unattributed"
    return {
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "kind": kind.value,
        "source": source,
        "source_label": source_label(source),
        "label": describe(kind, source),
        "authoritative": typed,
        "transformation_version": None,
        "ingested_at": record.get("created_at"),
        "confidence": None,
    }


_META_KEYS = (
    "programme_start_date",
    "checkin_number",
    "last_checkin_date",
    "last_checkin_workout_count",
    "last_checkin_week",
    "routine_folder_id",
    "hevy_source_sync",
)


def _table(
    path: str,
    table: str,
    description: str,
    columns: Sequence[str],
    order_by: str,
    **options: Any,
) -> ExportTable:
    return ExportTable(
        path=path,
        table=table,
        description=description,
        columns=tuple(columns),
        order_by=order_by,
        **options,
    )


def _json(*names: str) -> frozenset[str]:
    return frozenset(names)


EXPORT_TABLES: tuple[ExportTable, ...] = (
    # -- profile ---------------------------------------------------------
    _table(
        "profile/account.json",
        "users",
        "The account itself: identity, regional and accessibility settings.",
        (
            "id",
            "email",
            "display_name",
            "created_at",
            "last_login_at",
            "status",
            "status_changed_at",
            "updated_at",
            "timezone",
            "units",
            "locale",
            "motion",
            "contrast",
            "text_size",
        ),
        "t.created_at, t.id",
        owner="id",
        withheld={"session_epoch": "session-revocation counter (internal security data)"},
    ),
    _table(
        "profile/sign_in_methods.json",
        "user_identities",
        "Identity providers linked to the account (without provider subject ids).",
        ("provider", "email", "email_verified", "created_at", "last_login_at"),
        "t.created_at, t.provider",
        bool_columns=_json("email_verified"),
        withheld={
            "id": _ROW_ID,
            "user_id": _OWNER_KEY,
            "subject": "the identity provider's stable per-person identifier",
        },
    ),
    _table(
        "profile/coaching_profile.json",
        "user_preferences",
        "Goals, constraints and coaching preferences you set.",
        (
            "goals",
            "constraints",
            "experience_level",
            "coaching_style",
            "preferred_ai",
            "ai_model",
            "custom_rules",
            "updated_at",
        ),
        "t.user_id",
        json_columns=_json("goals", "constraints", "custom_rules"),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "profile/training_rules.json",
        "training_rules",
        "Structured training rules you saved.",
        ("version", "rules_json", "updated_at"),
        "t.user_id",
        json_columns=_json("rules_json"),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "profile/onboarding.json",
        "onboarding_skips",
        "First-run setup steps you skipped.",
        ("step", "skipped_at"),
        "t.skipped_at, t.id",
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    _table(
        "profile/access_invitations.json",
        "account_invitations",
        "The invitation, if any, that admitted this account (without operator identities).",
        ("status", "created_at", "expires_at", "accepted_at", "revoked_at"),
        "t.created_at, t.id",
        owner="accepted_user_id",
        withheld={
            "id": _ROW_ID,
            "email": "the invited address; already in profile/account.json",
            "invited_by": "identity of the operator who issued the invitation",
            "accepted_user_id": _OWNER_KEY,
        },
    ),
    # -- programmes ------------------------------------------------------
    _table(
        "programmes/programmes.json",
        "programmes",
        "Training programmes, including the generated definition.",
        (
            "id",
            "source",
            "template_key",
            "definition",
            "active",
            "created_at",
            "updated_at",
            "name",
            "status",
            "goal",
            "duration_weeks",
            "start_date",
            "end_date",
            "current_revision_id",
            "activated_at",
            "archived_at",
            "predecessor_programme_id",
        ),
        "t.id",
        json_columns=_json("definition"),
        bool_columns=_json("active"),
        derived=True,
        provenance=_programme_provenance,
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "programmes/state.json",
        "programme_state",
        "Where you are in the rotating split.",
        ("current_day", "split_name"),
        "t.user_id",
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "programmes/revisions.json",
        "programme_revisions",
        "Immutable programme revisions and why each was made.",
        (
            "id",
            "programme_id",
            "revision_number",
            "reason",
            "engine_version",
            "settings_json",
            "source_summary_json",
            "created_at",
        ),
        "t.programme_id, t.revision_number",
        json_columns=_json("settings_json", "source_summary_json"),
        derived=True,
        provenance=_derived(
            "rules:hevy-programme", version="engine_version", stamp="created_at"
        ),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "programmes/routines.json",
        "programme_routines",
        "Hevy routines a programme revision is built from.",
        (
            "id",
            "programme_id",
            "revision_id",
            "position",
            "provider",
            "source_routine_id",
            "title",
            "folder_id",
            "provider_updated_at",
            "snapshot_hash",
            "snapshot_json",
            "enabled",
            "participation",
            "cadence",
            "source_conflict_state",
            "created_at",
        ),
        "t.programme_id, t.revision_id, t.position",
        json_columns=_json("snapshot_json"),
        bool_columns=_json("enabled"),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "programmes/blocks.json",
        "programme_blocks",
        "Training blocks of each programme revision.",
        (
            "id",
            "programme_id",
            "revision_id",
            "position",
            "key",
            "name",
            "start_week",
            "end_week",
            "objective",
            "targets_json",
            "adaptation_policy_json",
            "exit_criteria_json",
            "created_at",
        ),
        "t.programme_id, t.revision_id, t.position",
        json_columns=_json("targets_json", "adaptation_policy_json", "exit_criteria_json"),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "programmes/prescriptions.json",
        "exercise_prescriptions",
        "Exercise prescriptions generated for each programme revision.",
        (
            "id",
            "programme_id",
            "revision_id",
            "routine_position",
            "block_position",
            "week_number",
            "exercise_template_id",
            "exercise_title",
            "role",
            "set_target_json",
            "rep_target_json",
            "effort_target_json",
            "load_anchor_json",
            "rest_seconds",
            "progression_method",
            "progression_rules_json",
            "confidence",
            "rule_version",
            "created_at",
        ),
        "t.programme_id, t.revision_id, t.routine_position, t.week_number, t.id",
        json_columns=_json(
            "set_target_json",
            "rep_target_json",
            "effort_target_json",
            "load_anchor_json",
            "progression_rules_json",
        ),
        derived=True,
        provenance=_derived(
            "rules:exercise-prescription", version="rule_version", confidence="confidence"
        ),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "programmes/week_states.json",
        "programme_week_states",
        "Planned versus actual training for each programme week.",
        (
            "id",
            "programme_id",
            "revision_id",
            "week_number",
            "planned_json",
            "actual_json",
            "adherence",
            "fatigue",
            "review_state",
            "created_at",
            "updated_at",
        ),
        "t.programme_id, t.week_number, t.id",
        json_columns=_json("planned_json", "actual_json"),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "programmes/adaptation_decisions.json",
        "adaptation_decisions",
        "Programme adaptation decisions with their inputs and explanation.",
        (
            "id",
            "programme_id",
            "from_revision_id",
            "to_revision_id",
            "scope_json",
            "inputs_json",
            "output_json",
            "confidence",
            "rule_version",
            "explanation",
            "confirmed",
            "created_at",
        ),
        "t.programme_id, t.created_at, t.id",
        json_columns=_json("scope_json", "inputs_json", "output_json"),
        bool_columns=_json("confirmed"),
        derived=True,
        provenance=_derived(
            "rules:programme-adaptation", version="rule_version", confidence="confidence"
        ),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "programmes/decisions.json",
        "programme_decisions",
        "Which programme source was chosen as authoritative, and why.",
        ("id", "programme_id", "source", "template_key", "decision", "reason", "created_at"),
        "t.created_at, t.id",
        withheld={"user_id": _OWNER_KEY},
    ),
    # -- workouts --------------------------------------------------------
    _table(
        "workouts/workouts.json",
        "workout_history",
        "Workouts imported from Hevy, as received (per-workout provenance is in "
        "provenance/record_provenance.json).",
        ("date", "hevy_payload"),
        "t.date, t.id",
        json_columns=_json("hevy_payload"),
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    _table(
        "workouts/exercise_progress.json",
        "exercise_progress",
        "Best set per exercise per workout.",
        ("date", "exercise_name", "top_weight_kg", "top_reps", "sets", "performed_at"),
        "t.date, t.id",
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    _table(
        "workouts/routines.json",
        "hevy_routines",
        "Hevy routines the app keeps track of.",
        ("routine_key", "routine_id", "content_hash"),
        "t.routine_key",
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "workouts/routine_snapshots.json",
        "hevy_routine_snapshots",
        "Versioned snapshots of your Hevy routines.",
        (
            "snapshot_id",
            "routine_id",
            "title",
            "content_hash",
            "schema_version",
            "updated_at",
            "fetched_at",
            "superseded_snapshot_id",
            "snapshot_json",
        ),
        "t.routine_id, t.fetched_at, t.snapshot_id",
        json_columns=_json("snapshot_json"),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "workouts/routine_matches.json",
        "routine_workout_matches",
        "How completed workouts were matched to programme routines.",
        (
            "id",
            "programme_id",
            "revision_id",
            "routine_position",
            "source_routine_id",
            "workout_id",
            "routine_snapshot_id",
            "routine_id",
            "score",
            "confidence",
            "features_json",
            "method",
            "matcher_version",
            "is_ambiguous",
            "user_confirmed",
            "matched_at",
            "created_at",
        ),
        "t.programme_id, t.matched_at, t.id",
        json_columns=_json("features_json"),
        bool_columns=_json("is_ambiguous", "user_confirmed"),
        derived=True,
        provenance=_derived(
            "rules:routine-matcher",
            version="matcher_version",
            confidence="confidence",
            method="method",
            stamp="matched_at",
        ),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "workouts/routine_match_overrides.json",
        "routine_match_overrides",
        "Routine matches you corrected by hand.",
        ("pattern_type", "pattern_value", "routine_id", "routine_snapshot_id", "created_at"),
        "t.created_at, t.pattern_type, t.pattern_value",
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "workouts/exercise_baselines.json",
        "exercise_history_baselines",
        "Baselines derived from your exercise history.",
        (
            "template_id",
            "rule_version",
            "confidence",
            "evidence_start",
            "evidence_end",
            "baseline_json",
            "created_at",
        ),
        "t.template_id, t.rule_version",
        json_columns=_json("baseline_json"),
        derived=True,
        provenance=_derived(
            "rules:exercise-baseline", version="rule_version", confidence="confidence"
        ),
        withheld={"user_id": _OWNER_KEY},
    ),
    # -- metrics ---------------------------------------------------------
    _table(
        "metrics/body_metrics.json",
        "body_metrics",
        "Daily body measurements, each with where the value came from.",
        ("date", "weight_kg", "body_fat_pct", "muscle_pct", "resting_hr", "hrv", "source"),
        "t.date, t.id",
        provenance=LedgerProvenance(RecordType.BODY_METRIC, lambda record: str(record["date"])),
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    _table(
        "metrics/health_metrics.json",
        "health_metrics",
        "Canonical health readings imported from connected providers.",
        (
            "metric_type",
            "value",
            "unit",
            "observed_at",
            "provider",
            "upstream_id",
            "source_device",
            "source_app",
            "data_origin",
            "normalization_version",
            "metadata_json",
        ),
        "t.observed_at, t.metric_type, t.id",
        json_columns=_json("metadata_json"),
        optional=True,
        provenance=_imported("provider", version="normalization_version", stamp="observed_at"),
        withheld={
            "id": _ROW_ID,
            "user_id": _OWNER_KEY,
            "connection_id": _ROW_ID,
            "fingerprint": "internal de-duplication digest",
        },
    ),
    # -- check-ins -------------------------------------------------------
    _table(
        "check_ins/check_ins.json",
        "check_ins",
        "Programme check-ins.",
        ("number", "date", "workouts_done", "weeks", "message"),
        "t.number, t.id",
        derived=True,
        provenance=LedgerProvenance(RecordType.CHECK_IN, lambda record: str(record["number"])),
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    # -- connectors (metadata only) ----------------------------------------
    _table(
        "connectors/hevy_connection.json",
        "hevy_connections",
        "The verified Hevy connection (never the key).",
        (
            "provider",
            "state",
            "account_username",
            "account_id",
            "api_version",
            "capabilities_json",
            "tested_at",
            "created_at",
            "updated_at",
        ),
        "t.provider",
        json_columns=_json("capabilities_json"),
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    _table(
        "connectors/credentials.json",
        "user_api_keys",
        "Which credentials are stored and their lifecycle. The secrets themselves are never exported.",
        (
            "provider",
            "status",
            "key_version",
            "created_at",
            "updated_at",
            "rotated_at",
            "revoked_at",
            "last_tested_at",
            "last_test_status",
        ),
        "t.provider",
        withheld={
            "id": _ROW_ID,
            "user_id": _OWNER_KEY,
            "api_key": "secret",
            "client_id": "stored beside secrets; not reviewed for export",
            "client_secret": "secret",
            "refresh_token": "secret",
            "extra_json": "may carry provider secrets",
            "key_hint": "trailing characters of a stored secret",
        },
    ),
    _table(
        "connectors/health_connections.json",
        "health_connections",
        "Connected health providers and what they may share.",
        ("provider", "state", "granted_capabilities", "last_sync_at", "created_at", "updated_at"),
        "t.provider, t.created_at",
        json_columns=_json("granted_capabilities"),
        optional=True,
        withheld={
            "id": _ROW_ID,
            "user_id": _OWNER_KEY,
            "cursor": "provider sync pointer; may embed upstream tokens",
        },
    ),
    _table(
        "connectors/health_sync_runs.json",
        "health_sync_runs",
        "History of health-provider syncs.",
        ("provider", "state", "started_at", "finished_at", "fetched", "written", "skipped", "error_code"),
        "t.started_at, t.id",
        optional=True,
        withheld={
            "id": _ROW_ID,
            "user_id": _OWNER_KEY,
            "connection_id": _ROW_ID,
            "cursor_before": "provider sync pointer; may embed upstream tokens",
            "cursor_after": "provider sync pointer; may embed upstream tokens",
        },
    ),
    _table(
        "connectors/state.json",
        "hevy_meta",
        "Sync and check-in bookkeeping. Only reviewed keys are exported; the same "
        "table also stores OAuth state and tokens, which are not.",
        ("key", "value"),
        "t.\"key\"",
        where=(
            "(t.\"key\" IN ("
            + ", ".join("?" for _ in _META_KEYS)
            + ") OR t.\"key\" GLOB 'connector:*:last_sync')"
        ),
        params=_META_KEYS,
        container_json_columns=_json("value"),
        withheld={"user_id": _OWNER_KEY},
    ),
    # -- notifications ---------------------------------------------------
    _table(
        "notifications/notifications.json",
        "notifications",
        "Notifications sent to you.",
        ("type", "title", "message", "link", "is_read", "created_at"),
        "t.created_at, t.id",
        bool_columns=_json("is_read"),
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    _table(
        "notifications/push_subscriptions.json",
        "push_subscriptions",
        "When a browser was subscribed to web push. Endpoints and keys are never exported.",
        ("created_at",),
        "t.created_at, t.id",
        withheld={
            "id": _ROW_ID,
            "user_id": _OWNER_KEY,
            "endpoint": "capability URL for delivering pushes to the browser",
            "p256dh": "push encryption key",
            "auth": "push authentication secret",
        },
    ),
    # -- coaching --------------------------------------------------------
    _table(
        "coaching/chat_messages.json",
        "chat_messages",
        "Coach chat history.",
        ("role", "content", "created_at"),
        "t.created_at, t.id",
        derived=True,
        provenance=_chat_provenance,
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    _table(
        "coaching/daily_plans.json",
        "daily_log",
        "Daily plans and lifestyle guidance the Coach issued.",
        ("date", "day", "focus", "carb_tier", "plan", "lifestyle"),
        "t.date, t.id",
        derived=True,
        provenance=LedgerProvenance(RecordType.DAILY_PLAN, lambda record: str(record["date"])),
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    _table(
        "coaching/insights.json",
        "dashboard_insights",
        "The latest dashboard insight.",
        ("date", "insight_json"),
        "t.user_id",
        json_columns=_json("insight_json"),
        derived=True,
        provenance=LedgerProvenance(RecordType.DASHBOARD_INSIGHT, lambda record: "current"),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "coaching/correlations.json",
        "deep_correlations",
        "The latest weekly correlation report.",
        ("date", "insight_markdown"),
        "t.user_id",
        derived=True,
        provenance=LedgerProvenance(RecordType.DEEP_CORRELATION, lambda record: "current"),
        withheld={"user_id": _OWNER_KEY},
    ),
    _table(
        "coaching/reasoning.json",
        "reasoning_logs",
        "Explanations the Coach gave for exercise changes.",
        ("context_id", "date", "exercise_name", "reasoning"),
        "t.date, t.context_id",
        derived=True,
        provenance=LedgerProvenance(RecordType.REASONING, lambda record: str(record["context_id"])),
        withheld={"user_id": _OWNER_KEY},
    ),
    # -- audit -----------------------------------------------------------
    _table(
        "audit/security_events.json",
        "security_audit_events",
        "Security events about your account (sign-ins, key and connector changes, "
        "exports, deletions). System-raised alerts are not included.",
        ("occurred_at", "event_type", "outcome", "severity", "actor_type", "subject_type", "request_id"),
        "t.seq",
        where="t.actor_type <> 'system' AND t.event_type NOT LIKE 'alert.%'",
        withheld={
            "seq": "global sequence number; would reveal other accounts' activity volume",
            "schema_version": _CHAIN,
            "event_id": _CHAIN,
            "actor_id": "identity of the actor (an operator, for operator actions)",
            "user_id": _OWNER_KEY,
            "subject_id": "may be a keyed pseudonym of an identity",
            "source_ref": "keyed pseudonym of the client address",
            "metadata_json": "free-form diagnostics not reviewed for portability",
            "key_id": _CHAIN,
            "prev_hash": _CHAIN,
            "event_hash": _CHAIN,
        },
    ),
    _table(
        "audit/credential_events.json",
        "credential_audit_events",
        "Credential lifecycle events (saved, rotated, revoked, tested).",
        (
            "provider",
            "action",
            "outcome",
            "detail",
            "actor",
            "source",
            "credential_version",
            "created_at",
        ),
        "t.created_at, t.id",
        withheld={"id": _ROW_ID, "user_id": _OWNER_KEY},
    ),
    _table(
        "audit/deletion_requests.json",
        "account_deletion_requests",
        "Requests to erase health data or close the account.",
        ("id", "scope", "state", "requested_at", "execute_after", "completed_at", "cancelled_at"),
        "t.requested_at, t.id",
        withheld={
            "user_id": _OWNER_KEY,
            "next_attempt_at": "internal job scheduling",
            "attempts": "internal job scheduling",
            "started_at": "internal job scheduling",
            "error_code": "internal job diagnostics",
        },
    ),
    # -- provenance ------------------------------------------------------
    _table(
        "provenance/record_provenance.json",
        "record_provenance",
        "Every stored revision of every provenance-tracked record, including "
        "corrections and the values they replaced.",
        (
            "record_type",
            "record_key",
            "revision",
            "kind",
            "source",
            "external_id",
            "source_timestamp",
            "ingested_at",
            "transformation_version",
            "confidence",
            "contract_version",
            "applied",
            "lineage_note",
            "content_hash",
            "retained_content",
        ),
        "t.record_type, t.record_key, t.revision",
        computed={
            "supersedes_revision": (
                "(SELECT prev.revision FROM record_provenance prev "
                "WHERE prev.id = t.supersedes_id AND prev.user_id = t.user_id)"
            ),
            "held_by_revision": (
                "(SELECT held.revision FROM record_provenance held "
                "WHERE held.id = t.held_by_id AND held.user_id = t.user_id)"
            ),
        },
        json_columns=_json("retained_content"),
        bool_columns=_json("applied"),
        withheld={
            "id": _ROW_ID,
            "user_id": _OWNER_KEY,
            "supersedes_id": "row identifier; exported as supersedes_revision",
            "held_by_id": "row identifier; exported as held_by_revision",
        },
    ),
)


def _validate_manifest() -> None:
    paths = [spec.path for spec in EXPORT_TABLES]
    if len(set(paths)) != len(paths):
        raise ValueError("export paths must be unique")
    tables = [spec.table for spec in EXPORT_TABLES]
    if len(set(tables)) != len(tables):
        raise ValueError("a table may be exported by one file only")
    overlap = set(tables) & set(EXCLUDED_TABLES)
    if overlap:
        raise ValueError(f"tables both exported and excluded: {sorted(overlap)}")


_validate_manifest()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _bounded_int(name: str, raw: str, default: int, minimum: int, maximum: int) -> int:
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
        logger.warning("%s is outside %d-%d; using %d.", name, minimum, maximum, clamped)
    return clamped


def retention_hours() -> int:
    """How long a finished export can be downloaded (ACCOUNT_EXPORT_RETENTION_HOURS)."""
    return _bounded_int(
        "ACCOUNT_EXPORT_RETENTION_HOURS",
        os.environ.get("ACCOUNT_EXPORT_RETENTION_HOURS", ""),
        DEFAULT_RETENTION_HOURS,
        MIN_RETENTION_HOURS,
        MAX_RETENTION_HOURS,
    )


def max_bytes() -> int:
    """Uncompressed size one export may reach (ACCOUNT_EXPORT_MAX_MB)."""
    megabytes = _bounded_int(
        "ACCOUNT_EXPORT_MAX_MB",
        os.environ.get("ACCOUNT_EXPORT_MAX_MB", ""),
        DEFAULT_MAX_MEGABYTES,
        MIN_MAX_MEGABYTES,
        MAX_MAX_MEGABYTES,
    )
    return megabytes * 1024 * 1024


def daily_limit() -> int:
    """Exports one account may request per rolling 24 hours (ACCOUNT_EXPORT_DAILY_LIMIT)."""
    return _bounded_int(
        "ACCOUNT_EXPORT_DAILY_LIMIT",
        os.environ.get("ACCOUNT_EXPORT_DAILY_LIMIT", ""),
        DEFAULT_DAILY_LIMIT,
        1,
        MAX_DAILY_LIMIT,
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class _Clock:
    """The time inside one job: its start plus the monotonic time elapsed since.

    A build can run for minutes, so lease renewals, publication and back-off must
    not use the instant the tick began. Starting from a caller-supplied ``now``
    (rather than reading the wall clock) keeps every timestamp deterministic when
    a test or a replay supplies one.
    """

    def __init__(self, start: datetime) -> None:
        self._start = start
        self._began = time.monotonic()

    def __call__(self) -> datetime:
        return self._start + timedelta(seconds=time.monotonic() - self._began)


def _ts(moment: datetime) -> str:
    """Fixed-width UTC timestamp so lexical comparison equals time order."""
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


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


@contextmanager
def _snapshot(db_path: str) -> Iterator[sqlite3.Connection]:
    """A read-only connection whose reads all see one consistent snapshot."""
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        conn.close()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _account_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(col[1]) for col in conn.execute(f"PRAGMA table_info({_quote(table)})")}


def unreviewed_owner_tables(conn: sqlite3.Connection) -> list[str]:
    """Tables that reference an account but are neither exported nor excluded."""
    known = {spec.table for spec in EXPORT_TABLES} | set(EXCLUDED_TABLES) | {"users"}
    unreviewed: list[str] = []
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        name = str(row[0])
        if name in known:
            continue
        if any(
            column == "user_id" or column.endswith("_user_id")
            for column in _account_columns(conn, name)
        ):
            unreviewed.append(name)
    return sorted(unreviewed)


# ---------------------------------------------------------------------------
# Building the archive
# ---------------------------------------------------------------------------


class FileSummary(TypedDict):
    path: str
    category: str
    description: str
    schema_version: int
    derived: bool
    records: int
    bytes: int
    sha256: str
    redacted_fields: int


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    files: tuple[FileSummary, ...]
    record_count: int
    uncompressed_bytes: int


@dataclass(slots=True)
class _Budget:
    """Caps the uncompressed bytes one export may hold."""

    limit: int
    used: int = 0

    def consume(self, size: int) -> None:
        self.used += size
        if self.used > self.limit:
            raise ExportError("too_large")


class _Member:
    """Writes text into one archive member while counting and hashing it."""

    def __init__(self, member: IO[bytes], budget: _Budget) -> None:
        self._member = member
        self._budget = budget
        self._digest = hashlib.sha256()
        self.size = 0

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        self._budget.consume(len(data))
        self._digest.update(data)
        self._member.write(data)
        self.size += len(data)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


@dataclass(slots=True)
class _Stats:
    redactions: int = 0


def _json_default(value: object) -> object:
    if isinstance(value, (bytes, bytearray)):
        return {"encoding": "base64", "data": base64.b64encode(bytes(value)).decode("ascii")}
    return str(value)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _scrub(value: Any, stats: _Stats) -> Any:
    """Drop credential-class keys from a decoded JSON blob (defence in depth)."""
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, item in value.items():
            if classify_field(str(key)) is DataClass.CREDENTIAL:
                stats.redactions += 1
                continue
            kept[key] = _scrub(item, stats)
        return kept
    if isinstance(value, list):
        return [_scrub(item, stats) for item in value]
    return value


def _decode(value: Any, stats: _Stats, *, containers_only: bool) -> Any:
    if not isinstance(value, str) or not value:
        return value
    if containers_only and value[0] not in "{[":
        return value
    try:
        return _scrub(json.loads(value), stats)
    except ValueError:
        return value


def _select_sql(spec: ExportTable) -> str:
    parts = [f"t.{_quote(column)}" for column in spec.columns]
    parts += [f"{expression} AS {_quote(alias)}" for alias, expression in spec.computed.items()]
    sql = (
        f"SELECT {', '.join(parts)} FROM {_quote(spec.table)} t "
        f"WHERE t.{_quote(spec.owner)} = ?"
    )
    if spec.where:
        sql += f" AND ({spec.where})"
    return f"{sql} ORDER BY {spec.order_by}"


def _record(spec: ExportTable, row: sqlite3.Row, stats: _Stats) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for name in spec.output_names:
        value = row[name]
        if name in spec.json_columns:
            value = _decode(value, stats, containers_only=False)
        elif name in spec.container_json_columns:
            value = _decode(value, stats, containers_only=True)
        elif name in spec.bool_columns and value is not None:
            value = bool(value)
        record[name] = value
    return record


def _ledger_view(entry: ledger.LedgerEntry) -> dict[str, Any]:
    view = entry.view()
    view.pop("content", None)
    return view


def _attach_provenance(
    spec: ExportTable,
    conn: sqlite3.Connection,
    user_id: str,
    records: list[dict[str, Any]],
) -> None:
    source = spec.provenance
    if source is None:
        return
    if isinstance(source, LedgerProvenance):
        keys = [source.key(record).strip() for record in records]
        entries = ledger.current_entries(
            conn, user_id, source.record_type, [key for key in keys if key]
        )
        for record, key in zip(records, keys, strict=True):
            entry = entries.get(key)
            record["provenance"] = _ledger_view(entry) if entry is not None else None
        return
    for record in records:
        record["provenance"] = source(record)


def _header(spec: ExportTable) -> str:
    return (
        "{"
        f'"schema_version":{spec.schema_version},'
        f'"section":{_dumps(spec.path)},'
        f'"description":{_dumps(spec.description)},'
        f'"derived":{"true" if spec.derived else "false"},'
        '"records":['
    )


def _write_table(
    archive: zipfile.ZipFile,
    conn: sqlite3.Connection,
    spec: ExportTable,
    user_id: str,
    budget: _Budget,
    stamp: tuple[int, int, int, int, int, int],
    heartbeat: Callable[[], None],
) -> FileSummary:
    info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{spec.path}", date_time=stamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    stats = _Stats()
    count = 0
    heartbeat()
    with archive.open(info, "w", force_zip64=budget.limit > _ZIP64_LIMIT) as raw:
        member = _Member(raw, budget)
        member.write(_header(spec))
        if not spec.optional or _table_exists(conn, spec.table):
            cursor = conn.execute(_select_sql(spec), (user_id, *spec.params))
            while True:
                batch = cursor.fetchmany(_BATCH_SIZE)
                if not batch:
                    break
                records = [_record(spec, row, stats) for row in batch]
                _attach_provenance(spec, conn, user_id, records)
                for record in records:
                    member.write(("," if count else "") + "\n" + _dumps(record))
                    count += 1
                    if count % _HEARTBEAT_EVERY == 0:
                        heartbeat()
        member.write(f'\n],"record_count":{count}}}\n')
        return FileSummary(
            path=f"{ARCHIVE_ROOT}/{spec.path}",
            category=spec.category,
            description=spec.description,
            schema_version=spec.schema_version,
            derived=spec.derived,
            records=count,
            bytes=member.size,
            sha256=member.sha256,
            redacted_fields=stats.redactions,
        )


_README = """\
Workout Agent account export
============================

This archive is a portable copy of the data Workout Agent holds about one
account. It contains personal and health information: store it securely and
delete it when you no longer need it.

* manifest.json lists every file with its record count, size and SHA-256, the
  conventions used, what was deliberately left out and why, and any table the
  service has not yet reviewed for export.
* Every other file is JSON: {"schema_version", "section", "description",
  "derived", "records": [...], "record_count"}.
* Timestamps are ISO-8601 in UTC. Weights are kilograms. Local days follow the
  time zone and unit system recorded in manifest.json.
* Files with "derived": true hold recommendations or summaries produced by this
  app or an AI model. Each record carries a "provenance" object (kind, source,
  transformation_version, ingested_at, confidence). A null provenance means the
  source was not recorded when the value was stored.
* provenance/record_provenance.json is the full revision history of tracked
  records, including corrections you made and the values they replaced.
* Secrets (API keys, tokens, push keys) are never included.
"""


def _manifest(
    export_id: str,
    user_id: str,
    generated_at: datetime,
    files: Sequence[FileSummary],
    unreviewed: Sequence[str],
    preferences: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "export_id": export_id,
        "generated_at": _ts(generated_at),
        "account_id": user_id,
        "preferences": dict(preferences),
        "conventions": {
            "timestamps": "ISO-8601 UTC",
            "weights": "kilograms",
            "file_layout": '{"schema_version","section","description","derived","records","record_count"}',
            "null_provenance": "the source of the value was not recorded when it was stored",
        },
        "provenance": {
            "contract_version": PROVENANCE_CONTRACT_VERSION,
            "ledger": f"{ARCHIVE_ROOT}/provenance/record_provenance.json",
            "kinds": [kind.value for kind in RecordKind],
        },
        "categories": CATEGORIES,
        "files": list(files),
        "totals": {
            "files": len(files),
            "records": sum(item["records"] for item in files),
            "uncompressed_bytes": sum(item["bytes"] for item in files),
        },
        "excluded": [{"category": name, "reason": reason} for name, reason in EXCLUDED_DATA],
        "unreviewed_tables": list(unreviewed),
    }


def build_archive(
    db_path: str,
    user_id: str,
    destination: IO[bytes],
    *,
    export_id: str,
    generated_at: datetime,
    byte_limit: int,
    heartbeat: Callable[[], None] = lambda: None,
) -> ArchiveResult:
    """Stream the ZIP archive for *user_id* into *destination*.

    Reads one consistent snapshot in bounded batches. Raises ``ExportError``
    (``too_large``) when the uncompressed content passes *byte_limit*.
    """
    if not user_id:
        raise ExportError("not_eligible")
    budget = _Budget(byte_limit)
    stamp = generated_at.astimezone(timezone.utc).timetuple()[:6]
    files: list[FileSummary] = []
    with _snapshot(db_path) as conn, zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as archive:
        for spec in EXPORT_TABLES:
            files.append(_write_table(archive, conn, spec, user_id, budget, stamp, heartbeat))
        unreviewed = unreviewed_owner_tables(conn)
        if unreviewed:
            logger.warning(
                "Export skipped tables that own rows but were never reviewed: %s",
                ", ".join(unreviewed),
            )
        settings = get_user_settings_or_defaults(user_id, db_path)
        manifest = _manifest(
            export_id, user_id, generated_at, files, unreviewed, export_preferences(settings)
        )
        for name, text in (
            (README_NAME, _README),
            (MANIFEST_NAME, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"),
        ):
            info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{name}", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            data = text.encode("utf-8")
            budget.consume(len(data))
            archive.writestr(info, data)
    return ArchiveResult(
        files=tuple(files),
        record_count=sum(item["records"] for item in files),
        uncompressed_bytes=budget.used,
    )


# ---------------------------------------------------------------------------
# Files on disk
# ---------------------------------------------------------------------------


def _exports_root(db_path: str) -> Path:
    return Path(db_path).resolve().parent / account_deletion.EXPORT_DIRNAME


def _directory(db_path: str, user_id: str) -> Path | None:
    return account_deletion.export_directory(db_path, user_id)


def _artifact(db_path: str, user_id: str, export_id: str) -> Path | None:
    """The published archive for a request; ``None`` for an unsafe owner or id."""
    directory = _directory(db_path, user_id)
    if directory is None or not _EXPORT_ID.fullmatch(export_id):
        return None
    return directory / f"{export_id}.zip"


def _partial(db_path: str, user_id: str, export_id: str, attempt: int) -> Path | None:
    """The private file one attempt writes; attempts never share a path."""
    directory = _directory(db_path, user_id)
    if directory is None or not _EXPORT_ID.fullmatch(export_id):
        return None
    return directory / f".{export_id}.{attempt}.partial"


def _ensure_directory(db_path: str, user_id: str) -> None:
    directory = _directory(db_path, user_id)
    if directory is None:
        raise ExportError("not_eligible")
    for path in (_exports_root(db_path), directory):
        if path.is_symlink():
            raise ExportError("unsafe_directory")
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)


def _remove_artifacts(db_path: str, user_id: str, export_id: str) -> bool:
    """Unlink (never follow) the archive and every partial file; ``False`` on failure."""
    archive = _artifact(db_path, user_id, export_id)
    directory = _directory(db_path, user_id)
    if archive is None or directory is None:
        return True
    clean = True
    try:
        leftovers = list(directory.glob(f".{export_id}.*.partial"))
    except OSError:
        leftovers = []
    for path in (archive, *leftovers):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("Could not remove export file for request %s.", export_id)
            clean = False
    return clean


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class ExportStatus(TypedDict):
    export_id: str
    state: str
    requested_at: str
    started_at: str | None
    completed_at: str | None
    expires_at: str | None
    size_bytes: int | None
    sha256: str | None
    record_count: int | None
    format_version: int
    attempts: int
    error_code: str | None
    can_download: bool


_STATUS_COLUMNS = (
    "id, state, requested_at, started_at, completed_at, expires_at, size_bytes, "
    "sha256, record_count, format_version, attempts, error_code"
)


def _status_from_row(row: sqlite3.Row, now: datetime) -> ExportStatus:
    state = str(row["state"])
    expires_at = row["expires_at"]
    lapsed = state == ExportState.READY and expires_at is not None and str(expires_at) <= _ts(now)
    if lapsed:  # fail closed before the sweeper has caught up
        state = ExportState.EXPIRED.value
    return ExportStatus(
        export_id=str(row["id"]),
        state=state,
        requested_at=str(row["requested_at"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        expires_at=expires_at,
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        record_count=row["record_count"],
        format_version=int(row["format_version"]),
        attempts=int(row["attempts"]),
        error_code=row["error_code"],
        can_download=state == ExportState.READY,
    )


def is_export_id(value: str) -> bool:
    """True for the exact shape of an id this service issues."""
    return bool(_EXPORT_ID.fullmatch(value))


def get_export(
    db_path: str, user_id: str, export_id: str, *, now: datetime | None = None
) -> ExportStatus | None:
    """The owner's export, or ``None`` - identical for unknown and foreign ids."""
    if not is_export_id(export_id):
        return None
    with _reader(db_path) as conn:
        row = conn.execute(
            f"SELECT {_STATUS_COLUMNS} FROM account_export_requests "
            "WHERE id = ? AND user_id = ?",
            (export_id, user_id),
        ).fetchone()
    return _status_from_row(row, now or _utcnow()) if row else None


def _is_exportable(db_path: str, user_id: str) -> bool:
    """Whether *user_id* is an active, signed-in-capable account that is not being erased.

    A pending closure still qualifies - the cancellation window is when people
    want their data - but an account being erased, or closed, does not. The shared
    single-user tenant has no authenticated owner, so it never qualifies.
    """
    if account_deletion.account_gate(db_path, user_id) not in (
        AccountGate.OPEN,
        AccountGate.PENDING,
    ):
        return False
    with _reader(db_path) as conn:
        row = conn.execute(
            "SELECT status, email FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return (
        row is not None
        and str(row["status"]) == "active"
        and str(row["email"]).strip().lower() != LEGACY_TENANT_EMAIL
    )


def describe_exports(
    db_path: str, user_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """What the Settings page needs: limits, what is included, and recent exports."""
    moment = now or _utcnow()
    with _reader(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_STATUS_COLUMNS} FROM account_export_requests "
            "WHERE user_id = ? ORDER BY requested_at DESC, id DESC LIMIT ?",
            (user_id, _LIST_LIMIT),
        ).fetchall()
        used = conn.execute(
            "SELECT COUNT(*) FROM account_export_requests WHERE user_id = ? AND requested_at > ?",
            (user_id, _ts(moment - _DAY)),
        ).fetchone()[0]
    eligible = _is_exportable(db_path, user_id)
    return {
        "eligible": eligible,
        "reason": None if eligible else "not_eligible",
        "retention_hours": retention_hours(),
        "daily_limit": daily_limit(),
        "remaining_today": max(daily_limit() - int(used), 0),
        "format_version": EXPORT_FORMAT_VERSION,
        "included": [{"category": key, "label": label} for key, label in CATEGORIES.items()],
        "excluded": [{"category": name, "reason": reason} for name, reason in EXCLUDED_DATA],
        "exports": [_status_from_row(row, moment) for row in rows],
    }


# ---------------------------------------------------------------------------
# Requesting
# ---------------------------------------------------------------------------


def request_export(
    db_path: str, user_id: str, *, now: datetime | None = None
) -> tuple[ExportStatus, bool]:
    """Queue an export for *user_id*; returns ``(status, created)``.

    A second request while one is queued or running returns that request
    (``created`` is ``False``) instead of queueing a duplicate.
    """
    if not user_id:
        raise ExportError("not_eligible")
    moment = now or _utcnow()
    if not _is_exportable(db_path, user_id):
        raise ExportError("not_eligible")
    export_id = uuid4().hex
    with _transaction(db_path) as conn:
        user = conn.execute(
            "SELECT status, email FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if (
            user is None
            or str(user["status"]) != "active"
            or str(user["email"]).strip().lower() == LEGACY_TENANT_EMAIL
        ):
            raise ExportError("not_eligible")
        open_row = conn.execute(
            f"SELECT {_STATUS_COLUMNS} FROM account_export_requests "
            "WHERE user_id = ? AND state IN ('queued', 'running') "
            "ORDER BY requested_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if open_row is not None:
            return _status_from_row(open_row, moment), False
        used = conn.execute(
            "SELECT COUNT(*) FROM account_export_requests "
            "WHERE user_id = ? AND requested_at > ?",
            (user_id, _ts(moment - _DAY)),
        ).fetchone()[0]
        if int(used) >= daily_limit():
            raise ExportError("rate_limited")
        conn.execute(
            "INSERT INTO account_export_requests "
            "(id, user_id, state, format_version, requested_at, next_attempt_at) "
            "VALUES (?, ?, 'queued', ?, ?, ?)",
            (export_id, user_id, EXPORT_FORMAT_VERSION, _ts(moment), _ts(moment)),
        )
        row = conn.execute(
            f"SELECT {_STATUS_COLUMNS} FROM account_export_requests WHERE id = ?",
            (export_id,),
        ).fetchone()
    logger.info("account_export event=requested request=%s", export_id)
    return _status_from_row(row, moment), True


# ---------------------------------------------------------------------------
# Executing
# ---------------------------------------------------------------------------


def _claim(db_path: str, export_id: str, user_id: str, now: datetime) -> int | None:
    """Atomically take a due request; returns its attempt number, else ``None``.

    The attempt number fences the run: a worker whose lease lapsed and was taken
    over by a later attempt can no longer renew, publish or fail the request.
    """
    with _transaction(db_path) as conn:
        claimed = conn.execute(
            "UPDATE account_export_requests SET state = 'running', started_at = ?, "
            "attempts = attempts + 1, next_attempt_at = ?, error_code = NULL "
            "WHERE id = ? AND user_id = ? AND state IN ('queued', 'running') "
            "AND next_attempt_at <= ? AND attempts < ?",
            (_ts(now), _ts(now + LEASE), export_id, user_id, _ts(now), MAX_ATTEMPTS),
        ).rowcount
        if claimed != 1:
            return None
        row = conn.execute(
            "SELECT attempts FROM account_export_requests WHERE id = ? AND user_id = ?",
            (export_id, user_id),
        ).fetchone()
        return int(row["attempts"])


class _Heartbeat:
    """Renews the lease while a long build runs and stops it if it is no longer valid.

    Throttled: a build touches the database at most once per ``HEARTBEAT_INTERVAL``
    however many sections and batches it writes. Publishing re-checks everything
    under the write lock, so a skipped beat can never publish for a closing account.
    """

    def __init__(
        self, db_path: str, export_id: str, user_id: str, attempt: int, clock: _Clock
    ) -> None:
        self._args = (db_path, export_id, user_id, attempt)
        self._clock = clock
        self._last = time.monotonic()

    def __call__(self) -> None:
        if time.monotonic() - self._last < HEARTBEAT_INTERVAL:
            return
        self._last = time.monotonic()
        db_path, export_id, user_id, attempt = self._args
        with _transaction(db_path) as conn:
            held = conn.execute(
                "UPDATE account_export_requests SET next_attempt_at = ? "
                "WHERE id = ? AND user_id = ? AND state = 'running' AND attempts = ?",
                (_ts(self._clock() + LEASE), export_id, user_id, attempt),
            ).rowcount
        if held != 1:
            raise _Aborted("this attempt no longer owns the request")
        if not _is_exportable(db_path, user_id):
            raise ExportError("not_eligible")


def _publish(
    db_path: str,
    export_id: str,
    user_id: str,
    attempt: int,
    partial: Path,
    final: Path,
    result: ArchiveResult,
    size: int,
    digest: str,
    clock: _Clock,
) -> list[str]:
    """Make the archive downloadable, inside the write lock.

    Returns the ids of this account's earlier exports that were retired so only
    the newest archive stays on disk.
    """
    with _transaction(db_path) as conn:
        row = conn.execute(
            "SELECT state, attempts FROM account_export_requests "
            "WHERE id = ? AND user_id = ?",
            (export_id, user_id),
        ).fetchone()
        if (
            row is None
            or str(row["state"]) != ExportState.RUNNING
            or int(row["attempts"]) != attempt
        ):
            raise _Aborted("this attempt no longer owns the request")
        # Checked while holding the write lock: a deletion cannot be claimed
        # between this check and the commit, and one claimed earlier is seen here.
        if not _is_exportable(db_path, user_id):
            raise ExportError("not_eligible")
        older = [
            str(item["id"])
            for item in conn.execute(
                "SELECT id FROM account_export_requests "
                "WHERE user_id = ? AND state = 'ready' AND id <> ?",
                (user_id, export_id),
            )
        ]
        moment = clock()
        os.replace(partial, final)
        conn.execute(
            "UPDATE account_export_requests SET state = 'ready', completed_at = ?, "
            "expires_at = ?, size_bytes = ?, sha256 = ?, record_count = ?, "
            "error_code = NULL WHERE id = ? AND user_id = ?",
            (
                _ts(moment),
                _ts(moment + timedelta(hours=retention_hours())),
                size,
                digest,
                result.record_count,
                export_id,
                user_id,
            ),
        )
        conn.execute(
            "UPDATE account_export_requests SET state = 'expired' "
            "WHERE user_id = ? AND state = 'ready' AND id <> ?",
            (user_id, export_id),
        )
    return older


def _build_and_publish(
    db_path: str, export_id: str, user_id: str, attempt: int, clock: _Clock
) -> ExportStatus | None:
    _ensure_directory(db_path, user_id)
    partial = _partial(db_path, user_id, export_id, attempt)
    final = _artifact(db_path, user_id, export_id)
    if partial is None or final is None:
        raise ExportError("not_eligible")
    with contextlib.suppress(FileNotFoundError):
        partial.unlink()
    descriptor = os.open(
        partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            result = build_archive(
                db_path,
                user_id,
                handle,
                export_id=export_id,
                generated_at=clock(),
                byte_limit=max_bytes(),
                heartbeat=_Heartbeat(db_path, export_id, user_id, attempt, clock),
            )
            handle.flush()
            os.fsync(handle.fileno())
        size = partial.stat().st_size
        digest = _file_sha256(partial)
        retired = _publish(
            db_path, export_id, user_id, attempt, partial, final, result, size, digest, clock
        )
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise
    for older_id in retired:
        _remove_artifacts(db_path, user_id, older_id)
    return get_export(db_path, user_id, export_id)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ExportError):
        return exc.code
    if isinstance(exc, sqlite3.Error):
        return "database_error"
    if isinstance(exc, OSError):
        return "filesystem_error"
    return "internal_error"


#: Failures that retrying cannot fix.
_TERMINAL_CODES = frozenset({"too_large", "not_eligible", "unsafe_directory"})


def _settle_failure(
    db_path: str, export_id: str, user_id: str, attempt: int, code: str, now: datetime
) -> None:
    """Re-queue a failed attempt with back-off, or fail it for good.

    Only the attempt that still owns the request may settle it.
    """
    with _transaction(db_path) as conn:
        final = code in _TERMINAL_CODES or attempt >= MAX_ATTEMPTS
        settled = conn.execute(
            "UPDATE account_export_requests SET state = ?, error_code = ?, "
            "next_attempt_at = ? WHERE id = ? AND user_id = ? "
            "AND state = 'running' AND attempts = ?",
            (
                ExportState.FAILED.value if final else ExportState.QUEUED.value,
                code,
                _ts(now + RETRY_BACKOFF),
                export_id,
                user_id,
                attempt,
            ),
        ).rowcount
    if final and settled:
        _remove_artifacts(db_path, user_id, export_id)


def execute_export(
    db_path: str,
    export_id: str,
    user_id: str,
    *,
    now: datetime | None = None,
) -> ExportStatus | None:
    """Run a due request for *user_id*; a no-op if it is not due or already running.

    Safe to call from any process and to repeat: the claim is atomic and a crashed
    run is resumed once its lease expires. ``user_id`` must be the owner proven by
    the worker; a request that belongs to anyone else is never claimed.
    """
    clock = _Clock(now or _utcnow())
    if not _EXPORT_ID.fullmatch(export_id):
        return None
    attempt = _claim(db_path, export_id, user_id, clock())
    if attempt is None:
        return get_export(db_path, user_id, export_id, now=clock())
    logger.info("account_export event=started request=%s attempt=%d", export_id, attempt)
    try:
        if not _is_exportable(db_path, user_id):
            raise ExportError("not_eligible")
        status = _build_and_publish(db_path, export_id, user_id, attempt, clock)
        logger.info("account_export event=completed request=%s", export_id)
        return status
    except _Aborted:
        # A later attempt owns the request now; it is that attempt's to finish.
        logger.warning("account_export event=superseded request=%s", export_id)
    except Exception as exc:
        code = _error_code(exc)
        logger.exception("Account export %s failed with %s.", export_id, code)
        _settle_failure(db_path, export_id, user_id, attempt, code, clock())
    return get_export(db_path, user_id, export_id, now=clock())


# ---------------------------------------------------------------------------
# Scheduler tick
# ---------------------------------------------------------------------------


def prune_finished_exports(db_path: str, *, now: datetime | None = None) -> int:
    """Forget expired and failed requests once they are old; their files are gone.

    Deliveries stay on record in the security audit trail (``data.exported``).
    """
    cutoff = _ts((now or _utcnow()) - HISTORY_RETENTION)
    with _transaction(db_path) as conn:
        return max(
            conn.execute(
                "DELETE FROM account_export_requests "
                "WHERE state IN ('expired', 'failed') AND requested_at < ?",
                (cutoff,),
            ).rowcount,
            0,
        )


def expire_exports(db_path: str, *, now: datetime | None = None) -> int:
    """Delete archives past their expiry and mark them expired; returns the count."""
    moment = now or _utcnow()
    with _reader(db_path) as conn:
        due = [
            (str(row["id"]), str(row["user_id"]))
            for row in conn.execute(
                "SELECT id, user_id FROM account_export_requests "
                "WHERE state = 'ready' AND expires_at <= ? ORDER BY expires_at LIMIT ?",
                (_ts(moment), _EXPIRY_PER_TICK),
            )
        ]
    expired = 0
    for export_id, user_id in due:
        if not _remove_artifacts(db_path, user_id, export_id):
            continue  # still refused for download; retried next tick
        with _transaction(db_path) as conn:
            expired += max(
                conn.execute(
                    "UPDATE account_export_requests SET state = 'expired' "
                    "WHERE id = ? AND user_id = ? AND state = 'ready'",
                    (export_id, user_id),
                ).rowcount,
                0,
            )
    return expired


def _fail_stalled(db_path: str, now: datetime) -> int:
    """Fail requests whose lease expired after their last attempt."""
    with _transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT id, user_id FROM account_export_requests "
            "WHERE state = 'running' AND next_attempt_at <= ? AND attempts >= ?",
            (_ts(now), MAX_ATTEMPTS),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE account_export_requests SET state = 'failed', error_code = 'timed_out' "
                "WHERE id = ? AND user_id = ? AND state = 'running'",
                (row["id"], row["user_id"]),
            )
    for row in rows:
        _remove_artifacts(db_path, str(row["user_id"]), str(row["id"]))
    return len(rows)


def process_due_exports(db_path: str, *, now: datetime | None = None) -> int:
    """Worker tick for the scheduler process; returns exports made ready this tick."""
    moment = now or _utcnow()
    try:
        expire_exports(db_path, now=moment)
        _fail_stalled(db_path, moment)
        prune_finished_exports(db_path, now=moment)
        with _reader(db_path) as conn:
            due = [
                (str(row["id"]), str(row["user_id"]))
                for row in conn.execute(
                    "SELECT id, user_id FROM account_export_requests "
                    "WHERE state IN ('queued', 'running') AND next_attempt_at <= ? "
                    "AND attempts < ? ORDER BY next_attempt_at LIMIT ?",
                    (_ts(moment), MAX_ATTEMPTS, _JOBS_PER_TICK),
                )
            ]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):  # a database that predates the feature
            return 0
        raise
    ready = 0
    for export_id, user_id in due:
        try:
            job = OwnedJob("account-export", OwnerIdentity(user_id))
        except OwnershipError:
            logger.warning("Skipping export %s: row without a usable owner.", export_id)
            continue
        outcome = run_owned_job(
            job,
            db_path,
            # ``now`` is passed on as given: in production each job starts from a
            # fresh reading, after the jobs before it have taken their time.
            lambda owner, export_id=export_id: execute_export(
                db_path, export_id, owner.user_id, now=now
            ),
        )
        if outcome.status is JobStatus.COMPLETED:
            status = outcome.result
            if status is not None and status["state"] == ExportState.READY:
                ready += 1
        elif outcome.status is JobStatus.DENIED:
            # The owner no longer exists: nothing can ever download this.
            with _transaction(db_path) as conn:
                conn.execute(
                    "UPDATE account_export_requests SET state = 'failed', "
                    "error_code = 'not_eligible' WHERE id = ? AND user_id = ? "
                    "AND state IN ('queued', 'running')",
                    (export_id, user_id),
                )
            _remove_artifacts(db_path, user_id, export_id)
    return ready


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DownloadTarget:
    path: Path
    filename: str
    size_bytes: int
    sha256: str
    expires_at: str


def open_download(
    db_path: str, user_id: str, export_id: str, *, now: datetime | None = None
) -> DownloadTarget:
    """Resolve an export for download, or raise ``ExportError``.

    ``not_found`` is identical for unknown and foreign ids. The file must be a
    regular, non-symlink file of exactly the recorded size inside the owner's
    export directory; anything else is reported as ``expired`` (gone).
    """
    moment = now or _utcnow()
    status = get_export(db_path, user_id, export_id, now=moment)
    if status is None:
        raise ExportError("not_found")
    if status["state"] == ExportState.EXPIRED:
        raise ExportError("expired")
    if status["state"] != ExportState.READY or status["size_bytes"] is None:
        raise ExportError("not_ready")
    path = _artifact(db_path, user_id, export_id)
    if path is None:
        raise ExportError("not_found")
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ExportError("expired") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size != status["size_bytes"]:
        logger.warning("Export file for request %s failed its integrity check.", export_id)
        raise ExportError("expired")
    stamp = str(status["completed_at"] or status["requested_at"])[:10].replace("-", "")
    return DownloadTarget(
        path=path,
        filename=f"workout-agent-export-{stamp}-{export_id[:8]}.zip",
        size_bytes=int(status["size_bytes"]),
        sha256=str(status["sha256"]),
        expires_at=str(status["expires_at"]),
    )
