"""Deterministic Hevy routine matching and exercise-baseline inference.

The public Hevy workout payload does not carry the source routine identifier.
This module reconstructs the most likely routine for each completed workout
from canonical routine snapshots and derives conservative exercise baselines
from the supporting sets.  Everything here is deterministic, versioned and free
of generative-AI decisions so programme previews and future adaptations can be
tested and explained.

The module is intentionally independent of the SQLite layer.  Persistence
helpers live in :mod:`database`; callers may pass stored overrides back into
:func:`analyse_history` so a user correction wins without mutating raw provider
history.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from typing import Any

from hevy_parser import normalise_name
from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    WorkoutExercise,
)

MATCHER_VERSION = "hevy-history-matcher-v1"
BASELINE_VERSION = "hevy-baseline-v1"
HISTORY_RULE_VERSION = "hevy-history-v1"
SNAPSHOT_SCHEMA_VERSION = 1

CONFIDENCE_HIGH = "high"
CONFIDENCE_PROBABLE = "probable"
CONFIDENCE_AMBIGUOUS = "ambiguous"
CONFIDENCE_UNMATCHED = "unmatched"
CONFIDENCE_BANDS = (
    CONFIDENCE_HIGH,
    CONFIDENCE_PROBABLE,
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_UNMATCHED,
)
_CONFIDENT_BANDS = {CONFIDENCE_HIGH, CONFIDENCE_PROBABLE}

HIGH_SCORE = 0.78
PROBABLE_SCORE = 0.52
AMBIGUOUS_SCORE = 0.35
AMBIGUITY_MARGIN = 0.06

MIN_EVIDENCE_EXPOSURES = 3
PREFERRED_EXPOSURE_WINDOW_DAYS = 42
DEFAULT_PROGRAMME_WEEKS = 12

_DEFAULT_WEIGHTS: dict[str, float] = {
    "exercise_overlap": 0.34,
    "sequence": 0.17,
    "title": 0.11,
    "set_structure": 0.13,
    "superset": 0.06,
    "cycle_position": 0.10,
    "recency": 0.09,
}

_WORKING_SET_TYPES = {"normal", "failure", "dropset"}
_SET_TYPE_ALIASES = {
    "warm_up": "warmup",
    "warm-up": "warmup",
    "warm up": "warmup",
    "drop": "dropset",
    "drop_set": "dropset",
    "drop set": "dropset",
    "amrap": "failure",
}
_COMPOUND_MUSCLES = {
    "chest",
    "back",
    "lats",
    "quads",
    "hamstrings",
    "glutes",
    "shoulders",
    "upper_back",
    "lower_back",
}
_DURATION_TYPES = {"duration", "distance", "distance_duration", "weight_duration"}
_BODYWEIGHT_TYPES = {"bodyweight_reps", "reps_only", "assisted_bodyweight"}
_ASSISTED_TYPES = {"assisted_bodyweight", "assisted"}


class MatchWeights(dict[str, float]):
    """Versioned score weights with a stable, explicit shape."""

    def __init__(self, values: Mapping[str, float] | None = None) -> None:
        super().__init__(_DEFAULT_WEIGHTS)
        if values:
            for key, value in values.items():
                if key not in self:
                    raise ValueError(f"Unknown match feature weight: {key}")
                self[key] = float(value)
        if not math.isclose(sum(self.values()), 1.0, abs_tol=1e-6):
            raise ValueError("Match weights must sum to 1.0")


DEFAULT_MATCH_WEIGHTS = MatchWeights()


# ---------------------------------------------------------------------------
# Typed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotExercise:
    """One ordered exercise in an immutable routine snapshot."""

    template_id: str
    title: str
    sets: int
    position: int
    superset_id: int | None = None
    exercise_type: str | None = None
    primary_muscle_group: str | None = None
    equipment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "title": self.title,
            "sets": self.sets,
            "position": self.position,
            "superset_id": self.superset_id,
            "exercise_type": self.exercise_type,
            "primary_muscle_group": self.primary_muscle_group,
            "equipment": self.equipment,
        }


@dataclass(frozen=True)
class RoutineSnapshot:
    """Canonical, immutable local copy of one routine revision."""

    id: str
    routine_id: str
    title: str
    content_hash: str
    exercises: tuple[SnapshotExercise, ...]
    updated_at: str | None = None
    fetched_at: str | None = None
    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    superseded_snapshot_id: str | None = None

    @property
    def template_ids(self) -> tuple[str, ...]:
        return tuple(exercise.template_id for exercise in self.exercises)

    @property
    def template_set_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for exercise in self.exercises:
            counts[exercise.template_id] += max(1, exercise.sets)
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "routine_id": self.routine_id,
            "title": self.title,
            "content_hash": self.content_hash,
            "exercises": [exercise.to_dict() for exercise in self.exercises],
            "updated_at": self.updated_at,
            "fetched_at": self.fetched_at,
            "schema_version": self.schema_version,
            "superseded_snapshot_id": self.superseded_snapshot_id,
        }


@dataclass(frozen=True)
class MatchOverride:
    """A user correction applied to matching for a stable workout pattern."""

    pattern_type: str
    pattern_value: str
    routine_snapshot_id: str
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_type": self.pattern_type,
            "pattern_value": self.pattern_value,
            "routine_snapshot_id": self.routine_snapshot_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RoutineMatch:
    """Explained workout-to-routine match result."""

    workout_id: str
    workout_title: str
    routine_snapshot_id: str | None
    routine_id: str | None
    score: float
    confidence: str
    features: dict[str, float]
    method: str = "feature_similarity"
    matcher_version: str = MATCHER_VERSION
    is_ambiguous: bool = False
    ambiguity_margin: float = 0.0
    user_confirmed: bool = False
    candidate_snapshot_id: str | None = None
    candidate_routine_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workout_id": self.workout_id,
            "workout_title": self.workout_title,
            "routine_snapshot_id": self.routine_snapshot_id,
            "routine_id": self.routine_id,
            "candidate_snapshot_id": self.candidate_snapshot_id,
            "candidate_routine_id": self.candidate_routine_id,
            "score": round(self.score, 4),
            "confidence": self.confidence,
            "features": {key: round(value, 4) for key, value in self.features.items()},
            "method": self.method,
            "matcher_version": self.matcher_version,
            "is_ambiguous": self.is_ambiguous,
            "ambiguity_margin": round(self.ambiguity_margin, 4),
            "user_confirmed": self.user_confirmed,
        }


@dataclass(frozen=True)
class EvidenceWindow:
    """The observed period supporting one history analysis."""

    start: str | None
    end: str | None
    preferred_days: int
    workout_count: int
    expanded: bool = False
    reason: str = "preferred_window"

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "preferred_days": self.preferred_days,
            "workout_count": self.workout_count,
            "expanded": self.expanded,
            "reason": self.reason,
        }


@dataclass
class _SetObservation:
    workout_id: str
    date: datetime | None
    kind: str
    weight_kg: float | None
    reps: float | None
    duration_seconds: float | None
    distance_meters: float | None
    rpe: float | None
    custom_metric: float | None


@dataclass
class ExerciseBaseline:
    """Conservative, explainable baseline for one exercise template."""

    template_id: str
    title: str
    exercise_type: str
    confidence: str
    evidence_window: dict[str, Any]
    working_set_count: int
    warmup_set_count: int
    failure_set_count: int
    dropset_set_count: int
    exposure_count: int
    supporting_workouts: list[dict[str, Any]]
    load_kg: dict[str, float | None]
    reps: dict[str, float | None]
    duration_seconds: dict[str, float | None]
    distance_meters: dict[str, float | None]
    custom_metric: dict[str, float | None]
    e1rm_kg: dict[str, Any]
    rpe: dict[str, float | None]
    trends: dict[str, float | None]
    variability: dict[str, float | None]
    exposure_frequency: dict[str, float | None]
    equipment_increment_kg: float | None
    role_features: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    rule_version: str = BASELINE_VERSION
    pathological_set_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "title": self.title,
            "exercise_type": self.exercise_type,
            "confidence": self.confidence,
            "evidence_window": self.evidence_window,
            "working_set_count": self.working_set_count,
            "warmup_set_count": self.warmup_set_count,
            "failure_set_count": self.failure_set_count,
            "dropset_set_count": self.dropset_set_count,
            "exposure_count": self.exposure_count,
            "supporting_workouts": self.supporting_workouts,
            "load_kg": self.load_kg,
            "reps": self.reps,
            "duration_seconds": self.duration_seconds,
            "distance_meters": self.distance_meters,
            "custom_metric": self.custom_metric,
            "e1rm_kg": self.e1rm_kg,
            "rpe": self.rpe,
            "trends": self.trends,
            "variability": self.variability,
            "exposure_frequency": self.exposure_frequency,
            "equipment_increment_kg": self.equipment_increment_kg,
            "role_features": self.role_features,
            "warnings": list(self.warnings),
            "rule_version": self.rule_version,
            "pathological_set_fields": list(self.pathological_set_fields),
        }


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            parsed = datetime.fromisoformat(text)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_workout_datetime(workouts: Iterable[CompletedWorkout]) -> datetime | None:
    dates = [
        parsed
        for parsed in (_parse_datetime(workout.start_time) for workout in workouts)
        if parsed is not None
    ]
    return max(dates) if dates else None


def _template_attr(
    templates: Mapping[str, ExerciseTemplate] | None,
    template_id: str,
) -> ExerciseTemplate | None:
    if not templates:
        return None
    return templates.get(template_id)


def _workout_start(workout: CompletedWorkout) -> datetime | None:
    return _parse_datetime(workout.start_time)


def _number(raw: Mapping[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _set_kind(raw: Mapping[str, Any]) -> str:
    raw_type = raw.get("type") or raw.get("set_type") or "normal"
    value = str(raw_type).strip().lower()
    value = _SET_TYPE_ALIASES.get(value, value)
    if "warm" in value:
        return "warmup"
    if "drop" in value:
        return "dropset"
    if "fail" in value or "amrap" in value:
        return "failure"
    return "normal"


def _exercise_type(template: ExerciseTemplate | None, exercise: WorkoutExercise) -> str:
    if template is not None and template.exercise_type:
        return str(template.exercise_type)
    for raw in exercise.sets:
        if not isinstance(raw, dict):
            continue
        for key in ("duration_seconds", "distance_meters", "weight_kg", "reps"):
            if raw.get(key) is not None:
                if key == "duration_seconds":
                    return "duration"
                if key == "distance_meters":
                    return "distance"
                return "weight_reps"
    return "unknown"


def _set_observations(
    workout: CompletedWorkout,
    exercise: WorkoutExercise,
) -> list[_SetObservation]:
    observations: list[_SetObservation] = []
    workout_date = _workout_start(workout)
    for raw in exercise.sets:
        if not isinstance(raw, dict):
            continue
        observations.append(
            _SetObservation(
                workout_id=workout.id,
                date=workout_date,
                kind=_set_kind(raw),
                weight_kg=_number(raw, "weight_kg"),
                reps=_number(raw, "reps"),
                duration_seconds=_number(raw, "duration_seconds"),
                distance_meters=_number(raw, "distance_meters"),
                rpe=_number(raw, "rpe"),
                custom_metric=_number(raw, "custom_metric"),
            )
        )
    return observations


def _epley_1rm(weight_kg: float, reps: float) -> float | None:
    if weight_kg <= 0 or reps < 1 or reps > 15:
        return None
    return weight_kg * (1.0 + reps / 30.0)


def _median(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return statistics.median(clean) if clean else None


def _iqr(values: Sequence[float]) -> float | None:
    clean = sorted(float(value) for value in values if value is not None)
    if len(clean) < 2:
        return None
    lower = clean[len(clean) // 4]
    upper = clean[(3 * len(clean)) // 4]
    return upper - lower


def _coefficient_of_variation(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if len(clean) < 2:
        return None
    mean = statistics.fmean(clean)
    if mean == 0:
        return None
    return statistics.pstdev(clean) / abs(mean)


def _mad(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    centre = statistics.median(clean)
    return statistics.median(abs(value - centre) for value in clean)


def _linear_trend(points: Sequence[tuple[float, float]]) -> float | None:
    if len(points) < 3:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return numerator / denominator


def _infer_equipment_increment(loads: Sequence[float]) -> float | None:
    unique = sorted({round(float(load), 3) for load in loads if load and load > 0})
    if len(unique) < 2:
        return None
    diffs = [
        round(current - previous, 3)
        for previous, current in pairwise(unique)
        if current - previous > 1e-9
    ]
    if not diffs:
        return None
    counts = Counter(diffs)
    common = max(counts.items(), key=lambda item: (item[1], -item[0]))[0]
    if not 0.25 <= common <= 25:
        return None
    return common


def _weighted_jaccard(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    keys = set(left) | set(right)
    intersection = sum(min(left.get(key, 0), right.get(key, 0)) for key in keys)
    union = sum(max(left.get(key, 0), right.get(key, 0)) for key in keys)
    return intersection / union if union else 0.0


def _lcs_length(left: Sequence[str], right: Sequence[str]) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for left_value in left:
        current = [0]
        for index, right_value in enumerate(right, start=1):
            if left_value == right_value:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def _sequence_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    longest = max(len(left), len(right))
    return _lcs_length(left, right) / longest if longest else 0.0


def _title_similarity(left: str, right: str) -> float:
    normalised_left = normalise_name(left)
    normalised_right = normalise_name(right)
    if not normalised_left or not normalised_right:
        return 0.0
    if normalised_left == normalised_right:
        return 1.0
    left_tokens = set(re.findall(r"[a-z0-9]+", normalised_left))
    right_tokens = set(re.findall(r"[a-z0-9]+", normalised_right))
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    overlap = left_tokens & right_tokens
    if not overlap:
        return 0.0
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return 0.75
    return len(overlap) / len(union)


def _work_set_count(exercise: WorkoutExercise) -> int:
    if exercise.sets:
        return sum(
            1
            for raw in exercise.sets
            if isinstance(raw, dict) and _set_kind(raw) in _WORKING_SET_TYPES
        )
    return max(0, int(exercise.total_sets))


def _workout_set_counts(workout: CompletedWorkout) -> Counter[str]:
    counts: Counter[str] = Counter()
    for exercise in workout.exercises:
        counts[exercise.template_id] += max(1, _work_set_count(exercise))
    return counts


def _set_structure_similarity(snapshot: RoutineSnapshot, workout: CompletedWorkout) -> float:
    snapshot_counts = snapshot.template_set_counts
    workout_counts = _workout_set_counts(workout)
    if not snapshot_counts or not workout_counts:
        return 0.0
    snapshot_total = sum(snapshot_counts.values())
    workout_total = sum(workout_counts.values())
    total_similarity = 1.0 - abs(snapshot_total - workout_total) / max(
        snapshot_total, workout_total, 1
    )
    common = set(snapshot_counts) & set(workout_counts)
    if common:
        dot = sum(snapshot_counts[key] * workout_counts[key] for key in common)
        snapshot_norm = math.sqrt(
            sum(snapshot_counts[key] ** 2 for key in common)
        )
        workout_norm = math.sqrt(sum(workout_counts[key] ** 2 for key in common))
        cosine = dot / (snapshot_norm * workout_norm) if snapshot_norm and workout_norm else 0.0
    else:
        cosine = 0.0
    return 0.5 * total_similarity + 0.5 * cosine


def _superset_signature(snapshot: RoutineSnapshot) -> set[tuple[str, ...]]:
    groups: dict[int, list[str]] = {}
    for exercise in snapshot.exercises:
        if exercise.superset_id is None:
            continue
        groups.setdefault(int(exercise.superset_id), []).append(exercise.template_id)
    return {tuple(sorted(group)) for group in groups.values()}


def _workout_superset_signature(workout: CompletedWorkout) -> tuple[set[tuple[str, ...]], bool]:
    groups: dict[int, list[str]] = {}
    saw_metadata = False
    for exercise in workout.exercises:
        superset_id: Any = getattr(exercise, "superset_id", None)
        if superset_id is None:
            for raw in exercise.sets:
                if isinstance(raw, dict) and raw.get("superset_id") is not None:
                    superset_id = raw.get("superset_id")
                    break
        if superset_id is None:
            continue
        saw_metadata = True
        groups.setdefault(int(superset_id), []).append(exercise.template_id)
    return {tuple(sorted(group)) for group in groups.values()}, saw_metadata


def _superset_similarity(snapshot: RoutineSnapshot, workout: CompletedWorkout) -> float:
    snapshot_groups = _superset_signature(snapshot)
    workout_groups, saw_metadata = _workout_superset_signature(workout)
    if not snapshot_groups and not workout_groups:
        return 1.0
    if snapshot_groups and not saw_metadata:
        return 0.6
    union = snapshot_groups | workout_groups
    if not union:
        return 1.0
    return len(snapshot_groups & workout_groups) / len(union)


def _recency_similarity(snapshot: RoutineSnapshot, workout: CompletedWorkout) -> float:
    snapshot_time = _parse_datetime(snapshot.updated_at)
    workout_time = _workout_start(workout)
    if snapshot_time is None or workout_time is None:
        return 0.5
    if workout_time >= snapshot_time:
        return 1.0
    days_before = (snapshot_time - workout_time).days
    return max(0.2, 1.0 - min(1.0, days_before / 90.0))


def _cycle_similarity(index: int, last_index: int | None, count: int) -> float:
    if last_index is None or count <= 1:
        return 0.5
    expected = (last_index + 1) % count
    if index == expected:
        return 1.0
    if index == last_index:
        return 0.25
    return 0.0


def _feature_scores(
    snapshot: RoutineSnapshot,
    snapshot_index: int,
    last_confident_index: int | None,
    snapshot_count: int,
    workout: CompletedWorkout,
) -> dict[str, float]:
    workout_counts = _workout_set_counts(workout)
    return {
        "exercise_overlap": _weighted_jaccard(
            snapshot.template_set_counts,
            workout_counts,
        ),
        "sequence": _sequence_similarity(
            snapshot.template_ids,
            tuple(exercise.template_id for exercise in workout.exercises),
        ),
        "title": _title_similarity(snapshot.title, workout.title),
        "set_structure": _set_structure_similarity(snapshot, workout),
        "superset": _superset_similarity(snapshot, workout),
        "cycle_position": _cycle_similarity(
            snapshot_index,
            last_confident_index,
            snapshot_count,
        ),
        "recency": _recency_similarity(snapshot, workout),
    }


def _score(features: Mapping[str, float], weights: Mapping[str, float]) -> float:
    return sum(float(features.get(key, 0.0)) * float(weight) for key, weight in weights.items())


def _confidence_for_score(score: float) -> str:
    if score >= HIGH_SCORE:
        return CONFIDENCE_HIGH
    if score >= PROBABLE_SCORE:
        return CONFIDENCE_PROBABLE
    if score >= AMBIGUOUS_SCORE:
        return CONFIDENCE_AMBIGUOUS
    return CONFIDENCE_UNMATCHED


def _workout_signature(workout: CompletedWorkout) -> str:
    return "|".join(sorted(exercise.template_id for exercise in workout.exercises))


def _override_index(
    overrides: Iterable[MatchOverride | Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for raw in overrides:
        override = raw if isinstance(raw, MatchOverride) else MatchOverride(
            pattern_type=str(raw.get("pattern_type", "")),
            pattern_value=str(raw.get("pattern_value", "")),
            routine_snapshot_id=str(raw.get("routine_snapshot_id", "")),
            created_at=raw.get("created_at"),
        )
        pattern_type = override.pattern_type.strip().lower()
        pattern_value = override.pattern_value.strip()
        if not pattern_type or not pattern_value or not override.routine_snapshot_id:
            continue
        if pattern_type == "title":
            pattern_value = normalise_name(pattern_value)
        elif pattern_type == "exercise_signature":
            pattern_value = "|".join(
                sorted(part.strip() for part in pattern_value.split("|") if part.strip())
            )
        index.setdefault(pattern_type, {})[pattern_value] = override.routine_snapshot_id
    return index


def _override_for_workout(
    index: Mapping[str, Mapping[str, str]],
    workout: CompletedWorkout,
) -> str | None:
    for pattern_type, value in (
        ("workout", workout.id),
        ("title", normalise_name(workout.title)),
        ("exercise_signature", _workout_signature(workout)),
    ):
        target = index.get(pattern_type, {}).get(value)
        if target:
            return target
    return None


# ---------------------------------------------------------------------------
# Snapshot and match construction
# ---------------------------------------------------------------------------


def build_snapshot(
    routine: Routine,
    templates: Mapping[str, ExerciseTemplate] | None = None,
    *,
    user_id: str | None = None,
    fetched_at: str | None = None,
    superseded_snapshot_id: str | None = None,
) -> RoutineSnapshot:
    """Build one deterministic snapshot for a Hevy routine revision."""

    exercises: list[SnapshotExercise] = []
    for position, exercise in enumerate(routine.exercises):
        template = _template_attr(templates, exercise.template_id)
        exercises.append(
            SnapshotExercise(
                template_id=exercise.template_id,
                title=exercise.title,
                sets=max(0, int(exercise.sets)),
                position=position,
                superset_id=exercise.superset_id,
                exercise_type=template.exercise_type if template else None,
                primary_muscle_group=(
                    template.primary_muscle_group if template else None
                ),
                equipment=template.equipment if template else None,
            )
        )
    content = {
        "routine_id": routine.id,
        "title": routine.title,
        "folder_id": routine.folder_id,
        "updated_at": routine.updated_at,
        "exercises": [
            {
                "template_id": exercise.template_id,
                "title": exercise.title,
                "sets": exercise.sets,
                "superset_id": exercise.superset_id,
                "position": exercise.position,
            }
            for exercise in exercises
        ],
    }
    content_hash = _hash(content)
    snapshot_id = "snap_" + _hash(
        {
            "user_id": user_id or "",
            "routine_id": routine.id,
            "content_hash": content_hash,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
        }
    )[:32]
    return RoutineSnapshot(
        id=snapshot_id,
        routine_id=routine.id,
        title=routine.title,
        content_hash=content_hash,
        exercises=tuple(exercises),
        updated_at=routine.updated_at,
        fetched_at=fetched_at,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        superseded_snapshot_id=superseded_snapshot_id,
    )


def build_snapshots(
    routines: Iterable[Routine],
    templates: Mapping[str, ExerciseTemplate] | None = None,
    *,
    user_id: str | None = None,
    fetched_at: str | None = None,
) -> list[RoutineSnapshot]:
    """Build snapshots in the caller's selected routine order."""

    return [
        build_snapshot(
            routine,
            templates,
            user_id=user_id,
            fetched_at=fetched_at,
        )
        for routine in routines
    ]


def match_workouts(
    snapshots: Sequence[RoutineSnapshot],
    workouts: Sequence[CompletedWorkout],
    *,
    overrides: Iterable[MatchOverride | Mapping[str, Any]] = (),
    weights: Mapping[str, float] | None = None,
) -> list[RoutineMatch]:
    """Match completed workouts to routine snapshots deterministically.

    Workouts are processed from oldest to newest so the rolling-cycle feature
    is stable.  A workout is never assigned to a routine from its title alone:
    title similarity is deliberately a small component of the score.
    """

    active_weights = MatchWeights(weights)
    snapshot_list = list(snapshots)
    snapshot_by_id = {snapshot.id: snapshot for snapshot in snapshot_list}
    override_index = _override_index(overrides)
    ordered_workouts = sorted(
        workouts,
        key=lambda workout: (
            _workout_start(workout) or datetime.min.replace(tzinfo=timezone.utc),
            workout.id,
        ),
    )

    matches: list[RoutineMatch] = []
    last_confident_index: int | None = None

    for workout in ordered_workouts:
        features_by_snapshot: list[tuple[RoutineSnapshot, int, dict[str, float], float]] = []
        for index, snapshot in enumerate(snapshot_list):
            features = _feature_scores(
                snapshot,
                index,
                last_confident_index,
                len(snapshot_list),
                workout,
            )
            features_by_snapshot.append(
                (snapshot, index, features, _score(features, active_weights))
            )

        override_target = _override_for_workout(override_index, workout)
        if override_target and override_target in snapshot_by_id:
            snapshot = snapshot_by_id[override_target]
            index = snapshot_list.index(snapshot)
            features = _feature_scores(
                snapshot,
                index,
                last_confident_index,
                len(snapshot_list),
                workout,
            )
            matches.append(
                RoutineMatch(
                    workout_id=workout.id,
                    workout_title=workout.title,
                    routine_snapshot_id=snapshot.id,
                    routine_id=snapshot.routine_id,
                    candidate_snapshot_id=snapshot.id,
                    candidate_routine_id=snapshot.routine_id,
                    score=1.0,
                    confidence=CONFIDENCE_HIGH,
                    features=features,
                    method="user_confirmed_override",
                    is_ambiguous=False,
                    user_confirmed=True,
                )
            )
            last_confident_index = index
            continue

        if not features_by_snapshot:
            matches.append(
                RoutineMatch(
                    workout_id=workout.id,
                    workout_title=workout.title,
                    routine_snapshot_id=None,
                    routine_id=None,
                    score=0.0,
                    confidence=CONFIDENCE_UNMATCHED,
                    features={key: 0.0 for key in active_weights},
                )
            )
            continue

        ranked = sorted(
            features_by_snapshot,
            key=lambda item: (-item[3], item[0].id),
        )
        best_snapshot, best_index, best_features, best_score = ranked[0]
        second_score = ranked[1][3] if len(ranked) > 1 else 0.0
        margin = best_score - second_score
        confidence = _confidence_for_score(best_score)
        is_ambiguous = False
        if (
            confidence != CONFIDENCE_UNMATCHED
            and len(ranked) > 1
            and margin < AMBIGUITY_MARGIN
            and second_score >= PROBABLE_SCORE
        ):
            confidence = CONFIDENCE_AMBIGUOUS
            is_ambiguous = True

        if confidence == CONFIDENCE_UNMATCHED:
            matches.append(
                RoutineMatch(
                    workout_id=workout.id,
                    workout_title=workout.title,
                    routine_snapshot_id=None,
                    routine_id=None,
                    candidate_snapshot_id=best_snapshot.id,
                    candidate_routine_id=best_snapshot.routine_id,
                    score=best_score,
                    confidence=CONFIDENCE_UNMATCHED,
                    features=best_features,
                    is_ambiguous=False,
                    ambiguity_margin=margin,
                )
            )
            continue

        matches.append(
            RoutineMatch(
                workout_id=workout.id,
                workout_title=workout.title,
                routine_snapshot_id=best_snapshot.id,
                routine_id=best_snapshot.routine_id,
                candidate_snapshot_id=best_snapshot.id,
                candidate_routine_id=best_snapshot.routine_id,
                score=best_score,
                confidence=confidence,
                features=best_features,
                is_ambiguous=is_ambiguous,
                ambiguity_margin=margin,
            )
        )
        if confidence in _CONFIDENT_BANDS:
            last_confident_index = best_index

    return matches


# ---------------------------------------------------------------------------
# Evidence window and baselines
# ---------------------------------------------------------------------------


def select_evidence_window(
    workouts: Sequence[CompletedWorkout],
    *,
    programme_weeks: int | None = None,
    now: datetime | None = None,
) -> EvidenceWindow:
    """Choose a bounded, deterministic evidence window.

    The window scales with programme duration (two programme cycles), with a
    minimum useful look-back and a cap.  Sparse data expands the window instead
    of pretending a short sample is authoritative.
    """

    weeks = programme_weeks or DEFAULT_PROGRAMME_WEEKS
    preferred_days = max(84, min(365, int(weeks) * 14))
    reference = now or _latest_workout_datetime(workouts)
    if reference is None:
        return EvidenceWindow(
            start=None,
            end=None,
            preferred_days=preferred_days,
            workout_count=0,
            reason="no_workouts",
        )
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    start = reference - timedelta(days=preferred_days)
    selected = [
        workout
        for workout in workouts
        if (start_time := _workout_start(workout)) is not None
        and start <= start_time <= reference
    ]
    expanded = False
    reason = "preferred_window"
    if len(selected) < MIN_EVIDENCE_EXPOSURES and len(workouts) > len(selected):
        selected = [
            workout
            for workout in workouts
            if (start_time := _workout_start(workout)) is None
            or start_time <= reference
        ]
        expanded = True
        reason = "expanded_for_sparse_history"
    dates = [
        parsed
        for parsed in (_workout_start(workout) for workout in selected)
        if parsed is not None
    ]
    return EvidenceWindow(
        start=min(dates).isoformat() if dates else None,
        end=max(dates).isoformat() if dates else None,
        preferred_days=preferred_days,
        workout_count=len(selected),
        expanded=expanded,
        reason=reason,
    )


def _inside_window(workout: CompletedWorkout, window: EvidenceWindow) -> bool:
    start = _parse_datetime(window.start)
    end = _parse_datetime(window.end)
    workout_date = _workout_start(workout)
    if workout_date is None:
        return window.start is None
    if start is not None and workout_date < start:
        return False
    return not (end is not None and workout_date > end)


def _match_weight(match: RoutineMatch | None) -> float:
    if match is None:
        return 0.35
    if match.user_confirmed:
        return 1.0
    return {
        CONFIDENCE_HIGH: 1.0,
        CONFIDENCE_PROBABLE: 0.75,
        CONFIDENCE_AMBIGUOUS: 0.4,
        CONFIDENCE_UNMATCHED: 0.25,
    }.get(match.confidence, 0.35)


def _downgrade(confidence: str) -> str:
    order = [CONFIDENCE_HIGH, CONFIDENCE_PROBABLE, CONFIDENCE_AMBIGUOUS]
    try:
        index = order.index(confidence)
    except ValueError:
        return confidence
    return order[min(index + 1, len(order) - 1)]


def _metric_distribution(values: Sequence[float]) -> dict[str, float | None]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"min": None, "median": None, "max": None}
    return {
        "min": _round(min(clean)),
        "median": _round(statistics.median(clean)),
        "max": _round(max(clean)),
    }


def _supporting_exposure(
    workout: CompletedWorkout,
    exercise: WorkoutExercise,
    match: RoutineMatch | None,
    working: Sequence[_SetObservation],
) -> dict[str, Any]:
    top_weight = max(
        (observation.weight_kg for observation in working if observation.weight_kg),
        default=None,
    )
    best_reps = max(
        (observation.reps for observation in working if observation.reps),
        default=None,
    )
    return {
        "workout_id": workout.id,
        "workout_title": workout.title,
        "date": workout.start_time,
        "routine_id": match.routine_id if match else None,
        "routine_snapshot_id": match.routine_snapshot_id if match else None,
        "match_confidence": match.confidence if match else CONFIDENCE_UNMATCHED,
        "match_score": round(match.score, 4) if match else None,
        "working_sets": len(working),
        "top_weight_kg": _round(top_weight),
        "best_reps": _round(best_reps),
        "exercise_title": exercise.title,
    }


def infer_exercise_baselines(
    workouts: Sequence[CompletedWorkout],
    matches: Sequence[RoutineMatch],
    templates: Mapping[str, ExerciseTemplate] | None,
    *,
    snapshots: Sequence[RoutineSnapshot] = (),
    window: EvidenceWindow | None = None,
    programme_weeks: int | None = None,
    now: datetime | None = None,
    equipment_increments: Mapping[str, float] | None = None,
    source_conflicts: Mapping[str, str] | None = None,
) -> dict[str, ExerciseBaseline]:
    """Derive robust baselines for every observed exercise template."""

    if window is None:
        window = select_evidence_window(
            workouts,
            programme_weeks=programme_weeks,
            now=now,
        )
    reference = now or _parse_datetime(window.end) or _latest_workout_datetime(workouts)
    if reference is None:
        reference = datetime.now(tz=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    match_by_workout = {match.workout_id: match for match in matches}
    in_window = [workout for workout in workouts if _inside_window(workout, window)]

    grouped: dict[str, list[tuple[CompletedWorkout, WorkoutExercise]]] = {}
    for workout in in_window:
        for exercise in workout.exercises:
            grouped.setdefault(exercise.template_id, []).append((workout, exercise))

    baselines: dict[str, ExerciseBaseline] = {}
    for template_id in sorted(grouped):
        template = _template_attr(templates, template_id)
        title = (
            template.title
            if template
            else grouped[template_id][0][1].title or template_id
        )
        exercise_type = _exercise_type(template, grouped[template_id][0][1])
        if exercise_type in _ASSISTED_TYPES:
            load_basis = "assisted"
        elif exercise_type in _BODYWEIGHT_TYPES:
            load_basis = "bodyweight"
        elif exercise_type in _DURATION_TYPES:
            load_basis = "duration_distance"
        else:
            load_basis = "load"

        working_observations: list[_SetObservation] = []
        warmup_count = 0
        failure_count = 0
        dropset_count = 0
        pathological: set[str] = set()
        exposures: list[dict[str, Any]] = []
        match_weights: list[float] = []
        incomplete_sessions = 0

        for workout, exercise in grouped[template_id]:
            match = match_by_workout.get(workout.id)
            weight = _match_weight(match)
            match_weights.append(weight)
            observations = _set_observations(workout, exercise)
            known = {"type", "set_type", "weight_kg", "reps", "duration_seconds", "distance_meters", "rpe", "custom_metric", "superset_id"}
            for raw in exercise.sets:
                if isinstance(raw, dict):
                    unknown = {key for key in raw if key not in known}
                    if unknown:
                        pathological.update(f"unknown:{key}" for key in unknown)
            working = [
                observation
                for observation in observations
                if observation.kind in _WORKING_SET_TYPES
            ]
            warmups = [observation for observation in observations if observation.kind == "warmup"]
            warmup_count += len(warmups)
            failure_count += sum(1 for observation in working if observation.kind == "failure")
            dropset_count += sum(1 for observation in working if observation.kind == "dropset")
            working_observations.extend(working)

            if workout.end_time is None or workout.duration_seconds is None:
                incomplete_sessions += 1
            if not working:
                incomplete_sessions += 1

            top_weight = max(
                (observation.weight_kg for observation in working if observation.weight_kg),
                default=None,
            )
            best_reps = max(
                (observation.reps for observation in working if observation.reps),
                default=None,
            )
            e1rm_values = [
                value
                for observation in working
                if load_basis not in {"assisted", "duration_distance"}
                and observation.weight_kg is not None
                and observation.reps is not None
                and observation.weight_kg > 0
                and (value := _epley_1rm(observation.weight_kg, observation.reps))
                is not None
            ]
            rpes = [observation.rpe for observation in working if observation.rpe is not None]
            durations = [
                observation.duration_seconds
                for observation in working
                if observation.duration_seconds is not None
            ]
            distances = [
                observation.distance_meters
                for observation in working
                if observation.distance_meters is not None
            ]
            exposures.append(
                {
                    "workout": workout,
                    "exercise": exercise,
                    "match": match,
                    "weight": weight,
                    "top_weight": top_weight,
                    "best_reps": best_reps,
                    "working_sets": len(working),
                    "top_e1rm": max(e1rm_values, default=None),
                    "rpe": _median(rpes),
                    "duration": _median(durations),
                    "distance": _median(distances),
                    "date": _workout_start(workout),
                }
            )

        exposure_count = len(exposures)
        all_e1rms = [
            exposure["top_e1rm"]
            for exposure in exposures
            if exposure["top_e1rm"] is not None
        ]
        outlier_workouts: set[str] = set()
        if len(all_e1rms) >= 3:
            centre = statistics.median(all_e1rms)
            spread = _mad(all_e1rms)
            if spread is not None:
                threshold = centre + max(3 * 1.4826 * spread, centre * 0.10)
                outlier_workouts = {
                    exposure["workout"].id
                    for exposure in exposures
                    if exposure["top_e1rm"] is not None
                    and exposure["top_e1rm"] > threshold
                }
        robust_e1rms = [
            exposure["top_e1rm"]
            for exposure in exposures
            if exposure["top_e1rm"] is not None
            and exposure["workout"].id not in outlier_workouts
        ]
        robust_e1rm = _median(robust_e1rms)
        e1rm_spread = _iqr(robust_e1rms)

        top_weights = [
            exposure["top_weight"]
            for exposure in exposures
            if exposure["top_weight"] is not None and exposure["workout"].id not in outlier_workouts
        ]
        working_weights = [
            observation.weight_kg
            for observation in working_observations
            if observation.weight_kg is not None
            and observation.weight_kg > 0
            and observation.workout_id not in outlier_workouts
        ]
        all_reps = [
            observation.reps
            for observation in working_observations
            if observation.reps is not None
        ]
        exposure_reps = [
            exposure["best_reps"]
            for exposure in exposures
            if exposure["best_reps"] is not None
            and exposure["workout"].id not in outlier_workouts
        ]
        rpe_values = [
            observation.rpe
            for observation in working_observations
            if observation.rpe is not None
        ]
        duration_values = [
            observation.duration_seconds
            for observation in working_observations
            if observation.duration_seconds is not None
        ]
        distance_values = [
            observation.distance_meters
            for observation in working_observations
            if observation.distance_meters is not None
        ]
        custom_values = [
            observation.custom_metric
            for observation in working_observations
            if observation.custom_metric is not None
        ]

        dated = [
            (exposure["date"], exposure)
            for exposure in exposures
            if exposure["date"] is not None
        ]
        if dated:
            first_date = min(date for date, _ in dated)
            points = [
                (
                    (date - first_date).total_seconds() / 604800.0,
                    exposure,
                )
                for date, exposure in dated
            ]
            load_trend = _linear_trend(
                [
                    (week, float(exposure["top_weight"]))
                    for week, exposure in points
                    if exposure["top_weight"] is not None
                ]
            )
            rep_trend = _linear_trend(
                [
                    (week, float(exposure["best_reps"]))
                    for week, exposure in points
                    if exposure["best_reps"] is not None
                ]
            )
            set_trend = _linear_trend(
                [
                    (week, float(exposure["working_sets"]))
                    for week, exposure in points
                ]
            )
            span_weeks = max(
                1.0,
                (max(date for date, _ in dated) - first_date).total_seconds()
                / 604800.0,
            )
            dates_sorted = sorted({date for date, _ in dated})
            gaps = [
                (current - previous).days
                for previous, current in pairwise(dates_sorted)
            ]
            median_gap = _median(gaps) if gaps else None
        else:
            load_trend = rep_trend = set_trend = None
            span_weeks = 1.0
            median_gap = None

        equipment_increment = None
        if equipment_increments and template is not None:
            equipment_increment = equipment_increments.get(
                template_id,
                equipment_increments.get(template.equipment or ""),
            )
        if equipment_increment is None:
            equipment_increment = _infer_equipment_increment(working_weights)

        average_match_weight = (
            statistics.fmean(match_weights) if match_weights else 0.0
        )
        latest_date = max((date for date, _ in dated), default=None)
        stale = (
            latest_date is not None
            and (reference - latest_date).days > PREFERRED_EXPOSURE_WINDOW_DAYS
        )
        completeness = (
            1.0 - (incomplete_sessions / exposure_count) if exposure_count else 0.0
        )

        if exposure_count >= 8 and average_match_weight >= 0.75 and completeness >= 0.75:
            confidence = CONFIDENCE_HIGH
        elif exposure_count >= 4:
            confidence = CONFIDENCE_PROBABLE
        elif exposure_count >= 1:
            confidence = CONFIDENCE_AMBIGUOUS
        else:
            confidence = CONFIDENCE_UNMATCHED
        if stale:
            confidence = _downgrade(confidence)
        if average_match_weight < 0.5:
            confidence = _downgrade(confidence)

        warnings: list[str] = []
        if stale:
            warnings.append("stale_history")
        if outlier_workouts:
            warnings.append("outlier_personal_records_excluded")
        if incomplete_sessions:
            warnings.append("incomplete_sessions_observed")
        if window.expanded:
            warnings.append("evidence_window_expanded_for_sparse_history")
        if average_match_weight < 0.75:
            warnings.append("down_weighted_low_confidence_routine_matches")
        if equipment_increment is None:
            warnings.append("equipment_increment_not_inferable")
        if pathological:
            warnings.append("unknown_provider_set_fields_ignored")
        if source_conflicts and template is not None:
            snapshot = next(
                (
                    candidate
                    for candidate in snapshots
                    if template_id in candidate.template_ids
                ),
                None,
            )
            conflict = (
                source_conflicts.get(snapshot.routine_id) if snapshot else None
            )
            if conflict and conflict != "unchanged":
                warnings.append(f"source_conflict:{conflict}")
        if confidence == CONFIDENCE_AMBIGUOUS or confidence == CONFIDENCE_UNMATCHED:
            warnings.append("calibration_recommended")

        role_features = _role_features(template, title, exercise_type, load_basis)
        supporting = [
            _supporting_exposure(
                exposure["workout"],
                exposure["exercise"],
                exposure["match"],
                [
                    observation
                    for observation in _set_observations(
                        exposure["workout"], exposure["exercise"]
                    )
                    if observation.kind in _WORKING_SET_TYPES
                ],
            )
            for exposure in sorted(
                exposures,
                key=lambda item: (
                    item["date"] or datetime.min.replace(tzinfo=timezone.utc),
                    item["workout"].id,
                ),
                reverse=True,
            )[:20]
        ]

        baselines[template_id] = ExerciseBaseline(
            template_id=template_id,
            title=title,
            exercise_type=exercise_type,
            confidence=confidence,
            evidence_window=window.to_dict(),
            working_set_count=len(working_observations),
            warmup_set_count=warmup_count,
            failure_set_count=failure_count,
            dropset_set_count=dropset_count,
            exposure_count=exposure_count,
            supporting_workouts=supporting,
            load_kg={
                **_metric_distribution(top_weights),
                "working_set_median": _round(_median(working_weights)),
            },
            reps={
                **_metric_distribution(exposure_reps),
                "working_set_median": _round(_median(all_reps)),
            },
            duration_seconds=_metric_distribution(duration_values),
            distance_meters=_metric_distribution(distance_values),
            custom_metric=_metric_distribution(custom_values),
            e1rm_kg={
                "value": _round(robust_e1rm),
                "low": _round(robust_e1rm - e1rm_spread) if robust_e1rm is not None and e1rm_spread is not None else None,
                "high": _round(robust_e1rm + e1rm_spread) if robust_e1rm is not None and e1rm_spread is not None else None,
                "method": "median_of_exposure_best_epley" if robust_e1rm is not None else None,
                "valid": robust_e1rm is not None,
                "load_basis": load_basis,
                "outlier_count": len(outlier_workouts),
            },
            rpe={
                "median": _round(_median(rpe_values)),
                "iqr": _round(_iqr(rpe_values)),
                "count": len(rpe_values) if rpe_values else 0,
            },
            trends={
                "load_kg_per_week": _round(load_trend),
                "reps_per_week": _round(rep_trend),
                "working_sets_per_week": _round(set_trend),
            },
            variability={
                "load_cv": _round(_coefficient_of_variation(top_weights)),
                "reps_cv": _round(_coefficient_of_variation(exposure_reps)),
            },
            exposure_frequency={
                "exposures_per_week": _round(exposure_count / span_weeks),
                "median_days_between_exposures": _round(median_gap),
                "days_since_last_exposure": (
                    float((reference - latest_date).days)
                    if latest_date is not None
                    else None
                ),
            },
            equipment_increment_kg=_round(equipment_increment),
            role_features=role_features,
            warnings=warnings,
            pathological_set_fields=sorted(pathological),
        )
    return baselines


def snapshot_id_for_routine(
    snapshots: Sequence[RoutineSnapshot],
    template_id: str,
) -> str:
    """Return the snapshot ID containing *template_id*, if any."""

    for snapshot in snapshots:
        if template_id in snapshot.template_ids:
            return snapshot.id
    return ""


def _role_features(
    template: ExerciseTemplate | None,
    title: str,
    exercise_type: str,
    load_basis: str,
) -> dict[str, Any]:
    muscle = template.primary_muscle_group if template else "other"
    equipment = template.equipment if template else None
    title_lower = title.lower()
    compound = (
        muscle in _COMPOUND_MUSCLES
        or any(
            token in title_lower
            for token in ("squat", "deadlift", "press", "row", "pull-up", "clean")
        )
    )
    return {
        "primary_muscle_group": muscle,
        "secondary_muscle_groups": (
            list(template.secondary_muscle_groups) if template else []
        ),
        "equipment": equipment,
        "exercise_type": exercise_type,
        "load_basis": load_basis,
        "compound_hint": "compound" if compound else "accessory",
        "unilateral_hint": any(
            token in title_lower for token in ("single", "one-arm", "split squat", "lunge")
        ),
        "bodyweight_hint": exercise_type in _BODYWEIGHT_TYPES,
        "duration_based_hint": exercise_type in _DURATION_TYPES,
    }


# ---------------------------------------------------------------------------
# High-level analysis
# ---------------------------------------------------------------------------


def _aggregate_confidence(
    matches: Sequence[RoutineMatch],
    routine_coverage: float,
    calibration_required: bool,
) -> str:
    if not matches:
        return CONFIDENCE_UNMATCHED
    confident = sum(1 for match in matches if match.confidence in _CONFIDENT_BANDS)
    ratio = confident / len(matches)
    if ratio >= 0.75 and routine_coverage >= 0.75 and not calibration_required:
        return CONFIDENCE_HIGH
    if ratio >= 0.5 and routine_coverage >= 0.5:
        return CONFIDENCE_PROBABLE
    if any(match.confidence != CONFIDENCE_UNMATCHED for match in matches):
        return CONFIDENCE_AMBIGUOUS
    return CONFIDENCE_UNMATCHED


def analyse_history(
    data: HevyTrainingData,
    selected_routine_ids: Sequence[str] | None = None,
    *,
    programme_weeks: int | None = None,
    now: datetime | None = None,
    overrides: Iterable[MatchOverride | Mapping[str, Any]] = (),
    weights: Mapping[str, float] | None = None,
    equipment_increments: Mapping[str, float] | None = None,
    source_conflicts: Mapping[str, str] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Return the complete, typed history analysis for programme generation."""

    selected = set(selected_routine_ids) if selected_routine_ids else None
    routines = [
        routine
        for routine in data.routines
        if selected is None or routine.id in selected
    ]
    if selected is not None:
        missing = sorted(selected - {routine.id for routine in routines})
        if missing:
            raise ValueError(
                "Selected routine IDs are not available in the current Hevy source: "
                + ", ".join(missing)
            )
    snapshots = build_snapshots(
        routines,
        data.exercise_templates,
        user_id=user_id,
    )
    reference_limit = now
    if reference_limit is not None and reference_limit.tzinfo is None:
        reference_limit = reference_limit.replace(tzinfo=timezone.utc)
    workouts = [
        workout
        for workout in data.recent_workouts
        if reference_limit is None
        or (workout_date := _workout_start(workout)) is None
        or workout_date <= reference_limit
    ]
    matches = match_workouts(
        snapshots,
        workouts,
        overrides=overrides,
        weights=weights,
    )
    window = select_evidence_window(
        workouts,
        programme_weeks=programme_weeks,
        now=now,
    )
    baselines = infer_exercise_baselines(
        workouts,
        matches,
        data.exercise_templates,
        snapshots=snapshots,
        window=window,
        programme_weeks=programme_weeks,
        now=now,
        equipment_increments=equipment_increments,
        source_conflicts=source_conflicts,
    )

    counts = {band: 0 for band in CONFIDENCE_BANDS}
    for match in matches:
        counts[match.confidence] += 1

    coverage_rows: list[dict[str, Any]] = []
    matched_routine_count = 0
    workout_by_id = {workout.id: workout for workout in workouts}
    for snapshot in snapshots:
        routine_matches = [
            match
            for match in matches
            if match.routine_snapshot_id == snapshot.id
        ]
        routine_counts = {band: 0 for band in CONFIDENCE_BANDS}
        for match in routine_matches:
            routine_counts[match.confidence] += 1
        has_confident = bool(
            routine_counts[CONFIDENCE_HIGH] or routine_counts[CONFIDENCE_PROBABLE]
        )
        if has_confident:
            matched_routine_count += 1
        match_dates = [
            _workout_start(workout_by_id[match.workout_id])
            for match in routine_matches
            if match.workout_id in workout_by_id
        ]
        latest_match = max(
            (match_date for match_date in match_dates if match_date is not None),
            default=None,
        )
        coverage_rows.append(
            {
                "routine_id": snapshot.routine_id,
                "routine_snapshot_id": snapshot.id,
                "title": snapshot.title,
                "workout_count": len(routine_matches),
                "match_counts": routine_counts,
                "covered": has_confident,
                "latest_match": latest_match.isoformat() if latest_match else None,
            }
        )

    routine_coverage = (
        matched_routine_count / len(snapshots) if snapshots else 0.0
    )
    total = len(matches)
    confident_total = counts[CONFIDENCE_HIGH] + counts[CONFIDENCE_PROBABLE]
    workout_coverage = confident_total / total if total else 0.0
    baseline_confident = sum(
        1
        for baseline in baselines.values()
        if baseline.confidence in _CONFIDENT_BANDS
    )
    calibration_required = (
        total < MIN_EVIDENCE_EXPOSURES
        or not baselines
        or baseline_confident < 1
        or workout_coverage < 0.5
        or window.expanded
    )
    confidence = _aggregate_confidence(matches, routine_coverage, calibration_required)

    warnings: list[str] = []
    if not matches:
        warnings.append("no_completed_workout_history")
    if counts[CONFIDENCE_UNMATCHED]:
        warnings.append("some_workouts_remain_unmatched")
    if counts[CONFIDENCE_AMBIGUOUS]:
        warnings.append("ambiguous_matches_require_confirmation")
    if window.expanded:
        warnings.append("evidence_window_expanded_for_sparse_history")
    if calibration_required:
        warnings.append("calibration_required")
    stale_baselines = [
        template_id
        for template_id, baseline in baselines.items()
        if "stale_history" in baseline.warnings
    ]
    if stale_baselines:
        warnings.append("stale_exercise_history")

    return {
        "rule_versions": {
            "history": HISTORY_RULE_VERSION,
            "matcher": MATCHER_VERSION,
            "baseline": BASELINE_VERSION,
            "snapshot_schema": SNAPSHOT_SCHEMA_VERSION,
        },
        "reference_time": window.end,
        "evidence_window": window.to_dict(),
        "routine_snapshots": [snapshot.to_dict() for snapshot in snapshots],
        "matches": [match.to_dict() for match in matches],
        "match_counts": counts,
        "routine_coverage": coverage_rows,
        "routine_match_coverage": round(routine_coverage, 3),
        "workout_match_coverage": round(workout_coverage, 3),
        "unmatched_workout_count": counts[CONFIDENCE_UNMATCHED],
        "ambiguous_workout_count": counts[CONFIDENCE_AMBIGUOUS],
        "confidence": confidence,
        "calibration_required": calibration_required,
        "baselines": {
            template_id: baseline.to_dict()
            for template_id, baseline in sorted(baselines.items())
        },
        "warnings": warnings,
    }


def merge_workout_history(
    primary: Iterable[CompletedWorkout],
    cached: Iterable[CompletedWorkout],
) -> list[CompletedWorkout]:
    """Merge provider-recent and cache-history workouts without duplicates.

    Provider rows win when the same workout ID appears in both sources because
    they reflect the latest provider revision.  Returned order is newest first.
    """

    merged: dict[str, CompletedWorkout] = {}
    for workout in cached:
        key = workout.id or _hash(workout.start_time or "")[:16]
        merged[key] = workout
    for workout in primary:
        key = workout.id or _hash(workout.start_time or "")[:16]
        merged[key] = workout
    return sorted(
        merged.values(),
        key=lambda workout: (
            _workout_start(workout) or datetime.min.replace(tzinfo=timezone.utc),
            workout.id,
        ),
        reverse=True,
    )


def parse_cached_workouts(
    payloads: Iterable[Any],
    templates: Mapping[str, ExerciseTemplate] | None = None,
) -> list[CompletedWorkout]:
    """Parse raw cached Hevy workout payloads from the local #819 cache.

    Accepts one workout object, a list of workouts, or the wrapped
    ``{"workouts": [...]}`` shape used by the local cache.
    """

    from hevy_reader import _parse_workout

    raw_workouts: list[dict[str, Any]] = []
    for payload in payloads:
        if isinstance(payload, dict) and isinstance(payload.get("workouts"), list):
            raw_workouts.extend(
                item for item in payload["workouts"] if isinstance(item, dict)
            )
        elif isinstance(payload, list):
            raw_workouts.extend(item for item in payload if isinstance(item, dict))
        elif isinstance(payload, dict):
            raw_workouts.append(payload)
    workouts: list[CompletedWorkout] = []
    for raw in raw_workouts:
        try:
            workouts.append(_parse_workout(raw, dict(templates or {})))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    deduped: dict[str, CompletedWorkout] = {}
    for workout in workouts:
        key = workout.id or _hash(workout.start_time or "")[:16]
        deduped[key] = workout
    return sorted(
        deduped.values(),
        key=lambda workout: (
            _workout_start(workout) or datetime.min.replace(tzinfo=timezone.utc),
            workout.id,
        ),
    )


__all__ = [
    "BASELINE_VERSION",
    "CONFIDENCE_AMBIGUOUS",
    "CONFIDENCE_BANDS",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_PROBABLE",
    "CONFIDENCE_UNMATCHED",
    "DEFAULT_MATCH_WEIGHTS",
    "HISTORY_RULE_VERSION",
    "MATCHER_VERSION",
    "EvidenceWindow",
    "ExerciseBaseline",
    "MatchOverride",
    "MatchWeights",
    "RoutineMatch",
    "RoutineSnapshot",
    "SnapshotExercise",
    "analyse_history",
    "build_snapshot",
    "build_snapshots",
    "infer_exercise_baselines",
    "match_workouts",
    "merge_workout_history",
    "parse_cached_workouts",
    "select_evidence_window",
]
