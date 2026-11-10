"""Typed contracts for the tenant-scoped Hevy-first programme domain.

The contracts in this module are deliberately independent of the legacy
``programmes.definition`` JSON blob.  Runtime callers should depend on these
models and the service in :mod:`programme_domain.service`; the old blob remains
only as a temporary compatibility read path while consumers migrate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

ENGINE_VERSION = "programme-engine/v1"
PROGRAMME_STATUSES = frozenset(
    {"draft", "active", "paused", "completed", "archived", "migration_required"}
)
ONE_ACTIVE_STATUS = "active"


class ProgrammeValidationError(ValueError):
    """Raised when a draft or repository operation violates a domain invariant."""


class ProgrammeNotFoundError(LookupError):
    """Raised when a programme is absent for the authenticated tenant."""


@dataclass(frozen=True)
class ProgrammeRoutine:
    """An immutable snapshot of one selected Hevy routine and its order."""

    source_routine_id: str
    title: str
    snapshot_hash: str
    rotation_position: int
    provider: str = "hevy"
    folder_id: str | None = None
    provider_updated_at: str | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)
    owner_user_id: str | None = None
    enabled: bool = True
    participation: str = "full"
    cadence: str = "rotation"
    source_conflict_state: str = "unchanged"


@dataclass(frozen=True)
class ProgrammeBlock:
    """An ordered, bounded phase of a programme revision."""

    position: int
    key: str
    name: str
    start_week: int
    end_week: int
    objective: str = ""
    targets: dict[str, Any] = field(default_factory=dict)
    adaptation_policy: dict[str, Any] = field(default_factory=dict)
    exit_criteria: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExercisePrescription:
    """Deterministic, modality-aware prescription overlay for one exercise."""

    exercise_template_id: str
    exercise_title: str
    role: str
    routine_position: int | None = None
    block_position: int | None = None
    week_number: int | None = None
    set_target: dict[str, Any] = field(default_factory=dict)
    rep_target: dict[str, Any] = field(default_factory=dict)
    effort_target: dict[str, Any] = field(default_factory=dict)
    load_anchor: dict[str, Any] = field(default_factory=dict)
    rest_seconds: int | None = None
    progression_method: str = "double_progression"
    progression_rules: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    rule_version: str = ENGINE_VERSION


@dataclass(frozen=True)
class ProgrammeDraft:
    """A complete draft revision persisted as one immutable version."""

    name: str
    goal: str
    duration_weeks: int
    start_date: date
    routines: list[ProgrammeRoutine]
    blocks: list[ProgrammeBlock]
    prescriptions: list[ExercisePrescription] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    engine_version: str = ENGINE_VERSION
    reason: str = "draft"

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(weeks=self.duration_weeks) - timedelta(
            days=1
        )


@dataclass(frozen=True)
class ProgrammeWeekState:
    """Mutable week-level planning/actual/review state for an exact revision."""

    week_number: int
    planned: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    adherence: float | None = None
    fatigue: float | None = None
    review_state: str = "planned"
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class AdaptationDecisionInput:
    """Inputs supplied when an adaptation creates a new revision."""

    scope: dict[str, Any]
    inputs: dict[str, Any]
    output: dict[str, Any]
    confidence: float
    rule_version: str = ENGINE_VERSION
    explanation: str = ""
    confirmed: bool = False


@dataclass(frozen=True)
class AdaptationDecision:
    """A persisted adaptation audit record linking two immutable revisions."""

    id: int
    programme_id: int
    from_revision_id: int | None
    to_revision_id: int
    scope: dict[str, Any]
    inputs: dict[str, Any]
    output: dict[str, Any]
    confidence: float
    rule_version: str
    explanation: str
    confirmed: bool
    created_at: str


@dataclass(frozen=True)
class RoutineWorkoutMatch:
    """A persisted workout-to-routine mapping with explicit confidence."""

    programme_id: int
    revision_id: int
    routine_position: int
    source_routine_id: str
    workout_id: str
    score: float
    confidence: float
    method: str
    user_confirmed: bool = False
    matcher_version: str = "routine-matcher/v1"
    created_at: str | None = None


@dataclass(frozen=True)
class ProgrammeRevision:
    """An immutable revision and its normalized contents."""

    id: int
    user_id: str
    programme_id: int
    revision_number: int
    reason: str
    engine_version: str
    settings: dict[str, Any]
    source_summary: dict[str, Any]
    created_at: str
    routines: list[ProgrammeRoutine] = field(default_factory=list)
    blocks: list[ProgrammeBlock] = field(default_factory=list)
    prescriptions: list[ExercisePrescription] = field(default_factory=list)


@dataclass(frozen=True)
class Programme:
    """A tenant-owned programme aggregate with its current revision."""

    id: int
    user_id: str
    name: str
    status: str
    goal: str | None
    duration_weeks: int | None
    start_date: date | None
    end_date: date | None
    current_revision_id: int | None
    current_revision: ProgrammeRevision | None
    created_at: str
    updated_at: str
    activated_at: str | None = None
    archived_at: str | None = None
    predecessor_programme_id: int | None = None


@dataclass(frozen=True)
class ProgrammeWeek:
    """The resolved prescription and state for one programme week."""

    programme_id: int
    revision_id: int
    revision_number: int
    week_number: int
    start_date: date
    end_date: date
    blocks: list[ProgrammeBlock]
    routines: list[ProgrammeRoutine]
    prescriptions: list[ExercisePrescription]
    state: ProgrammeWeekState | None
