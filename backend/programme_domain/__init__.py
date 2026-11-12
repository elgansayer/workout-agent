"""Tenant-scoped Hevy-first programme domain."""

from __future__ import annotations

from .drift import DriftConflictError, DriftService, Resolution
from .lifecycle import LifecycleService
from .models import (
    ENGINE_VERSION,
    AdaptationDecision,
    AdaptationDecisionInput,
    ExercisePrescription,
    Programme,
    ProgrammeBlock,
    ProgrammeDraft,
    ProgrammeNotFoundError,
    ProgrammeOverride,
    ProgrammeRevision,
    ProgrammeRoutine,
    ProgrammeValidationError,
    ProgrammeWeek,
    ProgrammeWeekState,
    RoutineWorkoutMatch,
)
from .repository import ProgrammeRepository
from .review import AdaptationService, WorkoutMatch, decision_view
from .service import ProgrammeService, validate_adaptation, validate_draft

__all__ = [
    "ENGINE_VERSION",
    "AdaptationDecision",
    "AdaptationDecisionInput",
    "AdaptationService",
    "DriftConflictError",
    "DriftService",
    "ExercisePrescription",
    "LifecycleService",
    "Programme",
    "ProgrammeBlock",
    "ProgrammeDraft",
    "ProgrammeNotFoundError",
    "ProgrammeOverride",
    "ProgrammeRepository",
    "ProgrammeRevision",
    "ProgrammeRoutine",
    "ProgrammeService",
    "ProgrammeValidationError",
    "ProgrammeWeek",
    "ProgrammeWeekState",
    "Resolution",
    "RoutineWorkoutMatch",
    "WorkoutMatch",
    "decision_view",
    "validate_adaptation",
    "validate_draft",
]
