"""Tenant-scoped Hevy-first programme domain."""

from __future__ import annotations

from .models import (
    ENGINE_VERSION,
    AdaptationDecision,
    AdaptationDecisionInput,
    ExercisePrescription,
    Programme,
    ProgrammeBlock,
    ProgrammeDraft,
    ProgrammeNotFoundError,
    ProgrammeRevision,
    ProgrammeRoutine,
    ProgrammeValidationError,
    ProgrammeWeek,
    ProgrammeWeekState,
    RoutineWorkoutMatch,
)
from .repository import ProgrammeRepository
from .service import ProgrammeService, validate_adaptation, validate_draft

__all__ = [
    "ENGINE_VERSION",
    "AdaptationDecision",
    "AdaptationDecisionInput",
    "ExercisePrescription",
    "Programme",
    "ProgrammeBlock",
    "ProgrammeDraft",
    "ProgrammeNotFoundError",
    "ProgrammeRepository",
    "ProgrammeRevision",
    "ProgrammeRoutine",
    "ProgrammeService",
    "ProgrammeValidationError",
    "ProgrammeWeek",
    "ProgrammeWeekState",
    "RoutineWorkoutMatch",
    "validate_adaptation",
    "validate_draft",
]
