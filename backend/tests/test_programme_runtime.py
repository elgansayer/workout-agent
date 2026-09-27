"""Synthetic regressions for the repeated workout/current block defects."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any

import pytest
from programme_runtime import local_today, resolve_programme_runtime


def _active() -> dict[str, Any]:
    return {
        "source": "hevy",
        "updated_at": "2026-09-01T12:00:00Z",
        "definition": {
            "programme_spec": {"start_date": "2026-09-01", "duration_weeks": 8},
            "blocks": [
                {"number": 1, "start_week": 1, "end_week": 4},
                {"number": 2, "start_week": 5, "end_week": 8},
            ],
            "days": [
                {
                    "number": index + 1,
                    "routine_id": f"routine-{index}",
                    "focus": "Same mutable title",
                    "exercises": [
                        {
                            "name": name,
                            "template_id": name,
                            "sets": 2,
                            "rep_range": "8-12",
                            "scheme": "2 × 8-12",
                            "prescriptions": [
                                {"block_number": 1, "sets": 2, "rep_range": "8-12"},
                                {"block_number": 2, "sets": 4, "rep_range": "10-15"},
                            ],
                        }
                    ],
                }
                for index, name in enumerate(["press", "row", "leg-curl"])
            ],
        },
    }


def _workout(routine_id: str, day: str = "2026-09-10") -> dict[str, Any]:
    return {
        "id": f"workout-{day}",
        "routine_id": routine_id,
        "end_time": f"{day}T12:00:00Z",
    }


def test_completion_advances_rotation_and_missed_days_do_not_skip_sessions() -> None:
    workouts = [_workout("routine-0")]
    for today in (date(2026, 9, 11), date(2026, 9, 14)):
        result = resolve_programme_runtime(_active(), workouts, today=today)
        assert result.next_day is not None
        assert result.next_day["routine_id"] == "routine-1"
    workouts.insert(0, _workout("routine-1", "2026-09-14"))
    result = resolve_programme_runtime(_active(), workouts, today=date(2026, 9, 15))
    assert result.next_day is not None
    assert result.next_day["routine_id"] == "routine-2"


def test_latest_completion_is_chosen_by_time_and_repeated_syncs_do_not_advance_twice() -> (
    None
):
    latest = _workout("routine-2", "2026-09-15")
    result = resolve_programme_runtime(
        _active(),
        [_workout("routine-0"), latest, latest],
        today=date(2026, 9, 16),
    )
    assert result.next_day is not None
    assert result.next_day["routine_id"] == "routine-0"


def test_current_block_prescription_replaces_opening_targets_without_mutating_snapshot() -> (
    None
):
    active = _active()
    original = deepcopy(active)
    result = resolve_programme_runtime(active, [], today=date(2026, 10, 1))
    assert result.week == 5
    assert result.days[0]["exercises"][0]["scheme"] == "4 × 10-15"
    assert active == original


def test_missing_start_uses_stable_activation_date_instead_of_resetting_daily() -> None:
    active = _active()
    active["definition"]["programme_spec"].pop("start_date")
    result = resolve_programme_runtime(active, [], today=date(2026, 10, 1))
    assert result.week == 5
    assert result.start_date == date(2026, 9, 1)


@pytest.mark.parametrize(
    "today, expected",
    [
        (date(2026, 8, 31), "not_started"),
        (date(2026, 10, 27), "completed"),
    ],
)
def test_outside_programme_dates_does_not_prescribe_a_workout(
    today: date, expected: str
) -> None:
    result = resolve_programme_runtime(_active(), [], today=today)
    assert result.status == expected
    assert result.next_day is None


def test_old_and_future_workouts_do_not_advance_rotation() -> None:
    result = resolve_programme_runtime(
        _active(),
        [
            _workout("routine-1", "2026-08-31"),
            _workout("routine-1", "2026-09-30"),
        ],
        today=date(2026, 9, 11),
    )
    assert result.next_day is not None
    assert result.next_day["routine_id"] == "routine-0"


def test_unique_exercise_ids_match_renamed_historical_workout_without_routine_id() -> (
    None
):
    workout = _workout("routine-1")
    workout.pop("routine_id")
    workout.update(title="Renamed session", exercises=[{"exercise_template_id": "row"}])
    result = resolve_programme_runtime(_active(), [workout], today=date(2026, 9, 11))
    assert result.next_day is not None
    assert result.next_day["routine_id"] == "routine-2"


def test_ambiguous_or_unknown_routine_never_uses_title_as_identity() -> None:
    workout = _workout("unknown-routine")
    workout["title"] = "Same mutable title"
    result = resolve_programme_runtime(_active(), [workout], today=date(2026, 9, 11))
    assert result.status == "needs_review"
    assert result.next_day is None


def test_user_timezone_controls_rollover_and_workout_date() -> None:
    now = datetime(2026, 9, 28, 0, 30, tzinfo=timezone.utc)
    assert local_today("America/Los_Angeles", now) == date(2026, 9, 27)
    now = datetime(2026, 9, 27, 22, 46, tzinfo=timezone.utc)
    assert local_today("Asia/Tokyo", now) == date(2026, 9, 28)
    workout = _workout("routine-0", "2026-08-31")
    workout["end_time"] = "2026-08-31T16:00:00Z"
    result = resolve_programme_runtime(
        _active(), [workout], today=date(2026, 9, 1), timezone_name="Asia/Tokyo"
    )
    assert result.next_day is not None
    assert result.next_day["routine_id"] == "routine-1"
