"""Programme lifecycle: week materialisation, pause/resume and extension.

Pausing and resuming never delete or rewrite history.  Extension appends a new
immutable revision (the same mechanism as adaptation and rebase) that lengthens
the block containing the current week and shifts the later blocks; completed
weeks keep the state recorded against the revision that produced them.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from typing import Any

from . import adaptation as engine
from .activation import build_week_state, definition_from_revision, definition_to_draft
from .models import (
    STATE_CONFIRMED,
    AdaptationDecisionInput,
    Programme,
    ProgrammeNotFoundError,
    ProgrammeValidationError,
)
from .repository import ProgrammeRepository
from .review import AdaptationService, decision_view
from .weeks import local_date_of, paused_weeks, rolling_pointer, week_for_date

MAX_EXTENSION_WEEKS = 8
MAX_PROGRAMME_WEEKS = 52


def extend_definition(
    definition: dict[str, Any], extra_weeks: int, at_week: int
) -> dict[str, Any]:
    """Lengthen the block containing ``at_week`` and shift every later block."""

    result = copy.deepcopy(definition)
    blocks = sorted(result.get("blocks") or [], key=lambda item: int(item.get("number") or 0))
    if not blocks:
        raise ProgrammeValidationError("The programme has no blocks to extend.")
    total = int(result.get("cycle_weeks") or 0)
    target = next(
        (
            block
            for block in blocks
            if int(block["start_week"]) <= at_week <= int(block["end_week"])
        ),
        blocks[-1],
    )
    start = date.fromisoformat(str((result.get("programme_spec") or {}).get("start_date")))
    target_end = int(target["end_week"])  # captured before the target grows
    for block in blocks:
        if int(block["start_week"]) > target_end:
            block["start_week"] = int(block["start_week"]) + extra_weeks
            block["end_week"] = int(block["end_week"]) + extra_weeks
        elif block is target:
            block["end_week"] = target_end + extra_weeks
    for block in blocks:
        first, last = int(block["start_week"]), int(block["end_week"])
        block["duration_weeks"] = last - first + 1
        block["weeks"] = str(first) if first == last else f"{first}-{last}"
        block["start_date"] = (start + timedelta(weeks=first - 1)).isoformat()
        block["end_date"] = (start + timedelta(weeks=last) - timedelta(days=1)).isoformat()
    new_total = total + extra_weeks
    result["blocks"] = blocks
    result["cycle_weeks"] = new_total
    spec = dict(result.get("programme_spec") or {})
    spec["duration_weeks"] = new_total
    result["programme_spec"] = spec
    cadence = result.get("cadence")
    if isinstance(cadence, dict) and cadence.get("rotation"):
        from dynamic_programme import _build_schedule

        result["schedule"] = _build_schedule(cadence, start, new_total)
    return result


class LifecycleService:
    """Pause, resume, extend and materialise weeks for one tenant's programme."""

    def __init__(
        self,
        db_path: str,
        repository: ProgrammeRepository | None = None,
        adaptation: AdaptationService | None = None,
    ) -> None:
        self.repository = repository or ProgrammeRepository(db_path)
        self.db_path = db_path
        self.adaptation = adaptation or AdaptationService(db_path, self.repository)

    def _event(
        self,
        programme: Programme,
        user_id: str,
        decision_type: str,
        output: dict[str, Any],
        explanation: str,
        week: int = 0,
        inputs: dict[str, Any] | None = None,
    ) -> AdaptationDecisionInput:
        revision = programme.current_revision
        return AdaptationDecisionInput(
            scope={
                "level": "programme",
                "user_id": user_id,
                "programme_id": programme.id,
                "revision_number": revision.revision_number if revision else None,
                "week_number": week,
            },
            inputs=inputs or {},
            output={"decision_type": decision_type, **output},
            confidence=1.0,
            rule_version=engine.RULE_VERSION,
            explanation=explanation,
            confirmed=True,
            decision_type=decision_type,
            application_state=STATE_CONFIRMED,
            week_number=week,
        )

    def _timezone(self, conn: Any, user_id: str) -> str:
        row = conn.execute("SELECT timezone FROM users WHERE id = ?", (user_id,)).fetchone()
        return str((row["timezone"] if row else None) or "UTC")

    # ---- pause / resume ------------------------------------------------------

    def pause(self, user_id: str, programme_id: int, *, now: datetime) -> Programme:
        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self.repository.programme_in(conn, user_id, programme_id)
            if programme.status != "active" or programme.current_revision is None:
                raise ProgrammeValidationError("Only the active programme can be paused.")
            today = local_date_of(now, self._timezone(conn, user_id)) or now.date()
            week = week_for_date(
                programme.start_date or today, int(programme.duration_weeks or 0), today
            )
            paused = self.repository.set_status_in(conn, user_id, programme_id, "paused")
            revision = programme.current_revision
            self.repository.record_decision_in(
                conn,
                user_id,
                programme_id,
                revision.id,
                revision.id,
                self._event(
                    programme,
                    user_id,
                    "pause",
                    {"paused_on": today.isoformat()},
                    "The user paused the programme. History and revisions are untouched.",
                    week or 0,
                    {"paused_on": today.isoformat()},
                ),
            )
            return paused

    def resume(
        self,
        user_id: str,
        programme_id: int,
        *,
        now: datetime,
        extend: bool = True,
    ) -> Programme:
        """Resume a paused programme; whole weeks lost to the pause can extend it."""

        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self.repository.programme_in(conn, user_id, programme_id)
            if programme.status != "paused" or programme.current_revision is None:
                raise ProgrammeValidationError("Only a paused programme can be resumed.")
            timezone_name = self._timezone(conn, user_id)
            today = local_date_of(now, timezone_name) or now.date()
            pauses = self.repository.list_decisions_in(
                conn, user_id, programme_id, decision_types=("pause",)
            )
            paused_on = None
            if pauses:
                try:
                    paused_on = date.fromisoformat(str(pauses[-1].output.get("paused_on")))
                except ValueError:
                    paused_on = None
            lost = paused_weeks(paused_on, today) if paused_on else 0
            revision = programme.current_revision
            resumed = self.repository.resume_in(conn, user_id, programme_id)
            extended_by = 0
            current_week = week_for_date(
                programme.start_date or today, int(programme.duration_weeks or 0), today
            ) or int(programme.duration_weeks or 1)
            if extend and lost >= 1:
                extended_by = min(
                    lost,
                    MAX_EXTENSION_WEEKS,
                    MAX_PROGRAMME_WEEKS - int(programme.duration_weeks or 0),
                )
            if extended_by > 0:
                self._extend_in(
                    conn, user_id, programme, extended_by, current_week, timezone_name,
                    reason="resume", note=f"Extended by {extended_by} week(s) after a pause.",
                )
            self.repository.record_decision_in(
                conn,
                user_id,
                programme_id,
                revision.id,
                revision.id,
                self._event(
                    programme,
                    user_id,
                    "resume",
                    {"resumed_on": today.isoformat(), "weeks_lost": lost, "extended_by": extended_by},
                    "The user resumed the programme; the rotation continues from the last completed routine.",
                    current_week,
                    {"paused_on": paused_on.isoformat() if paused_on else None},
                ),
            )
            return resumed

    # ---- extension -------------------------------------------------------------

    def extend(
        self,
        user_id: str,
        programme_id: int,
        weeks: int,
        *,
        now: datetime,
    ) -> Programme:
        """Extend the programme through a new immutable revision."""

        if not 1 <= weeks <= MAX_EXTENSION_WEEKS:
            raise ProgrammeValidationError(
                f"An extension must be between 1 and {MAX_EXTENSION_WEEKS} weeks."
            )
        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self.repository.programme_in(conn, user_id, programme_id)
            if programme.status not in {"active", "paused"} or programme.current_revision is None:
                raise ProgrammeValidationError("Only an active or paused programme can be extended.")
            if int(programme.duration_weeks or 0) + weeks > MAX_PROGRAMME_WEEKS:
                raise ProgrammeValidationError(
                    f"A programme cannot exceed {MAX_PROGRAMME_WEEKS} weeks."
                )
            timezone_name = self._timezone(conn, user_id)
            today = local_date_of(now, timezone_name) or now.date()
            week = week_for_date(
                programme.start_date or today, int(programme.duration_weeks or 0), today
            ) or int(programme.duration_weeks or 1)
            self._extend_in(
                conn, user_id, programme, weeks, week, timezone_name,
                reason="extension", note=f"Extended by {weeks} week(s) at the user's request.",
            )
            return self.repository.programme_in(conn, user_id, programme_id)

    def _extend_in(
        self,
        conn: Any,
        user_id: str,
        programme: Programme,
        weeks: int,
        at_week: int,
        timezone_name: str,
        *,
        reason: str,
        note: str,
    ) -> None:
        revision = programme.current_revision
        if revision is None:  # pragma: no cover - guarded by callers
            raise ProgrammeValidationError("The programme has no revision.")
        definition = definition_from_revision(programme, revision, timezone_name=timezone_name)
        extended = extend_definition(definition, weeks, at_week)
        draft = definition_to_draft(
            extended,
            user_id,
            reason=reason,
            audit={"event": reason, "added_weeks": weeks, "from_revision": revision.revision_number},
        )
        self.repository.create_revision_in(
            conn,
            user_id,
            programme.id,
            draft,
            reason=reason,
            adaptation=self._event(
                programme,
                user_id,
                "extension",
                {
                    "added_weeks": weeks,
                    "new_duration_weeks": draft.duration_weeks,
                    "trigger": reason,
                    "rationale_codes": [],
                },
                note,
                at_week,
                {"requested_weeks": weeks},
            ),
        )

    # ---- week materialisation ---------------------------------------------------

    def materialise_week(
        self,
        user_id: str,
        programme_id: int,
        week_number: int,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Return the dated week with effective prescriptions (read-only).

        A stored week state is returned as recorded; a missing one is derived
        deterministically from the stored revision and the recorded exposures.
        """

        with self.repository.transaction() as conn:
            programme = self.repository.programme_in(conn, user_id, programme_id)
            revision = programme.current_revision
            if revision is None or programme.start_date is None:
                raise ProgrammeNotFoundError("Programme has no materialised revision.")
            duration = int(programme.duration_weeks or 0)
            if not 1 <= week_number <= duration:
                raise ProgrammeValidationError(
                    f"Week {week_number} is outside the programme duration."
                )
            timezone_name = self._timezone(conn, user_id)
            today = local_date_of(now, timezone_name) or now.date()
            definition = definition_from_revision(programme, revision, timezone_name=timezone_name)
            stored = self.repository.get_week_state_in(
                conn, user_id, programme_id, revision.id, week_number
            ) or self.repository.get_latest_week_state_in(
                conn, user_id, programme_id, week_number
            )
            if stored is None:
                exposures = self.adaptation.exposures_for_week(conn, user_id, programme_id, week_number)
                earlier: list[int | None] = []
                for week in range(1, week_number):
                    earlier.extend(
                        item.get("routine_position")
                        for item in self.adaptation.exposures_for_week(conn, user_id, programme_id, week)
                        if item.get("status") in {"completed", "partial", "substituted"}
                    )
                stored = build_week_state(
                    definition,
                    start_date=programme.start_date,
                    duration_weeks=duration,
                    week_number=week_number,
                    timezone_name=timezone_name,
                    today=today,
                    rotation_pointer=rolling_pointer(definition.get("days") or [], earlier),
                    exposures=exposures,
                )
            decisions = self.repository.list_decisions_in(conn, user_id, programme_id)
            overrides = self.repository.list_overrides_in(conn, user_id, programme_id, active_only=True)
            sessions: list[dict[str, Any]] = []
            planned = (stored.actual.get("slots") or stored.planned.get("exposures") or [])
            for slot in planned:
                day = next(
                    (
                        item
                        for item in definition.get("days") or []
                        if int(item.get("number") or 0) == int(slot.get("routine_position") or 0)
                    ),
                    None,
                )
                exercises: list[dict[str, Any]] = []
                for exercise in (day or {}).get("exercises") or []:
                    effective = self.adaptation.effective_prescription(
                        definition,
                        decisions,
                        revision,
                        template_id=str(exercise.get("template_id")),
                        routine_position=int(slot.get("routine_position") or 0),
                        week=week_number,
                    )
                    if effective is None:
                        continue
                    prescription, folded = effective
                    pins = sorted(
                        {
                            field_name
                            for item in overrides
                            if item.kind == "pin"
                            and item.exercise_template_id == prescription.template_id
                            and item.is_active_on(today)
                            for field_name in item.value.get("fields", [])
                        }
                    )
                    exercises.append(
                        {
                            "template_id": prescription.template_id,
                            "name": prescription.title,
                            "sets": prescription.sets,
                            "rep_range": (
                                f"{prescription.rep_min}-{prescription.rep_max}"
                                if prescription.rep_min is not None and prescription.rep_max is not None
                                else None
                            ),
                            "rep_target": folded.get("rep_target"),
                            "load_kg": prescription.load_kg,
                            "pinned_fields": pins,
                            "fatigue_management": bool(folded.get("fatigue_management")),
                            "adjustments": [
                                item["decision_type"]
                                for item in self.adaptation.applied_for(
                                    decisions,
                                    revision.id,
                                    prescription.template_id,
                                    int(slot.get("routine_position") or 0),
                                )
                            ],
                        }
                    )
                sessions.append({**slot, "exercises": exercises})
            first = programme.start_date + timedelta(weeks=week_number - 1)
            return {
                "programme_id": programme.id,
                "revision_id": revision.id,
                "revision_number": revision.revision_number,
                "week_number": week_number,
                "start_date": first.isoformat(),
                "end_date": (first + timedelta(days=6)).isoformat(),
                "timezone": timezone_name,
                "cadence": stored.planned.get("cadence"),
                "block": stored.planned.get("block"),
                "review_state": stored.review_state,
                "adherence": stored.adherence,
                "fatigue": stored.fatigue,
                "planned": stored.planned,
                "actual": {
                    key: value for key, value in stored.actual.items() if key != "slots"
                },
                "sessions": sessions,
                "next_exposure": stored.actual.get("next_exposure")
                or stored.planned.get("next_exposure"),
                "pending_decisions": [
                    decision_view(item)
                    for item in decisions
                    if item.application_state == "pending_confirmation"
                ],
            }


__all__ = ["LifecycleService", "extend_definition"]
