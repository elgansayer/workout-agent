"""Read-first Hevy integration: understand what the user actually trains.

Instead of pushing a static programme TO Hevy (the old ``hevy_sync.py`` flow),
this module pulls the user's routines, recent workouts, and exercise templates
FROM Hevy, then distills them into a structured ``HevyTrainingData`` object
that the rest of the system can reason about.

The data is fetched per-user using their stored API key (from Sprint 1).

``fetch_user_training_snapshot`` is the entry point used by the programme
builder: it classifies provider failures and reports whether the account was
read completely. ``fetch_user_training`` keeps the older best-effort contract
for callers that cannot handle a classified error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from hevy_client import (
    HevyAuthError,
    HevyError,
    HevyRateLimitError,
    get_exercise_templates,
    get_exercise_templates_strict,
    get_recent_workouts,
    get_recent_workouts_strict,
    get_routine_folders,
    get_routine_folders_strict,
    get_routines,
    get_routines_strict,
    get_user_info,
    get_user_info_strict,
    get_workout_count,
    get_workout_count_strict,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ExerciseTemplate:
    """Metadata for a single exercise from the Hevy template library."""

    id: str
    title: str
    exercise_type: str  # "weight_reps", "bodyweight_reps", "duration", etc.
    primary_muscle_group: str
    secondary_muscle_groups: list[str] = field(default_factory=list)
    equipment: str | None = None
    is_custom: bool = False


@dataclass
class RoutineExercise:
    """A single exercise within a routine template."""

    template_id: str
    title: str  # Resolved from the template
    sets: int
    rest_seconds: int
    notes: str | None = None
    superset_id: int | None = None
    # Planned targets (from the routine, not logged values)
    target_weight_kg: float | None = None
    target_reps: int | None = None
    target_rep_range: tuple[int, int] | None = None
    # Exact provider set payloads are retained for source preview and hashing.
    set_targets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Routine:
    """A routine template as it exists in Hevy."""

    id: str
    title: str
    folder_id: int | None = None
    exercises: list[RoutineExercise] = field(default_factory=list)
    updated_at: str | None = None


@dataclass
class WorkoutExercise:
    """A single exercise from a completed workout."""

    template_id: str
    title: str
    sets: list[dict[str, Any]] = field(default_factory=list)
    # Derived top set
    top_weight_kg: float | None = None
    top_reps: int | None = None
    total_sets: int = 0


@dataclass
class CompletedWorkout:
    """A completed (logged) workout from Hevy."""

    id: str
    title: str
    start_time: str | None = None
    end_time: str | None = None
    duration_seconds: int | None = None
    exercises: list[WorkoutExercise] = field(default_factory=list)
    total_volume_kg: float = 0.0


@dataclass
class HevyTrainingData:
    """Everything we know about a user's training from their Hevy account.

    This is the read-first data structure that replaces the static ``program.py``
    for determining what a user trains, how often, and at what intensity.
    """

    # Account info
    username: str | None = None
    workout_count: int | None = None

    # Exercise library (template_id -> metadata)
    exercise_templates: dict[str, ExerciseTemplate] = field(default_factory=dict)

    # Routine templates (what the user plans to do)
    routines: list[Routine] = field(default_factory=list)

    # Recently completed workouts (what they actually did)
    recent_workouts: list[CompletedWorkout] = field(default_factory=list)

    # Routine folders
    folders: dict[int, str] = field(default_factory=dict)  # folder_id -> title

    def routine_titles(self) -> list[str]:
        """Return all routine titles (the user's training days)."""
        return [r.title for r in self.routines]

    def exercises_for_routine(self, title: str) -> list[RoutineExercise]:
        """Return exercises for a named routine."""
        for r in self.routines:
            if r.title == title:
                return r.exercises
        return []

    def muscle_group_for(self, template_id: str) -> str:
        """Return the primary muscle group for an exercise template."""
        tmpl = self.exercise_templates.get(template_id)
        return tmpl.primary_muscle_group if tmpl else "other"

    def exercise_name(self, template_id: str) -> str:
        """Human-readable name for a template ID."""
        tmpl = self.exercise_templates.get(template_id)
        return tmpl.title if tmpl else template_id

    def latest_workout(self) -> CompletedWorkout | None:
        """Return the most recently completed workout."""
        return self.recent_workouts[0] if self.recent_workouts else None


@dataclass
class HevySourceSnapshot:
    """One classified read of the authenticated user's Hevy account.

    ``status`` is ``ok`` when every resource was read, ``partial`` when at
    least one resource failed but the account is still usable, and the fetch
    raises :class:`hevy_client.HevyError` subclasses when the credential or
    the provider itself makes the read impossible.
    """

    data: HevyTrainingData
    status: str = "ok"
    fetched_at: str = ""
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def is_partial(self) -> bool:
        """True when some source resources could not be read."""
        return self.status == "partial"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_exercise_template(raw: dict[str, Any]) -> ExerciseTemplate:
    return ExerciseTemplate(
        id=str(raw.get("id", "")),
        title=raw.get("title", "Unknown"),
        exercise_type=raw.get("type", "weight_reps"),
        primary_muscle_group=raw.get("primary_muscle_group", "other"),
        secondary_muscle_groups=raw.get("secondary_muscle_groups") or [],
        equipment=raw.get("equipment"),
        is_custom=raw.get("is_custom", False),
    )


def _parse_routine(
    raw: dict[str, Any],
    templates: dict[str, ExerciseTemplate],
) -> Routine:
    exercises: list[RoutineExercise] = []
    for ex_raw in raw.get("exercises", []):
        tid = str(ex_raw.get("exercise_template_id", ""))
        tmpl = templates.get(tid)
        title = tmpl.title if tmpl else tid

        sets_raw = ex_raw.get("sets", [])
        num_sets = len(sets_raw)

        # Extract planned targets from the first set (routines usually
        # have uniform targets across sets).
        target_weight = None
        target_reps = None
        target_range = None
        if sets_raw:
            first_set = sets_raw[0] if isinstance(sets_raw[0], dict) else {}
            target_weight = first_set.get("weight_kg")
            target_reps = first_set.get("reps")
            rep_range = first_set.get("rep_range")
            if isinstance(rep_range, dict):
                target_range = (
                    rep_range.get("start", 0),
                    rep_range.get("end", 0),
                )

        exercises.append(
            RoutineExercise(
                template_id=tid,
                title=title,
                sets=num_sets,
                rest_seconds=ex_raw.get("rest_seconds", 90),
                notes=ex_raw.get("notes"),
                superset_id=ex_raw.get("superset_id"),
                target_weight_kg=target_weight,
                target_reps=target_reps,
                target_rep_range=target_range,
                set_targets=[
                    dict(item) if isinstance(item, dict) else {"value": item}
                    for item in sets_raw
                ],
            ),
        )

    return Routine(
        id=str(raw.get("id", "")),
        title=raw.get("title", "Untitled"),
        folder_id=raw.get("folder_id"),
        exercises=exercises,
        updated_at=raw.get("updated_at"),
    )


def _top_set_from_sets(
    sets_raw: list[dict[str, Any]],
) -> tuple[float | None, int | None]:
    """Return (top_weight_kg, top_reps) from a list of set dicts."""
    best_weight: float | None = None
    best_reps: int | None = None
    best_key = (-1.0, -1)

    for s in sets_raw:
        if not isinstance(s, dict):
            continue
        w = s.get("weight_kg")
        r = s.get("reps")
        key = (float(w) if w is not None else -1.0, int(r) if r is not None else -1)
        if key > best_key:
            best_key = key
            best_weight = float(w) if w is not None else None
            best_reps = int(r) if r is not None else None

    return best_weight, best_reps


def _parse_workout(
    raw: dict[str, Any],
    templates: dict[str, ExerciseTemplate],
) -> CompletedWorkout:
    exercises: list[WorkoutExercise] = []
    total_volume = 0.0

    for ex_raw in raw.get("exercises", []):
        tid = str(ex_raw.get("exercise_template_id", ""))
        tmpl = templates.get(tid)
        title = ex_raw.get("title") or (tmpl.title if tmpl else tid)

        sets_raw = ex_raw.get("sets", [])
        top_w, top_r = _top_set_from_sets(sets_raw)

        # Calculate volume from all sets.
        for s in sets_raw:
            if isinstance(s, dict):
                w = s.get("weight_kg") or 0
                r = s.get("reps") or 0
                total_volume += w * r

        exercises.append(
            WorkoutExercise(
                template_id=tid,
                title=title,
                sets=sets_raw,
                top_weight_kg=top_w,
                top_reps=top_r,
                total_sets=len(sets_raw),
            ),
        )

    # Duration.
    duration = None
    start = raw.get("start_time")
    end = raw.get("end_time")
    if start and end:
        try:
            from datetime import datetime

            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            duration = int((e - s).total_seconds())
        except (ValueError, TypeError):
            pass

    return CompletedWorkout(
        id=str(raw.get("id", "")),
        title=raw.get("title", "Workout"),
        start_time=start,
        end_time=end,
        duration_seconds=duration,
        exercises=exercises,
        total_volume_kg=round(total_volume, 1),
    )


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------


def _build_training_data(
    raw_templates: list[dict[str, Any]] | None,
    raw_routines: list[dict[str, Any]] | None,
    raw_workouts: list[dict[str, Any]] | None,
    raw_folders: list[dict[str, Any]] | None,
    user_info: dict[str, Any] | None,
    count: int | None,
) -> HevyTrainingData:
    """Normalise raw Hevy payloads into the read-first training structure."""

    data = HevyTrainingData()

    for raw in raw_templates or []:
        tmpl = _parse_exercise_template(raw)
        data.exercise_templates[tmpl.id] = tmpl

    data.routines = [
        _parse_routine(r, data.exercise_templates) for r in raw_routines or []
    ]

    data.recent_workouts = [
        _parse_workout(w, data.exercise_templates) for w in raw_workouts or []
    ]

    data.folders = {
        int(f["id"]): f.get("title", "") for f in raw_folders or [] if "id" in f
    }

    if user_info:
        data.username = user_info.get("username")
    if count is not None:
        data.workout_count = count

    return data


def fetch_user_training(api_key: str, *, workout_limit: int = 15) -> HevyTrainingData:
    """Pull everything we need from Hevy to understand what this user trains.

    This is the single entry point for the read-first flow. It fetches:
    1. Exercise templates (the exercise library with muscle groups)
    2. Routine templates (what the user plans to do)
    3. Recent completed workouts (what they actually did)
    4. Routine folders (organisational structure)
    5. Account info (username, workout count)

    All calls are best-effort: a failure in one does not block the others.
    """

    data = _build_training_data(
        get_exercise_templates(api_key),
        get_routines(api_key),
        get_recent_workouts(api_key, limit=workout_limit),
        get_routine_folders(api_key),
        get_user_info(api_key),
        get_workout_count(api_key),
    )

    if not data.exercise_templates:
        logger.warning("Could not fetch exercise templates from Hevy.")
    if not data.routines:
        logger.warning("Could not fetch routines from Hevy.")

    return data


def fetch_user_training_snapshot(
    api_key: str,
    *,
    workout_limit: int = 56,
) -> HevySourceSnapshot:
    """Read the user's Hevy account and classify the result.

    Resource level failures degrade the snapshot to ``partial`` so the builder
    can show exactly what is missing. Credential and rate-limit failures are
    raised because they affect the whole account.
    """

    data = HevyTrainingData()
    warnings: list[str] = []

    def _partial(resource: str, exc: Exception) -> None:
        warnings.append(f"{resource} could not be read from Hevy: {exc}")
        logger.warning("%s could not be read from Hevy: %s", resource, exc)

    # 1. Exercise templates (needed to resolve names in routines/workouts).
    try:
        raw_templates = get_exercise_templates_strict(api_key)
    except (HevyAuthError, HevyRateLimitError):
        raise
    except HevyError as exc:
        _partial("Exercise templates", exc)
        raw_templates = []

    # 2. Routines.
    try:
        raw_routines = get_routines_strict(api_key)
    except (HevyAuthError, HevyRateLimitError):
        raise
    except HevyError as exc:
        _partial("Routines", exc)
        raw_routines = []

    # 3. Recent workouts.
    try:
        raw_workouts = get_recent_workouts_strict(api_key, limit=workout_limit)
    except (HevyAuthError, HevyRateLimitError):
        raise
    except HevyError as exc:
        _partial("Workout history", exc)
        raw_workouts = []

    # 4. Folders.
    try:
        raw_folders = get_routine_folders_strict(api_key)
    except (HevyAuthError, HevyRateLimitError):
        raise
    except HevyError as exc:
        _partial("Routine folders", exc)
        raw_folders = []

    # 5. Account info.
    try:
        user_info = get_user_info_strict(api_key)
    except (HevyAuthError, HevyRateLimitError):
        raise
    except HevyError as exc:
        _partial("Account information", exc)
        user_info = {}

    try:
        count = get_workout_count_strict(api_key)
    except (HevyAuthError, HevyRateLimitError):
        raise
    except HevyError as exc:
        _partial("Workout count", exc)
        count = None

    data = _build_training_data(
        raw_templates,
        raw_routines,
        raw_workouts,
        raw_folders,
        user_info,
        count,
    )

    logger.info(
        "Read %d routines, %d folders and %d recent workouts from Hevy (%s).",
        len(data.routines),
        len(data.folders),
        len(data.recent_workouts),
        "partial" if warnings else "complete",
    )

    return HevySourceSnapshot(
        data=data,
        status="partial" if warnings else "ok",
        fetched_at=datetime.now(tz=timezone.utc).isoformat(),
        warnings=warnings,
        counts={
            "routines": len(data.routines),
            "folders": len(data.folders),
            "exercise_templates": len(data.exercise_templates),
            "recent_workouts": len(data.recent_workouts),
            "workouts": int(data.workout_count or 0),
        },
    )
