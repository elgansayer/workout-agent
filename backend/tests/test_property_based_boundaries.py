"""Property-based regression tests for parser, import, and programme boundaries."""

from __future__ import annotations

from copy import deepcopy
import json
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from hypothesis import example, given, settings, strategies as st
from pydantic import ValidationError

from database import get_recent_hevy_logs, init_db, save_workout
from dynamic_programme import (
    MAX_DURATION_WEEKS,
    MIN_DURATION_WEEKS,
    ProgrammePreviewRequest,
    _allocate_blocks,
    serialise_hevy_source,
)
from hevy_parser import _top_set, normalise_name, parse_workout
from hevy_reader import (
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
    _parse_routine,
    _parse_workout,
)

GOALS = (
    "general_fitness",
    "hypertrophy",
    "maximal_strength",
    "strength_test",
    "recomposition",
    "maintenance",
    "return_to_training",
    "power_performance",
)
SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=48,
)
WEIGHTS = st.floats(
    min_value=0.0,
    max_value=2_000.0,
    allow_nan=False,
    allow_infinity=False,
)
REPS = st.integers(min_value=0, max_value=1_000)
VALID_SET = st.fixed_dictionaries(
    {},
    optional={
        "weight_kg": WEIGHTS,
        "reps": REPS,
    },
)
MALFORMED_SET = st.one_of(st.none(), st.integers(-10, 10), SAFE_TEXT)
SET_LIST = st.lists(st.one_of(VALID_SET, MALFORMED_SET), max_size=12)


REGRESSION_FIXTURES = Path(__file__).parent / "fixtures" / "property_regressions.json"


def test_shrunk_regression_fixtures_remain_executable() -> None:
    """Keep minimal counterexamples reusable after Hypothesis has shrunk them."""

    fixture = json.loads(REGRESSION_FIXTURES.read_text(encoding="utf-8"))

    for payload in fixture["parser_payloads"]:
        original = deepcopy(payload)
        first = parse_workout(payload)
        second = parse_workout(payload)
        assert payload == original
        assert first == second

    for payload in fixture["invalid_programme_requests"]:
        with pytest.raises(ValidationError):
            ProgrammePreviewRequest.model_validate(payload)

    for case in fixture["timezone_workouts"]:
        parsed = _parse_workout(case["payload"], {})
        assert parsed.duration_seconds == case["expected_duration_seconds"]


@settings(max_examples=80, deadline=None)
@given(SAFE_TEXT)
@example("  BENCH   PRESS  ")
@example("\tPull-Up\n")
def test_normalise_name_is_idempotent_and_whitespace_stable(value: str) -> None:
    once = normalise_name(value)
    twice = normalise_name(once)

    assert twice == once
    assert once == " ".join(value.lower().split())
    assert "\t" not in once
    assert "\n" not in once


@settings(max_examples=80, deadline=None)
@given(SET_LIST)
@example([{"weight_kg": 100.0, "reps": 5}, None, {"weight_kg": 100.0, "reps": 6}])
@example([{"reps": 8}, {"reps": 12}, "malformed"])
def test_top_set_is_order_independent_and_ignores_malformed_entries(
    sets: list[Any],
) -> None:
    forward = _top_set(sets)
    backward = _top_set(list(reversed(sets)))

    assert forward == backward
    assert forward[2] == sum(isinstance(item, dict) for item in sets)
    if forward[0] is not None:
        assert 0.0 <= forward[0] <= 2_000.0
    if forward[1] is not None:
        assert 0 <= forward[1] <= 1_000


@st.composite
def parser_payloads(draw) -> dict[str, Any]:
    title = draw(SAFE_TEXT)
    exercise_count = draw(st.integers(min_value=0, max_value=5))
    exercises: list[Any] = []
    for index in range(exercise_count):
        if draw(st.booleans()):
            exercises.append(draw(MALFORMED_SET))
            continue
        exercises.append(
            {
                "title": draw(SAFE_TEXT) or f"Exercise {index}",
                "sets": draw(SET_LIST),
            }
        )
    return {
        "title": title or "Workout",
        "exercises": exercises,
    }


@settings(max_examples=80, deadline=None)
@given(parser_payloads())
@example(
    {
        "title": "Regression",
        "exercises": [
            None,
            {"title": "Bodyweight", "sets": [{"reps": 10}, "broken"]},
        ],
    }
)
def test_parse_workout_is_pure_deterministic_and_bounded(
    payload: dict[str, Any],
) -> None:
    original = deepcopy(payload)

    first = parse_workout(payload)
    second = parse_workout(payload)

    assert payload == original
    assert first == second
    if first is not None:
        assert isfinite(first.total_volume_kg)
        assert first.total_volume_kg >= 0
        assert len(first.exercises) <= len(payload["exercises"])


@settings(max_examples=60, deadline=None)
@given(
    st.integers(min_value=-720, max_value=840),
    st.integers(min_value=0, max_value=7 * 24 * 60 * 60),
    WEIGHTS,
    REPS,
)
@example(840, 3_600, 100.0, 5)
@example(-720, 86_400, 0.0, 0)
def test_hevy_import_preserves_timezone_duration_and_volume_bounds(
    offset_minutes: int,
    elapsed_seconds: int,
    weight: float,
    reps: int,
) -> None:
    tz = timezone(timedelta(minutes=offset_minutes))
    start = datetime(2025, 6, 15, 12, 0, 0, tzinfo=tz)
    end = start + timedelta(seconds=elapsed_seconds)
    raw = {
        "id": "workout-1",
        "title": "Timezone regression",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "exercises": [
            {
                "exercise_template_id": "exercise-1",
                "sets": [{"weight_kg": weight, "reps": reps}],
            }
        ],
    }
    original = deepcopy(raw)

    parsed = _parse_workout(raw, {})

    assert raw == original
    assert parsed.duration_seconds == elapsed_seconds
    assert parsed.total_volume_kg == round(weight * reps, 1)
    assert isfinite(parsed.total_volume_kg)
    assert 0 <= parsed.total_volume_kg <= 2_000_000


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.one_of(VALID_SET, MALFORMED_SET), min_size=0, max_size=10),
    SAFE_TEXT,
)
@example([{"weight_kg": 50.0, "reps": 8}, "legacy-scalar"], "notes")
def test_routine_import_round_trip_is_stable_and_preserves_provider_targets(
    sets: list[Any],
    notes: str,
) -> None:
    raw = {
        "id": "routine-1",
        "title": "Imported routine",
        "folder_id": 7,
        "updated_at": "2026-08-20T12:00:00+00:00",
        "exercises": [
            {
                "exercise_template_id": "exercise-1",
                "sets": sets,
                "notes": notes,
                "rest_seconds": 90,
            }
        ],
    }
    original = deepcopy(raw)
    template = ExerciseTemplate(
        id="exercise-1",
        title="Imported exercise",
        exercise_type="weight_reps",
        primary_muscle_group="chest",
    )
    templates = {template.id: template}

    first = _parse_routine(deepcopy(raw), templates)
    second = _parse_routine(deepcopy(raw), templates)

    assert first == second
    assert raw == original
    expected_targets = [
        dict(item) if isinstance(item, dict) else {"value": item} for item in sets
    ]
    assert first.exercises[0].set_targets == expected_targets

    data = HevyTrainingData(exercise_templates=templates, routines=[first])
    source_a = serialise_hevy_source(data)
    source_b = serialise_hevy_source(data)
    assert source_a == source_b
    assert source_a["routines"][0]["source_hash"] == source_b["routines"][0]["source_hash"]


@settings(max_examples=80, deadline=None)
@given(
    st.lists(
        st.text(
            alphabet=st.characters(blacklist_categories=("Cs", "Zs")),
            min_size=1,
            max_size=24,
        ),
        min_size=1,
        max_size=8,
        unique=True,
    ),
    st.integers(min_value=MIN_DURATION_WEEKS, max_value=MAX_DURATION_WEEKS),
    st.sampled_from(GOALS),
    st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31)),
    st.one_of(st.none(), st.integers(min_value=1, max_value=14)),
    st.one_of(st.none(), st.integers(min_value=20, max_value=240)),
)
def test_programme_request_has_a_stable_round_trip(
    routine_ids: list[str],
    duration_weeks: int,
    goal: str,
    start_date: date,
    sessions_per_week: int | None,
    max_session_minutes: int | None,
) -> None:
    request = ProgrammePreviewRequest(
        selected_routine_ids=routine_ids,
        duration_weeks=duration_weeks,
        goal=goal,
        start_date=start_date,
        sessions_per_week=sessions_per_week,
        max_session_minutes=max_session_minutes,
    )
    encoded = request.model_dump(mode="json")

    restored = ProgrammePreviewRequest.model_validate(encoded)

    assert restored.model_dump(mode="json") == encoded


@settings(max_examples=50, deadline=None)
@given(
    st.one_of(
        st.integers(max_value=MIN_DURATION_WEEKS - 1),
        st.integers(
            min_value=MAX_DURATION_WEEKS + 1,
            max_value=MAX_DURATION_WEEKS + 1_000,
        ),
    )
)
@example(MIN_DURATION_WEEKS - 1)
@example(MAX_DURATION_WEEKS + 1)
def test_programme_duration_validation_fails_closed_deterministically(
    duration_weeks: int,
) -> None:
    payload = {
        "selected_routine_ids": ["routine-1"],
        "duration_weeks": duration_weeks,
        "goal": "general_fitness",
    }

    failures: list[list[tuple[tuple[Any, ...], str]]] = []
    for _ in range(2):
        with pytest.raises(ValidationError) as exc_info:
            ProgrammePreviewRequest.model_validate(payload)
        failures.append(
            [(tuple(error["loc"]), error["type"]) for error in exc_info.value.errors()]
        )

    assert failures[0] == failures[1]
    assert any(location == ("duration_weeks",) for location, _ in failures[0])


@settings(max_examples=100, deadline=None)
@given(
    st.integers(min_value=MIN_DURATION_WEEKS, max_value=MAX_DURATION_WEEKS),
    st.sampled_from(GOALS),
    st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31)),
)
@example(4, "strength_test", date(2026, 1, 1))
@example(52, "hypertrophy", date(2026, 12, 31))
def test_block_allocation_is_deterministic_contiguous_and_bounded(
    duration_weeks: int,
    goal: str,
    start_date: date,
) -> None:
    first = _allocate_blocks(duration_weeks, goal, start_date)
    second = _allocate_blocks(duration_weeks, goal, start_date)

    assert first == second
    assert sum(block["duration_weeks"] for block in first) == duration_weeks
    assert first[0]["start_week"] == 1
    assert first[-1]["end_week"] == duration_weeks

    expected_week = 1
    previous_end_date: date | None = None
    for block in first:
        assert block["start_week"] == expected_week
        assert block["end_week"] == block["start_week"] + block["duration_weeks"] - 1
        assert 1 <= block["duration_weeks"] <= 6
        assert 0 < float(block["volume_multiplier"]) <= 2

        block_start = date.fromisoformat(block["start_date"])
        block_end = date.fromisoformat(block["end_date"])
        assert block_start == start_date + timedelta(weeks=block["start_week"] - 1)
        assert block_end == start_date + timedelta(weeks=block["end_week"]) - timedelta(
            days=1
        )
        if previous_end_date is not None:
            assert block_start == previous_end_date + timedelta(days=1)
        previous_end_date = block_end
        expected_week = block["end_week"] + 1

    assert expected_week == duration_weeks + 1


@settings(max_examples=18, deadline=None)
@given(
    st.lists(st.integers(min_value=-1_000_000, max_value=1_000_000), max_size=6),
    st.lists(st.integers(min_value=-1_000_000, max_value=1_000_000), max_size=6),
)
@example([1, 2, 3], [101, 102, 103])
def test_workout_imports_remain_tenant_isolated(
    values_a: list[int],
    values_b: list[int],
) -> None:
    with TemporaryDirectory() as directory:
        db_path = str(Path(directory) / "property.db")
        init_db(db_path)

        for value in values_a:
            save_workout(
                {"tenant_marker": "a", "value": value},
                db_path,
                user_id="property-user-a",
            )
        for value in values_b:
            save_workout(
                {"tenant_marker": "b", "value": value},
                db_path,
                user_id="property-user-b",
            )

        rows_a = get_recent_hevy_logs(
            limit=20,
            db_path=db_path,
            user_id="property-user-a",
        )
        rows_b = get_recent_hevy_logs(
            limit=20,
            db_path=db_path,
            user_id="property-user-b",
        )

    assert len(rows_a) == len(values_a)
    assert len(rows_b) == len(values_b)
    assert all(row["tenant_marker"] == "a" for row in rows_a)
    assert all(row["tenant_marker"] == "b" for row in rows_b)
    assert sorted(row["value"] for row in rows_a) == sorted(values_a)
    assert sorted(row["value"] for row in rows_b) == sorted(values_b)


@settings(max_examples=50, deadline=None)
@given(REPS, WEIGHTS)
def test_equivalent_provider_dict_key_order_has_the_same_source_hash(
    reps: int,
    weight: float,
) -> None:
    template = ExerciseTemplate(
        id="exercise-1",
        title="Hash exercise",
        exercise_type="weight_reps",
        primary_muscle_group="chest",
    )
    first_target = {"reps": reps, "weight_kg": weight}
    second_target = {"weight_kg": weight, "reps": reps}

    def source_hash(target: dict[str, Any]) -> str:
        routine = Routine(
            id="routine-1",
            title="Routine",
            exercises=[
                RoutineExercise(
                    template_id=template.id,
                    title=template.title,
                    sets=1,
                    rest_seconds=90,
                    set_targets=[target],
                )
            ],
        )
        data = HevyTrainingData(
            exercise_templates={template.id: template},
            routines=[routine],
        )
        return serialise_hevy_source(data)["routines"][0]["source_hash"]

    assert source_hash(first_target) == source_hash(second_target)
