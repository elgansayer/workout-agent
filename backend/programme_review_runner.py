"""Run the deterministic adaptation review for one tenant's active programme.

The review reads the tenant's own locally cached Hevy workouts (the incremental
#819 cache), matches them to the *active revision's* routine snapshots with the
#972 matcher, and hands the result to
:class:`programme_domain.AdaptationService`.  It makes no provider request and
never calls a language model; recovery evidence is optional and absent unless a
caller supplies it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import database
from hevy_history import (
    MatchOverride,
    RoutineSnapshot,
    SnapshotExercise,
    match_workouts,
    parse_cached_workouts,
)
from programme_domain import AdaptationService, ProgrammeService, WorkoutMatch
from programme_domain import adaptation as engine
from programme_domain.review import ReviewResult

logger = logging.getLogger(__name__)


def _snapshots(revision: Any) -> dict[str, tuple[RoutineSnapshot, int]]:
    """The active revision's routines as matcher snapshots, keyed by routine ID."""

    result: dict[str, tuple[RoutineSnapshot, int]] = {}
    for routine in sorted(revision.routines, key=lambda item: item.rotation_position):
        exercises = tuple(
            SnapshotExercise(
                template_id=str(item.get("template_id") or ""),
                title=str(item.get("title") or ""),
                sets=int(item.get("sets") or 1),
                position=index,
                superset_id=item.get("superset_id"),
                exercise_type=item.get("exercise_type"),
                primary_muscle_group=item.get("primary_muscle_group"),
                equipment=item.get("equipment"),
            )
            for index, item in enumerate((routine.snapshot or {}).get("exercises") or [])
        )
        snapshot = RoutineSnapshot(
            id=f"{routine.source_routine_id}:{routine.snapshot_hash[:16]}",
            routine_id=routine.source_routine_id,
            title=routine.title,
            content_hash=routine.snapshot_hash,
            exercises=exercises,
            updated_at=routine.provider_updated_at,
        )
        result[routine.source_routine_id] = (snapshot, routine.rotation_position)
    return result


def match_for_programme(
    user_id: str,
    db_path: str,
    programme: Any,
    workouts: Sequence[Any],
) -> dict[str, WorkoutMatch]:
    """Match workouts to the programme's routines; user confirmations win."""

    revision = programme.current_revision
    snapshots = _snapshots(revision)
    by_snapshot = {snapshot.id: position for snapshot, position in snapshots.values()}
    snapshot_for_routine = {rid: snap.id for rid, (snap, _pos) in snapshots.items()}
    overrides: list[MatchOverride] = []
    for row in database.get_routine_match_overrides(user_id, db_path):
        target = snapshot_for_routine.get(str(row.get("routine_id")))
        if target:
            overrides.append(
                MatchOverride(
                    pattern_type=str(row["pattern_type"]),
                    pattern_value=str(row["pattern_value"]),
                    routine_snapshot_id=target,
                )
            )
    matches = match_workouts(
        [snapshot for snapshot, _position in snapshots.values()],
        workouts,
        overrides=overrides,
    )
    result: dict[str, WorkoutMatch] = {}
    for match in matches:
        candidate = match.routine_snapshot_id or match.candidate_snapshot_id
        result[match.workout_id] = WorkoutMatch(
            workout_id=match.workout_id,
            confidence=match.confidence,
            routine_position=by_snapshot.get(candidate or ""),
            score=float(match.score),
            method=match.method,
            user_confirmed=bool(match.user_confirmed),
            matcher_version=match.matcher_version,
        )
    # A confirmation recorded against this programme is authoritative.
    for stored in ProgrammeService(db_path).list_routine_workout_matches(
        user_id, programme.id
    ):
        if stored.user_confirmed:
            result[stored.workout_id] = WorkoutMatch(
                workout_id=stored.workout_id,
                confidence=engine.MATCH_HIGH,
                routine_position=stored.routine_position,
                score=stored.score,
                method=stored.method,
                user_confirmed=True,
                matcher_version=stored.matcher_version,
            )
    return result


def run_review(
    user_id: str,
    db_path: str,
    *,
    now: datetime | None = None,
    workouts: Sequence[Any] | None = None,
    deleted_workout_ids: Sequence[str] = (),
    recovery: engine.RecoveryEvidence | None = None,
) -> ReviewResult | None:
    """Evaluate the tenant's completed workouts against the active programme.

    Returns ``None`` when the tenant has no active domain programme.  Replaying
    the same workouts is a no-op, so this is safe to run on every daily cycle.
    """

    service = ProgrammeService(db_path)
    programme = service.get_active_programme(user_id)
    if programme is None or programme.current_revision is None:
        return None
    if workouts is None:
        since = programme.start_date.isoformat() if programme.start_date else None
        workouts = parse_cached_workouts(
            database.get_cached_workout_payloads(user_id, db_path, since=since)
        )
    adaptation = AdaptationService(db_path)
    moment = now or datetime.now(tz=timezone.utc)
    if deleted_workout_ids:
        adaptation.retract_workouts(
            user_id, programme.id, list(deleted_workout_ids), now=moment
        )
    matches: Mapping[str, WorkoutMatch] = match_for_programme(
        user_id, db_path, programme, workouts
    )
    return adaptation.review(
        user_id,
        programme.id,
        workouts,
        matches,
        now=moment,
        recovery=recovery,
    )


__all__ = ["match_for_programme", "run_review"]
