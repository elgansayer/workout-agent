"""Tenant-scoped SQL repository for immutable programme revisions.

Every public method requires an authenticated ``user_id``.  Reads, writes and
deletes include that identifier in their predicates; caller-supplied programme
or revision identifiers are never treated as authorisation.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database import DEFAULT_DB_PATH, _connect

from .models import (
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


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _loads(value: Any) -> Any:
    if value in (None, ""):
        return {}
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


class ProgrammeRepository:
    """Storage boundary for programmes and their immutable revisions."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    # ------------------------------------------------------------------
    # Draft creation and revisions
    # ------------------------------------------------------------------

    def create_draft(self, user_id: str, draft: ProgrammeDraft) -> Programme:
        """Persist a complete draft revision in a single transaction."""

        self._assert_snapshot_ownership(user_id, draft)
        with self.transaction() as conn:
            now = _now()
            cursor = conn.execute(
                """
                INSERT INTO programmes
                    (user_id, source, template_key, definition, active,
                     created_at, updated_at, name, status, goal,
                     duration_weeks, start_date, end_date)
                VALUES (?, 'hevy', NULL, ?, 0, ?, ?, ?, 'draft', ?, ?, ?, ?)
                """,
                (
                    user_id,
                    _json(draft.settings),
                    now,
                    now,
                    draft.name,
                    draft.goal,
                    draft.duration_weeks,
                    draft.start_date.isoformat(),
                    draft.end_date.isoformat(),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Failed to persist programme.")
            programme_id = int(cursor.lastrowid)
            revision = self._insert_revision_contents(
                conn,
                user_id=user_id,
                programme_id=programme_id,
                revision_number=1,
                draft=draft,
                reason=draft.reason,
                engine_version=draft.engine_version,
            )
            conn.execute(
                "UPDATE programmes SET current_revision_id = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (revision.id, now, programme_id, user_id),
            )
            return self._load_programme(conn, user_id, programme_id)

    def create_revision(
        self,
        user_id: str,
        programme_id: int,
        draft: ProgrammeDraft,
        *,
        reason: str,
        adaptation: AdaptationDecisionInput | None = None,
    ) -> ProgrammeRevision:
        """Append an immutable revision and optionally an adaptation record."""

        self._assert_snapshot_ownership(user_id, draft)
        with self.transaction() as conn:
            programme = self._programme_row(conn, user_id, programme_id)
            if programme is None:
                raise ProgrammeNotFoundError("Programme not found for this user.")
            if programme["status"] in {"completed", "archived", "migration_required"}:
                raise ProgrammeValidationError(
                    "A completed or archived programme cannot receive a new revision."
                )
            next_number = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision_number), 0) + 1 "
                    "FROM programme_revisions WHERE user_id = ? AND programme_id = ?",
                    (user_id, programme_id),
                ).fetchone()[0]
            )
            previous_id = programme["current_revision_id"]
            revision = self._insert_revision_contents(
                conn,
                user_id=user_id,
                programme_id=programme_id,
                revision_number=next_number,
                draft=draft,
                reason=reason,
                engine_version=draft.engine_version,
            )
            now = _now()
            conn.execute(
                "UPDATE programmes SET current_revision_id = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (revision.id, now, programme_id, user_id),
            )
            if adaptation is not None:
                conn.execute(
                    """
                    INSERT INTO adaptation_decisions
                        (user_id, programme_id, from_revision_id, to_revision_id,
                         scope_json, inputs_json, output_json, confidence,
                         rule_version, explanation, confirmed, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        programme_id,
                        previous_id,
                        revision.id,
                        _json(adaptation.scope),
                        _json(adaptation.inputs),
                        _json(adaptation.output),
                        adaptation.confidence,
                        adaptation.rule_version,
                        adaptation.explanation,
                        1 if adaptation.confirmed else 0,
                        now,
                    ),
                )
            return self._load_revision(conn, revision.id, user_id)

    def _insert_revision_contents(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        programme_id: int,
        revision_number: int,
        draft: ProgrammeDraft,
        reason: str,
        engine_version: str,
    ) -> ProgrammeRevision:
        now = _now()
        source_summary = {
            "selected_routines": [
                {
                    "source_routine_id": routine.source_routine_id,
                    "title": routine.title,
                    "snapshot_hash": routine.snapshot_hash,
                    "provider": routine.provider,
                    "rotation_position": routine.rotation_position,
                }
                for routine in draft.routines
            ],
            "goal": draft.goal,
            "duration_weeks": draft.duration_weeks,
        }
        cursor = conn.execute(
            """
            INSERT INTO programme_revisions
                (user_id, programme_id, revision_number, reason, engine_version,
                 settings_json, source_summary_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                programme_id,
                revision_number,
                reason,
                engine_version,
                _json(draft.settings),
                _json(source_summary),
                now,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Failed to persist programme revision.")
        revision_id = int(cursor.lastrowid)

        for routine in draft.routines:
            conn.execute(
                """
                INSERT INTO programme_routines
                    (user_id, programme_id, revision_id, position, provider,
                     source_routine_id, title, folder_id, provider_updated_at,
                     snapshot_hash, snapshot_json, enabled, participation,
                     cadence, source_conflict_state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    programme_id,
                    revision_id,
                    routine.rotation_position,
                    routine.provider,
                    routine.source_routine_id,
                    routine.title,
                    routine.folder_id,
                    routine.provider_updated_at,
                    routine.snapshot_hash,
                    _json(routine.snapshot),
                    1 if routine.enabled else 0,
                    routine.participation,
                    routine.cadence,
                    routine.source_conflict_state,
                    now,
                ),
            )

        for block in draft.blocks:
            conn.execute(
                """
                INSERT INTO programme_blocks
                    (user_id, programme_id, revision_id, position, key, name,
                     start_week, end_week, objective, targets_json,
                     adaptation_policy_json, exit_criteria_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    programme_id,
                    revision_id,
                    block.position,
                    block.key,
                    block.name,
                    block.start_week,
                    block.end_week,
                    block.objective,
                    _json(block.targets),
                    _json(block.adaptation_policy),
                    _json(block.exit_criteria),
                    now,
                ),
            )

        for prescription in draft.prescriptions:
            conn.execute(
                """
                INSERT INTO exercise_prescriptions
                    (user_id, programme_id, revision_id, routine_position,
                     block_position, week_number, exercise_template_id,
                     exercise_title, role, set_target_json, rep_target_json,
                     effort_target_json, load_anchor_json, rest_seconds,
                     progression_method, progression_rules_json, confidence,
                     rule_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    programme_id,
                    revision_id,
                    prescription.routine_position,
                    prescription.block_position,
                    prescription.week_number,
                    prescription.exercise_template_id,
                    prescription.exercise_title,
                    prescription.role,
                    _json(prescription.set_target),
                    _json(prescription.rep_target),
                    _json(prescription.effort_target),
                    _json(prescription.load_anchor),
                    prescription.rest_seconds,
                    prescription.progression_method,
                    _json(prescription.progression_rules),
                    prescription.confidence,
                    prescription.rule_version,
                    now,
                ),
            )

        return ProgrammeRevision(
            id=revision_id,
            user_id=user_id,
            programme_id=programme_id,
            revision_number=revision_number,
            reason=reason,
            engine_version=engine_version,
            settings=dict(draft.settings),
            source_summary=source_summary,
            created_at=now,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def activate(self, user_id: str, programme_id: int) -> Programme:
        """Activate one programme, archiving any previous active programme."""

        with self.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self._programme_row(conn, user_id, programme_id)
            if programme is None:
                raise ProgrammeNotFoundError("Programme not found for this user.")
            if programme["status"] in {"completed", "archived", "migration_required"}:
                raise ProgrammeValidationError(
                    f"A {programme['status']} programme cannot be activated."
                )
            if programme["current_revision_id"] is None:
                raise ProgrammeValidationError(
                    "A programme needs a current revision before activation."
                )
            now = _now()
            conn.execute(
                """
                UPDATE programmes
                SET status = 'archived', active = 0, archived_at = ?,
                    updated_at = ?
                WHERE user_id = ? AND status = 'active' AND id <> ?
                """,
                (now, now, user_id, programme_id),
            )
            conn.execute(
                """
                UPDATE programmes
                SET status = 'active', active = 1, archived_at = NULL,
                    activated_at = COALESCE(activated_at, ?), updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (now, now, programme_id, user_id),
            )
            return self._load_programme(conn, user_id, programme_id)

    def set_status(
        self,
        user_id: str,
        programme_id: int,
        status: str,
        *,
        archive: bool = False,
    ) -> Programme:
        with self.transaction() as conn:
            programme = self._programme_row(conn, user_id, programme_id)
            if programme is None:
                raise ProgrammeNotFoundError("Programme not found for this user.")
            now = _now()
            if status == "active":
                raise ProgrammeValidationError("Use activate() to activate a programme.")
            conn.execute(
                """
                UPDATE programmes
                SET status = ?, active = 0,
                    archived_at = CASE WHEN ? THEN ? ELSE archived_at END,
                    updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (status, 1 if archive else 0, now, now, programme_id, user_id),
            )
            return self._load_programme(conn, user_id, programme_id)

    # ------------------------------------------------------------------
    # Tenant-scoped reads
    # ------------------------------------------------------------------

    def get_programme(self, user_id: str, programme_id: int) -> Programme | None:
        with self.transaction() as conn:
            row = self._programme_row(conn, user_id, programme_id)
            if row is None:
                return None
            return self._load_programme(conn, user_id, programme_id)

    def get_active_programme(self, user_id: str) -> Programme | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM programmes "
                "WHERE user_id = ? AND status = 'active' "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            return self._load_programme(conn, user_id, int(row["id"]))

    def list_revisions(
        self, user_id: str, programme_id: int
    ) -> list[ProgrammeRevision]:
        with self.transaction() as conn:
            if self._programme_row(conn, user_id, programme_id) is None:
                return []
            rows = conn.execute(
                """
                SELECT id FROM programme_revisions
                WHERE user_id = ? AND programme_id = ?
                ORDER BY revision_number ASC
                """,
                (user_id, programme_id),
            ).fetchall()
            return [
                self._load_revision(conn, int(row["id"]), user_id) for row in rows
            ]

    def get_revision(
        self, user_id: str, programme_id: int, revision_number: int
    ) -> ProgrammeRevision | None:
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT id FROM programme_revisions
                WHERE user_id = ? AND programme_id = ? AND revision_number = ?
                """,
                (user_id, programme_id, revision_number),
            ).fetchone()
            if row is None:
                return None
            return self._load_revision(conn, int(row["id"]), user_id)

    def get_programme_week(
        self,
        user_id: str,
        programme_id: int,
        week_number: int,
        *,
        revision_number: int | None = None,
    ) -> ProgrammeWeek | None:
        with self.transaction() as conn:
            programme = self._programme_row(conn, user_id, programme_id)
            if programme is None:
                return None
            if revision_number is None:
                revision_row = None
                if programme["current_revision_id"] is not None:
                    revision_row = conn.execute(
                        "SELECT * FROM programme_revisions WHERE id = ? AND user_id = ?",
                        (programme["current_revision_id"], user_id),
                    ).fetchone()
            else:
                revision_row = conn.execute(
                    """
                    SELECT * FROM programme_revisions
                    WHERE user_id = ? AND programme_id = ? AND revision_number = ?
                    """,
                    (user_id, programme_id, revision_number),
                ).fetchone()
            if revision_row is None:
                return None
            revision = self._load_revision(conn, int(revision_row["id"]), user_id)
            duration = int(programme["duration_weeks"] or 0)
            if not programme["start_date"] or duration < 1:
                raise ProgrammeValidationError(
                    "This programme has no dated schedule to materialise."
                )
            if week_number < 1 or week_number > duration:
                raise ProgrammeValidationError(
                    f"Week {week_number} is outside the programme duration."
                )
            start_date = date.fromisoformat(str(programme["start_date"]))
            week_start = start_date + timedelta(weeks=week_number - 1)
            week_end = week_start + timedelta(days=6)
            state_row = conn.execute(
                """
                SELECT * FROM programme_week_states
                WHERE user_id = ? AND programme_id = ? AND revision_id = ?
                  AND week_number = ?
                """,
                (user_id, programme_id, revision.id, week_number),
            ).fetchone()
            return ProgrammeWeek(
                programme_id=programme_id,
                revision_id=revision.id,
                revision_number=revision.revision_number,
                week_number=week_number,
                start_date=week_start,
                end_date=week_end,
                blocks=[
                    block
                    for block in revision.blocks
                    if block.start_week <= week_number <= block.end_week
                ],
                routines=list(revision.routines),
                prescriptions=[
                    prescription
                    for prescription in revision.prescriptions
                    if prescription.week_number in (None, week_number)
                ],
                state=self._row_to_week_state(state_row) if state_row else None,
            )

    # ------------------------------------------------------------------
    # Week state and matches
    # ------------------------------------------------------------------

    def save_week_state(
        self,
        user_id: str,
        programme_id: int,
        revision_id: int,
        state: ProgrammeWeekState,
    ) -> ProgrammeWeekState:
        with self.transaction() as conn:
            if not self._revision_belongs_to(
                conn, user_id, programme_id, revision_id
            ):
                raise ProgrammeNotFoundError(
                    "Revision not found for this user and programme."
                )
            now = _now()
            conn.execute(
                """
                INSERT INTO programme_week_states
                    (user_id, programme_id, revision_id, week_number,
                     planned_json, actual_json, adherence, fatigue,
                     review_state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(revision_id, week_number) DO UPDATE SET
                    planned_json = excluded.planned_json,
                    actual_json = excluded.actual_json,
                    adherence = excluded.adherence,
                    fatigue = excluded.fatigue,
                    review_state = excluded.review_state,
                    updated_at = excluded.updated_at
                WHERE programme_week_states.user_id = excluded.user_id
                """,
                (
                    user_id,
                    programme_id,
                    revision_id,
                    state.week_number,
                    _json(state.planned),
                    _json(state.actual),
                    state.adherence,
                    state.fatigue,
                    state.review_state,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM programme_week_states
                WHERE user_id = ? AND programme_id = ? AND revision_id = ?
                  AND week_number = ?
                """,
                (user_id, programme_id, revision_id, state.week_number),
            ).fetchone()
            return self._row_to_week_state(row)

    def save_routine_workout_match(
        self, user_id: str, match: RoutineWorkoutMatch
    ) -> RoutineWorkoutMatch:
        with self.transaction() as conn:
            if not self._revision_belongs_to(
                conn, user_id, match.programme_id, match.revision_id
            ):
                raise ProgrammeNotFoundError(
                    "Revision not found for this user and programme."
                )
            routine_row = conn.execute(
                """
                SELECT 1 FROM programme_routines
                WHERE user_id = ? AND programme_id = ? AND revision_id = ?
                  AND position = ? AND source_routine_id = ?
                """,
                (
                    user_id,
                    match.programme_id,
                    match.revision_id,
                    match.routine_position,
                    match.source_routine_id,
                ),
            ).fetchone()
            if routine_row is None:
                raise ProgrammeValidationError(
                    "The routine does not belong to this revision."
                )
            now = _now()
            conn.execute(
                """
                INSERT INTO routine_workout_matches
                    (user_id, programme_id, revision_id, routine_position,
                     source_routine_id, workout_id, score, confidence, method,
                     user_confirmed, matcher_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, programme_id, workout_id) DO UPDATE SET
                    revision_id = excluded.revision_id,
                    routine_position = excluded.routine_position,
                    source_routine_id = excluded.source_routine_id,
                    score = excluded.score,
                    confidence = excluded.confidence,
                    method = excluded.method,
                    user_confirmed = excluded.user_confirmed,
                    matcher_version = excluded.matcher_version,
                    created_at = excluded.created_at
                """,
                (
                    user_id,
                    match.programme_id,
                    match.revision_id,
                    match.routine_position,
                    match.source_routine_id,
                    match.workout_id,
                    match.score,
                    match.confidence,
                    match.method,
                    1 if match.user_confirmed else 0,
                    match.matcher_version,
                    now,
                ),
            )
            return RoutineWorkoutMatch(
                programme_id=match.programme_id,
                revision_id=match.revision_id,
                routine_position=match.routine_position,
                source_routine_id=match.source_routine_id,
                workout_id=match.workout_id,
                score=match.score,
                confidence=match.confidence,
                method=match.method,
                user_confirmed=match.user_confirmed,
                matcher_version=match.matcher_version,
                created_at=now,
            )

    def list_routine_workout_matches(
        self, user_id: str, programme_id: int
    ) -> list[RoutineWorkoutMatch]:
        with self.transaction() as conn:
            if self._programme_row(conn, user_id, programme_id) is None:
                return []
            rows = conn.execute(
                """
                SELECT * FROM routine_workout_matches
                WHERE user_id = ? AND programme_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user_id, programme_id),
            ).fetchall()
            return [
                RoutineWorkoutMatch(
                    programme_id=int(row["programme_id"]),
                    revision_id=int(row["revision_id"]),
                    routine_position=int(row["routine_position"]),
                    source_routine_id=str(row["source_routine_id"]),
                    workout_id=str(row["workout_id"]),
                    score=float(row["score"]),
                    confidence=float(row["confidence"]),
                    method=str(row["method"]),
                    user_confirmed=bool(row["user_confirmed"]),
                    matcher_version=str(row["matcher_version"]),
                    created_at=str(row["created_at"]),
                )
                for row in rows
            ]

    def list_adaptation_decisions(
        self, user_id: str, programme_id: int
    ) -> list[AdaptationDecision]:
        with self.transaction() as conn:
            if self._programme_row(conn, user_id, programme_id) is None:
                return []
            rows = conn.execute(
                """
                SELECT * FROM adaptation_decisions
                WHERE user_id = ? AND programme_id = ?
                ORDER BY id ASC
                """,
                (user_id, programme_id),
            ).fetchall()
            return [
                AdaptationDecision(
                    id=int(row["id"]),
                    programme_id=int(row["programme_id"]),
                    from_revision_id=row["from_revision_id"],
                    to_revision_id=int(row["to_revision_id"]),
                    scope=_loads(row["scope_json"]),
                    inputs=_loads(row["inputs_json"]),
                    output=_loads(row["output_json"]),
                    confidence=float(row["confidence"]),
                    rule_version=str(row["rule_version"]),
                    explanation=str(row["explanation"]),
                    confirmed=bool(row["confirmed"]),
                    created_at=str(row["created_at"]),
                )
                for row in rows
            ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_snapshot_ownership(user_id: str, draft: ProgrammeDraft) -> None:
        for routine in draft.routines:
            if (
                routine.owner_user_id is not None
                and routine.owner_user_id != user_id
            ):
                raise ProgrammeValidationError(
                    "A programme revision cannot reference a routine snapshot "
                    "owned by another user."
                )

    def _programme_row(
        self, conn: sqlite3.Connection, user_id: str, programme_id: int
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM programmes WHERE id = ? AND user_id = ?",
            (programme_id, user_id),
        ).fetchone()

    def _revision_belongs_to(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        programme_id: int,
        revision_id: int,
    ) -> bool:
        return (
            conn.execute(
                """
                SELECT 1 FROM programme_revisions
                WHERE id = ? AND user_id = ? AND programme_id = ?
                """,
                (revision_id, user_id, programme_id),
            ).fetchone()
            is not None
        )

    def _load_programme(
        self, conn: sqlite3.Connection, user_id: str, programme_id: int
    ) -> Programme:
        row = self._programme_row(conn, user_id, programme_id)
        if row is None:
            raise ProgrammeNotFoundError("Programme not found for this user.")
        revision = None
        if row["current_revision_id"] is not None:
            revision = self._load_revision(
                conn, int(row["current_revision_id"]), user_id
            )
        return Programme(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            name=str(row["name"] or ""),
            status=str(row["status"]),
            goal=row["goal"],
            duration_weeks=row["duration_weeks"],
            start_date=date.fromisoformat(row["start_date"])
            if row["start_date"]
            else None,
            end_date=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
            current_revision_id=row["current_revision_id"],
            current_revision=revision,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            activated_at=row["activated_at"],
            archived_at=row["archived_at"],
            predecessor_programme_id=row["predecessor_programme_id"],
        )

    def _load_revision(
        self, conn: sqlite3.Connection, revision_id: int, user_id: str
    ) -> ProgrammeRevision:
        row = conn.execute(
            "SELECT * FROM programme_revisions WHERE id = ? AND user_id = ?",
            (revision_id, user_id),
        ).fetchone()
        if row is None:
            raise ProgrammeNotFoundError("Revision not found for this user.")
        programme_id = int(row["programme_id"])
        routines = [
            ProgrammeRoutine(
                source_routine_id=str(routine["source_routine_id"]),
                title=str(routine["title"]),
                snapshot_hash=str(routine["snapshot_hash"]),
                rotation_position=int(routine["position"]),
                provider=str(routine["provider"]),
                folder_id=routine["folder_id"],
                provider_updated_at=routine["provider_updated_at"],
                snapshot=_loads(routine["snapshot_json"]),
                enabled=bool(routine["enabled"]),
                participation=str(routine["participation"]),
                cadence=str(routine["cadence"]),
                source_conflict_state=str(routine["source_conflict_state"]),
            )
            for routine in conn.execute(
                """
                SELECT * FROM programme_routines
                WHERE user_id = ? AND programme_id = ? AND revision_id = ?
                ORDER BY position ASC
                """,
                (user_id, programme_id, revision_id),
            ).fetchall()
        ]
        blocks = [
            ProgrammeBlock(
                position=int(block["position"]),
                key=str(block["key"]),
                name=str(block["name"]),
                start_week=int(block["start_week"]),
                end_week=int(block["end_week"]),
                objective=str(block["objective"]),
                targets=_loads(block["targets_json"]),
                adaptation_policy=_loads(block["adaptation_policy_json"]),
                exit_criteria=_loads(block["exit_criteria_json"]),
            )
            for block in conn.execute(
                """
                SELECT * FROM programme_blocks
                WHERE user_id = ? AND programme_id = ? AND revision_id = ?
                ORDER BY position ASC
                """,
                (user_id, programme_id, revision_id),
            ).fetchall()
        ]
        prescriptions = [
            ExercisePrescription(
                exercise_template_id=str(prescription["exercise_template_id"]),
                exercise_title=str(prescription["exercise_title"]),
                role=str(prescription["role"]),
                routine_position=prescription["routine_position"],
                block_position=prescription["block_position"],
                week_number=prescription["week_number"],
                set_target=_loads(prescription["set_target_json"]),
                rep_target=_loads(prescription["rep_target_json"]),
                effort_target=_loads(prescription["effort_target_json"]),
                load_anchor=_loads(prescription["load_anchor_json"]),
                rest_seconds=prescription["rest_seconds"],
                progression_method=str(prescription["progression_method"]),
                progression_rules=_loads(prescription["progression_rules_json"]),
                confidence=float(prescription["confidence"]),
                rule_version=str(prescription["rule_version"]),
            )
            for prescription in conn.execute(
                """
                SELECT * FROM exercise_prescriptions
                WHERE user_id = ? AND programme_id = ? AND revision_id = ?
                ORDER BY id ASC
                """,
                (user_id, programme_id, revision_id),
            ).fetchall()
        ]
        return ProgrammeRevision(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            programme_id=programme_id,
            revision_number=int(row["revision_number"]),
            reason=str(row["reason"]),
            engine_version=str(row["engine_version"]),
            settings=_loads(row["settings_json"]),
            source_summary=_loads(row["source_summary_json"]),
            created_at=str(row["created_at"]),
            routines=routines,
            blocks=blocks,
            prescriptions=prescriptions,
        )

    @staticmethod
    def _row_to_week_state(row: sqlite3.Row) -> ProgrammeWeekState:
        return ProgrammeWeekState(
            week_number=int(row["week_number"]),
            planned=_loads(row["planned_json"]),
            actual=_loads(row["actual_json"]),
            adherence=row["adherence"],
            fatigue=row["fatigue"],
            review_state=str(row["review_state"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
