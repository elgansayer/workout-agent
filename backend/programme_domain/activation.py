"""Conversion between a generated preview and an immutable domain revision.

A generated preview (``dynamic_programme.build_programme_preview``) is the
*definition* of a programme.  Activation stores that definition verbatim in the
revision's ``settings`` (the compatibility read path used by
:mod:`programme_runtime`) **and** projects it onto the normalised revision
tables.  Every later revision (adaptation, rebase, extension) is produced the
same way, so the two representations can never drift apart: a structural change
edits a copy of the definition, and :func:`definition_to_draft` derives the rows.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from .models import (
    ENGINE_VERSION,
    ExercisePrescription,
    ProgrammeBlock,
    ProgrammeDraft,
    ProgrammeRevision,
    ProgrammeRoutine,
    ProgrammeValidationError,
    ProgrammeWeekState,
)
from .weeks import cadence_mode, plan_week, summarise_week

ROLE_CONFIDENCE = {"high": 0.85, "medium": 0.6, "low": 0.35, "user_override": 1.0}
_BLOCK_STRUCTURAL_KEYS = frozenset(
    {
        "number",
        "key",
        "name",
        "start_week",
        "end_week",
        "duration_weeks",
        "weeks",
        "start_date",
        "end_date",
        "focus",
        "objective",
        "exit_criteria",
    }
)
_BLOCK_POLICY_KEYS = frozenset(
    {
        "progression_aggressiveness",
        "fatigue_management",
        "review_gate",
        "test_intent",
        "calibration",
        "exercise_variation",
    }
)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _set_target(prescription: Mapping[str, Any], source_sets: int) -> dict[str, Any]:
    sets = int(prescription.get("sets") or source_sets or 1)
    bounds = prescription.get("allowed_set_change_from_source") or {}
    allowed = prescription.get("allowed_change") or {}
    minimum = _optional_int(bounds.get("minimum"))
    maximum = _optional_int(bounds.get("maximum"))
    return {
        "target": sets,
        "min": max(1, source_sets + (minimum if minimum is not None else -1)),
        "max": max(sets, source_sets + (maximum if maximum is not None else 2)),
        "source_sets": source_sets,
        "max_set_delta": allowed.get("max_set_delta"),
    }


def definition_to_draft(
    definition: Mapping[str, Any],
    user_id: str,
    *,
    reason: str = "generated",
    audit: Mapping[str, Any] | None = None,
) -> ProgrammeDraft:
    """Project a generated definition onto the normalised domain contracts."""

    spec = definition.get("programme_spec") or {}
    try:
        start = date.fromisoformat(str(spec.get("start_date")))
    except ValueError as exc:
        raise ProgrammeValidationError("The programme start date is invalid.") from exc
    duration = int(spec.get("duration_weeks") or definition.get("cycle_weeks") or 0)
    goal = str(
        spec.get("goal")
        or (definition.get("goal_profile") or {}).get("key")
        or ""
    )
    snapshots = {
        str(snapshot.get("id")): snapshot
        for snapshot in definition.get("source_snapshots") or []
        if isinstance(snapshot, Mapping)
    }
    cadence = definition.get("cadence") if isinstance(definition.get("cadence"), Mapping) else {}
    rotation = {
        str(entry.get("routine_id")): entry
        for entry in (cadence or {}).get("rotation") or []
        if isinstance(entry, Mapping)
    }
    mode = "rolling" if cadence_mode(definition) == "rolling" else "weekly"

    routines: list[ProgrammeRoutine] = []
    for day in definition.get("days") or []:
        routine_id = str(day.get("routine_id") or "")
        snapshot = snapshots.get(routine_id, {})
        routines.append(
            ProgrammeRoutine(
                source_routine_id=routine_id,
                title=str(day.get("focus") or snapshot.get("title") or routine_id),
                snapshot_hash=str(day.get("source_hash") or snapshot.get("source_hash") or ""),
                rotation_position=int(day.get("number") or len(routines) + 1),
                folder_id=(
                    str(snapshot["folder_id"]) if snapshot.get("folder_id") is not None else None
                ),
                provider_updated_at=snapshot.get("updated_at"),
                snapshot=copy.deepcopy(dict(snapshot)),
                owner_user_id=user_id,
                participation=str(rotation.get(routine_id, {}).get("role") or "full"),
                cadence=mode,
            )
        )

    blocks: list[ProgrammeBlock] = []
    for block in definition.get("blocks") or []:
        blocks.append(
            ProgrammeBlock(
                position=int(block.get("number") or len(blocks) + 1),
                key=str(block.get("key") or f"block-{len(blocks) + 1}"),
                name=str(block.get("name") or block.get("key") or "Block"),
                start_week=int(block.get("start_week") or 1),
                end_week=int(block.get("end_week") or 1),
                objective=str(block.get("focus") or block.get("objective") or ""),
                targets={
                    key: value
                    for key, value in block.items()
                    if key not in _BLOCK_STRUCTURAL_KEYS and key not in _BLOCK_POLICY_KEYS
                },
                adaptation_policy={
                    key: block[key] for key in _BLOCK_POLICY_KEYS if key in block
                },
                exit_criteria={"summary": str(block.get("exit_criteria") or "")},
            )
        )

    prescriptions: list[ExercisePrescription] = []
    for day in definition.get("days") or []:
        for exercise in day.get("exercises") or []:
            source_sets = int(exercise.get("source_sets") or exercise.get("sets") or 1)
            for item in exercise.get("prescriptions") or []:
                progression = dict(item.get("progression") or {})
                progression.setdefault("allowed_change", item.get("allowed_change") or {})
                load_anchor = item.get("load_anchor") or {}
                prescriptions.append(
                    ExercisePrescription(
                        exercise_template_id=str(exercise.get("template_id") or ""),
                        exercise_title=str(exercise.get("name") or exercise.get("template_id") or ""),
                        role=str(exercise.get("role") or "hypertrophy_accessory"),
                        routine_position=int(day.get("number") or 0) or None,
                        block_position=_optional_int(item.get("block_number")),
                        week_number=_optional_int(item.get("week_number")),
                        set_target=_set_target(item, source_sets),
                        rep_target=dict(item.get("rep_target") or {}),
                        effort_target=dict(item.get("effort_target") or {}),
                        load_anchor=dict(load_anchor),
                        rest_seconds=_optional_int(exercise.get("rest_seconds")),
                        progression_method=str(
                            progression.get("method") or "double_progression"
                        ),
                        progression_rules=progression,
                        confidence=ROLE_CONFIDENCE.get(
                            str(item.get("confidence") or exercise.get("role_confidence") or ""),
                            0.5,
                        ),
                        rule_version=str(item.get("rule_version") or ENGINE_VERSION),
                    )
                )

    return ProgrammeDraft(
        name=str(definition.get("name") or "Programme"),
        goal=goal,
        duration_weeks=duration,
        start_date=start,
        routines=routines,
        blocks=blocks,
        prescriptions=prescriptions,
        settings=copy.deepcopy(dict(definition)),
        engine_version=str(definition.get("engine_version") or ENGINE_VERSION),
        reason=reason,
        audit=dict(audit or {}),
    )


def definition_from_revision(
    programme: Any,
    revision: ProgrammeRevision,
    *,
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    """Return an editable deep copy of a revision's definition."""

    settings = revision.settings
    if isinstance(settings, Mapping) and isinstance(settings.get("days"), list):
        return copy.deepcopy(dict(settings))
    from programme_runtime import _definition_from_domain

    return _definition_from_domain(programme, revision, timezone_name=timezone_name)


def build_week_state(
    definition: Mapping[str, Any],
    *,
    start_date: date,
    duration_weeks: int,
    week_number: int,
    timezone_name: str,
    today: date,
    rotation_pointer: int = 0,
    exposures: Sequence[Mapping[str, Any]] = (),
    review_state: str = "planned",
    fatigue: float | None = None,
) -> ProgrammeWeekState:
    """Materialise one week: a pure ``planned`` plan plus the recorded ``actual``."""

    plan = plan_week(
        definition,
        start_date=start_date,
        duration_weeks=duration_weeks,
        week_number=week_number,
        timezone_name=timezone_name,
        rotation_pointer=rotation_pointer,
    )
    updated, actual = summarise_week(plan, exposures, today=today, definition=definition)
    planned = plan.to_dict()
    planned["rotation_pointer"] = rotation_pointer
    actual["slots"] = [item.to_dict() for item in updated.exposures]
    actual["next_exposure"] = (
        updated.next_exposure.to_dict() if updated.next_exposure else None
    )
    return ProgrammeWeekState(
        week_number=week_number,
        planned=planned,
        actual=actual,
        adherence=actual.get("adherence"),
        fatigue=fatigue,
        review_state=review_state,
    )


__all__ = [
    "ROLE_CONFIDENCE",
    "build_week_state",
    "definition_from_revision",
    "definition_to_draft",
]
