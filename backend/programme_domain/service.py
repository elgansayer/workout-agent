"""Application boundary for the Hevy-first programme domain.

This service is the intended runtime interface for programme lifecycle,
immutable revisions and week state.  It enforces every cross-row invariant
before delegating SQL to :class:`ProgrammeRepository`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date

from database import DEFAULT_DB_PATH

from .models import (
    AdaptationDecision,
    AdaptationDecisionInput,
    ExercisePrescription,
    Programme,
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

MAX_DURATION_WEEKS = 104


class ProgrammeService:
    """The only supported way for runtime code to read or change programmes."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        repository: ProgrammeRepository | None = None,
    ) -> None:
        self.repository = repository or ProgrammeRepository(db_path)

    # ------------------------------------------------------------------
    # Drafts and lifecycle
    # ------------------------------------------------------------------

    def preview_draft(self, user_id: str, draft: ProgrammeDraft) -> ProgrammeDraft:
        """Validate a draft without writing it, for preview surfaces."""

        validate_draft(user_id, draft)
        return draft

    def persist_draft(self, user_id: str, draft: ProgrammeDraft) -> Programme:
        """Persist a validated draft revision; it is not activated."""

        validate_draft(user_id, draft)
        return self.repository.create_draft(user_id, draft)

    def activate(self, user_id: str, programme_id: int) -> Programme:
        self._require_user(user_id)
        return self.repository.activate(user_id, programme_id)

    def activate_generated(
        self,
        user_id: str,
        draft: ProgrammeDraft,
        *,
        guard: Callable[[sqlite3.Connection], None] | None = None,
        first_week: ProgrammeWeekState | None = None,
        event: AdaptationDecisionInput | None = None,
        notification: dict[str, str] | None = None,
        legacy_start_date: str | None = None,
        template_key: str | None = None,
    ) -> Programme:
        """Validate, persist and activate a generated draft in one transaction."""

        self._require_user(user_id)
        validate_draft(user_id, draft)
        return self.repository.activate_generated(
            user_id,
            draft,
            guard=guard,
            first_week=first_week,
            event=event,
            notification=notification,
            legacy_start_date=legacy_start_date,
            template_key=template_key,
        )

    def pause(self, user_id: str, programme_id: int) -> Programme:
        self._require_user(user_id)
        return self.repository.set_status(user_id, programme_id, "paused")

    def complete(self, user_id: str, programme_id: int) -> Programme:
        self._require_user(user_id)
        return self.repository.set_status(user_id, programme_id, "completed")

    def archive(self, user_id: str, programme_id: int) -> Programme:
        self._require_user(user_id)
        return self.repository.set_status(
            user_id, programme_id, "archived", archive=True
        )

    # ------------------------------------------------------------------
    # Tenant-scoped reads
    # ------------------------------------------------------------------

    def get_active_programme(self, user_id: str) -> Programme | None:
        self._require_user(user_id)
        return self.repository.get_active_programme(user_id)

    def get_active_revision(self, user_id: str) -> ProgrammeRevision | None:
        programme = self.get_active_programme(user_id)
        return programme.current_revision if programme else None

    def get_programme(self, user_id: str, programme_id: int) -> Programme | None:
        self._require_user(user_id)
        return self.repository.get_programme(user_id, programme_id)

    def get_programme_week(
        self,
        user_id: str,
        programme_id: int,
        week_number: int,
        *,
        revision_number: int | None = None,
    ) -> ProgrammeWeek | None:
        self._require_user(user_id)
        return self.repository.get_programme_week(
            user_id,
            programme_id,
            week_number,
            revision_number=revision_number,
        )

    def list_revision_history(
        self, user_id: str, programme_id: int
    ) -> list[ProgrammeRevision]:
        self._require_user(user_id)
        return self.repository.list_revisions(user_id, programme_id)

    def get_revision(
        self, user_id: str, programme_id: int, revision_number: int
    ) -> ProgrammeRevision | None:
        self._require_user(user_id)
        return self.repository.get_revision(
            user_id, programme_id, revision_number
        )

    # ------------------------------------------------------------------
    # New revisions from adaptation or rebase
    # ------------------------------------------------------------------

    def create_revision_from_adaptation(
        self,
        user_id: str,
        programme_id: int,
        draft: ProgrammeDraft,
        decision: AdaptationDecisionInput,
    ) -> ProgrammeRevision:
        """Record an adaptation and append the resulting immutable revision."""

        self._require_user(user_id)
        validate_adaptation(decision)
        validate_draft(user_id, draft)
        return self.repository.create_revision(
            user_id,
            programme_id,
            draft,
            reason="adaptation",
            adaptation=decision,
        )

    def create_revision_from_rebase(
        self,
        user_id: str,
        programme_id: int,
        draft: ProgrammeDraft,
        *,
        reason: str = "rebase",
    ) -> ProgrammeRevision:
        """Append a new revision after source reconciliation or explicit edits."""

        self._require_user(user_id)
        validate_draft(user_id, draft)
        return self.repository.create_revision(
            user_id, programme_id, draft, reason=reason
        )

    def list_adaptation_decisions(
        self, user_id: str, programme_id: int
    ) -> list[AdaptationDecision]:
        self._require_user(user_id)
        return self.repository.list_adaptation_decisions(user_id, programme_id)

    def list_week_states(
        self, user_id: str, programme_id: int
    ) -> list[tuple[int, ProgrammeWeekState]]:
        """Every stored week state with the revision id that produced it."""

        self._require_user(user_id)
        return self.repository.list_week_states(user_id, programme_id)

    def get_adaptation_decision(
        self, user_id: str, programme_id: int, decision_id: int
    ) -> AdaptationDecision | None:
        self._require_user(user_id)
        return self.repository.get_adaptation_decision(user_id, programme_id, decision_id)

    # ------------------------------------------------------------------
    # Week state and workout matching
    # ------------------------------------------------------------------

    def save_week_state(
        self,
        user_id: str,
        programme_id: int,
        revision_id: int,
        state: ProgrammeWeekState,
    ) -> ProgrammeWeekState:
        self._require_user(user_id)
        if state.week_number < 1:
            raise ProgrammeValidationError("Week numbers start at 1.")
        return self.repository.save_week_state(
            user_id, programme_id, revision_id, state
        )

    def save_routine_workout_match(
        self, user_id: str, match: RoutineWorkoutMatch
    ) -> RoutineWorkoutMatch:
        self._require_user(user_id)
        if not 0.0 <= match.score <= 1.0:
            raise ProgrammeValidationError("Match score must be between 0 and 1.")
        if not 0.0 <= match.confidence <= 1.0:
            raise ProgrammeValidationError(
                "Match confidence must be between 0 and 1."
            )
        return self.repository.save_routine_workout_match(user_id, match)

    def list_routine_workout_matches(
        self, user_id: str, programme_id: int
    ) -> list[RoutineWorkoutMatch]:
        self._require_user(user_id)
        return self.repository.list_routine_workout_matches(user_id, programme_id)

    @staticmethod
    def _require_user(user_id: str) -> None:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ProgrammeValidationError("An authenticated user_id is required.")


def validate_draft(user_id: str, draft: ProgrammeDraft) -> None:
    """Enforce tenant, ordering, duration and block invariants for a draft."""

    ProgrammeService._require_user(user_id)
    if not draft.name.strip():
        raise ProgrammeValidationError("A programme name is required.")
    if not draft.goal.strip():
        raise ProgrammeValidationError("A programme goal is required.")
    if not 1 <= int(draft.duration_weeks) <= MAX_DURATION_WEEKS:
        raise ProgrammeValidationError(
            f"Programme duration must be between 1 and {MAX_DURATION_WEEKS} weeks."
        )
    if not isinstance(draft.start_date, date):
        raise ProgrammeValidationError("A programme start date is required.")
    if not draft.engine_version.strip():
        raise ProgrammeValidationError("An engine version is required.")

    _validate_routines(user_id, draft.routines)
    _validate_blocks(draft.blocks, draft.duration_weeks)
    _validate_prescriptions(draft)


def validate_adaptation(decision: AdaptationDecisionInput) -> None:
    if not 0.0 <= decision.confidence <= 1.0:
        raise ProgrammeValidationError(
            "Adaptation confidence must be between 0 and 1."
        )
    if not decision.rule_version.strip():
        raise ProgrammeValidationError("An adaptation rule version is required.")


def _validate_routines(user_id: str, routines: list[ProgrammeRoutine]) -> None:
    if not routines:
        raise ProgrammeValidationError(
            "Select at least one Hevy routine for the programme."
        )
    positions = [routine.rotation_position for routine in routines]
    if sorted(positions) != list(range(1, len(routines) + 1)):
        raise ProgrammeValidationError(
            "Routine rotation positions must be explicit and unique from 1."
        )
    source_ids = [routine.source_routine_id for routine in routines]
    if len(set(source_ids)) != len(source_ids):
        raise ProgrammeValidationError(
            "Each selected Hevy routine may appear only once."
        )
    for routine in routines:
        if not routine.source_routine_id.strip():
            raise ProgrammeValidationError("A routine provider ID is required.")
        if not routine.snapshot_hash.strip():
            raise ProgrammeValidationError(
                "A routine snapshot hash is required before persistence."
            )
        if routine.provider != "hevy":
            raise ProgrammeValidationError(
                "Only Hevy routines can be selected in this programme version."
            )
        if (
            routine.owner_user_id is not None
            and routine.owner_user_id != user_id
        ):
            raise ProgrammeValidationError(
                "A programme revision cannot reference a routine snapshot "
                "owned by another user."
            )


def _validate_blocks(blocks: list, duration_weeks: int) -> None:
    if not blocks:
        raise ProgrammeValidationError("A programme requires at least one block.")
    positions = [block.position for block in blocks]
    if sorted(positions) != list(range(1, len(blocks) + 1)):
        raise ProgrammeValidationError(
            "Block positions must be explicit and unique from 1."
        )
    ordered = sorted(blocks, key=lambda block: block.position)
    previous_end = 0
    for block in ordered:
        if block.start_week < 1 or block.end_week > duration_weeks:
            raise ProgrammeValidationError(
                f"Block {block.name!r} falls outside the programme duration."
            )
        if block.start_week > block.end_week:
            raise ProgrammeValidationError(
                f"Block {block.name!r} starts after it ends."
            )
        if block.start_week <= previous_end:
            raise ProgrammeValidationError(
                "Programme blocks must be ordered and non-overlapping."
            )
        previous_end = block.end_week


def _validate_prescriptions(draft: ProgrammeDraft) -> None:
    routine_positions = {
        routine.rotation_position for routine in draft.routines
    }
    block_positions = {block.position for block in draft.blocks}
    for prescription in draft.prescriptions:
        if not prescription.exercise_template_id.strip():
            raise ProgrammeValidationError(
                "An exercise template ID is required for each prescription."
            )
        if not prescription.role.strip():
            raise ProgrammeValidationError(
                "An exercise role is required for each prescription."
            )
        if (
            prescription.routine_position is not None
            and prescription.routine_position not in routine_positions
        ):
            raise ProgrammeValidationError(
                "A prescription references an unselected routine."
            )
        if (
            prescription.block_position is not None
            and prescription.block_position not in block_positions
        ):
            raise ProgrammeValidationError(
                "A prescription references an unknown block."
            )
        if prescription.week_number is not None and not (
            1 <= prescription.week_number <= draft.duration_weeks
        ):
            raise ProgrammeValidationError(
                "A prescription week falls outside the programme duration."
            )
        if not 0.0 <= prescription.confidence <= 1.0:
            raise ProgrammeValidationError(
                "Prescription confidence must be between 0 and 1."
            )
        if prescription.rest_seconds is not None and prescription.rest_seconds < 0:
            raise ProgrammeValidationError("Rest seconds cannot be negative.")


__all__ = [
    "ExercisePrescription",
    "ProgrammeNotFoundError",
    "ProgrammeService",
    "ProgrammeValidationError",
    "validate_adaptation",
    "validate_draft",
]
