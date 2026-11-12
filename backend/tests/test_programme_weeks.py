"""Week materialisation, cadence and timezone handling (pure functions)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from programme_domain import weeks

START = date(2026, 9, 1)  # Tuesday


def definition(*, mode: str = "rolling", sessions: int = 3, schedule: bool = True) -> dict[str, Any]:
    days = [
        {"number": 1, "routine_id": "r-a", "focus": "Upper A"},
        {"number": 2, "routine_id": "r-b", "focus": "Lower B"},
        {"number": 3, "routine_id": "r-c", "focus": "Push C"},
    ]
    entries = []
    if schedule:
        for week in range(1, 5):
            for slot in range(sessions):
                day = START.toordinal() + (week - 1) * 7 + slot * 2
                position = (slot + (week - 1) * sessions) % 3 + 1
                entries.append(
                    {
                        "week": week,
                        "date": date.fromordinal(day).isoformat(),
                        "routine_id": days[position - 1]["routine_id"],
                        "rotation_position": position,
                    }
                )
    return {
        "days": days,
        "blocks": [
            {"number": 1, "key": "foundation", "name": "Foundation", "start_week": 1, "end_week": 2},
            {"number": 2, "key": "development", "name": "Development", "start_week": 3, "end_week": 4},
        ],
        "cadence": {"mode": mode, "training_days": list(range(sessions))},
        "programme_spec": {"sessions_per_week": sessions, "duration_weeks": 4},
        "schedule": entries,
    }


def plan(week: int = 1, *, pointer: int = 0, tz: str = "UTC", **kwargs: Any) -> weeks.WeekPlan:
    return weeks.plan_week(
        definition(**kwargs),
        start_date=START,
        duration_weeks=4,
        week_number=week,
        timezone_name=tz,
        rotation_pointer=pointer,
    )


def test_cadence_mode_reads_the_preview_shape() -> None:
    assert weeks.cadence_mode({"cadence": {"mode": "rolling"}}) == weeks.CADENCE_ROLLING
    assert weeks.cadence_mode({"cadence": {"mode": "weekly"}}) == weeks.CADENCE_CALENDAR
    assert weeks.cadence_mode({"programme_spec": {"cadence": {"mode": "rolling"}}}) == "rolling"
    assert weeks.cadence_mode({}) == weeks.CADENCE_CALENDAR  # historical behaviour


def test_rolling_week_follows_rotation_order_from_the_pointer() -> None:
    first = plan(1, pointer=0)
    assert [item.routine_position for item in first.exposures] == [1, 2, 3]
    second = plan(2, pointer=1)
    assert [item.routine_position for item in second.exposures] == [2, 3, 1]
    assert first.cadence == "rolling" and first.block["key"] == "foundation"
    assert plan(3).block["key"] == "development"


def test_calendar_week_uses_the_explicit_dates() -> None:
    calendar = plan(1, mode="weekly")
    assert calendar.cadence == "calendar"
    assert [item.scheduled_date for item in calendar.exposures] == [
        "2026-09-01",
        "2026-09-03",
        "2026-09-05",
    ]
    assert [item.routine_position for item in calendar.exposures] == [1, 2, 3]


def test_a_missed_rolling_session_is_never_skipped_because_its_weekday_passed() -> None:
    rolling = plan(1)
    summary_plan, actual = weeks.summarise_week(
        rolling, [], today=date(2026, 9, 7), definition=definition()
    )
    # All nominal dates passed, yet the routines are unchanged and still due.
    assert [item.routine_position for item in summary_plan.exposures] == [1, 2, 3]
    assert {item.status for item in summary_plan.exposures} == {weeks.STATUS_CARRIED_OVER}
    assert summary_plan.next_exposure is not None
    assert summary_plan.next_exposure.routine_position == 1
    assert actual["completed_exposures"] == 0 and actual["adherence"] == 0.0

    # Completing the first routine late advances to the second, not the third.
    done = [
        {
            "workout_id": "w1",
            "performed_at": "2026-09-07T10:00:00+00:00",
            "routine_position": 1,
            "status": "completed",
        }
    ]
    advanced, actual = weeks.summarise_week(
        rolling, done, today=date(2026, 9, 7), definition=definition()
    )
    assert advanced.exposures[0].status == "completed"
    assert advanced.next_exposure is not None and advanced.next_exposure.routine_position == 2
    assert actual["completed_exposures"] == 1


def test_calendar_cadence_reports_missed_sessions_without_rescheduling() -> None:
    calendar = plan(1, mode="weekly")
    summary_plan, _ = weeks.summarise_week(
        calendar,
        [
            {
                "workout_id": "w1",
                "performed_at": "2026-09-01T10:00:00+00:00",
                "routine_position": 1,
                "status": "completed",
            }
        ],
        today=date(2026, 9, 4),
        definition=definition(mode="weekly"),
    )
    assert [item.status for item in summary_plan.exposures] == ["completed", "missed", "planned"]
    assert summary_plan.exposures[1].scheduled_date == "2026-09-03"  # not moved


def test_exposure_classification_tracks_partial_substituted_and_skipped() -> None:
    planned = [("bench", 3), ("row", 3), ("fly", 2)]
    full = weeks.classify_session(planned, {"bench": 3, "row": 3, "fly": 2})
    assert full["status"] == weeks.STATUS_COMPLETED and full["skipped_exercises"] == []

    partial = weeks.classify_session(planned, {"bench": 3, "row": 3})
    assert partial["status"] == weeks.STATUS_PARTIAL
    assert partial["skipped_exercises"] == ["fly"]

    short = weeks.classify_session(planned, {"bench": 1, "row": 1, "fly": 1})
    assert short["status"] == weeks.STATUS_PARTIAL  # below 80% of planned sets
    assert short["completed_sets"] == 3 and short["planned_sets"] == 8

    substituted = weeks.classify_session(
        planned, {"bench": 3, "row": 3, "machine-fly": 2}, {"fly": "machine-fly"}
    )
    assert substituted["status"] == weeks.STATUS_SUBSTITUTED
    assert substituted["substitutions"] == [{"planned": "fly", "performed": "machine-fly"}]
    assert substituted["skipped_exercises"] == []

    # A substitute that was never recorded as a user decision is not inferred.
    unrecorded = weeks.classify_session(planned, {"bench": 3, "row": 3, "machine-fly": 2})
    assert unrecorded["skipped_exercises"] == ["fly"]
    assert unrecorded["extra_exercises"] == ["machine-fly"]


def test_week_summary_counts_partial_unmatched_and_ambiguous_workouts() -> None:
    rolling = plan(1)
    exposures = [
        {"workout_id": "a", "performed_at": "2026-09-01T10:00:00+00:00", "routine_position": 1,
         "status": "completed"},
        {"workout_id": "b", "performed_at": "2026-09-03T10:00:00+00:00", "routine_position": 2,
         "status": "partial", "skipped_exercises": ["curl"]},
        {"workout_id": "c", "performed_at": "2026-09-04T10:00:00+00:00", "routine_position": None,
         "match_confidence": "unmatched", "status": "unmatched"},
        {"workout_id": "d", "performed_at": "2026-09-05T10:00:00+00:00", "routine_position": None,
         "match_confidence": "ambiguous", "status": "ambiguous"},
    ]
    _, actual = weeks.summarise_week(rolling, exposures, today=date(2026, 9, 5), definition=definition())
    assert actual["completed_exposures"] == 1 and actual["partial_exposures"] == 1
    assert actual["unmatched_workouts"] == ["c"] and actual["ambiguous_workouts"] == ["d"]
    assert actual["skipped_exercises"] == ["curl"]
    assert actual["adherence"] == pytest.approx((1 + 0.5) / 3, abs=1e-3)
    # Unmatched and ambiguous workouts never advance the rotation.
    assert weeks.rolling_pointer(definition()["days"], [1, 2, None, None]) == 2


def test_dates_and_weeks_follow_the_users_timezone() -> None:
    # 23:30 UTC on the 7th is already the 8th in Auckland (week 2) ...
    stamp = "2026-09-07T23:30:00+00:00"
    assert weeks.local_date_of(stamp, "UTC") == date(2026, 9, 7)
    assert weeks.local_date_of(stamp, "Pacific/Auckland") == date(2026, 9, 8)
    assert weeks.week_for_date(START, 4, weeks.local_date_of(stamp, "UTC")) == 1  # type: ignore[arg-type]
    assert weeks.week_for_date(START, 4, weeks.local_date_of(stamp, "Pacific/Auckland")) == 2  # type: ignore[arg-type]
    # ... while 03:00 UTC on the 8th is still the 7th in Los Angeles (week 1).
    late = "2026-09-08T03:00:00+00:00"
    assert weeks.week_for_date(START, 4, weeks.local_date_of(late, "America/Los_Angeles")) == 1  # type: ignore[arg-type]
    assert weeks.week_for_date(START, 4, weeks.local_date_of(late, "UTC")) == 2  # type: ignore[arg-type]
    # Unknown zones fall back to UTC, naive timestamps are UTC, nothing raises.
    assert weeks.local_date_of("2026-09-08T03:00:00", "Not/AZone") == date(2026, 9, 8)
    assert weeks.local_date_of("garbage", "UTC") is None
    assert plan(1, tz="Pacific/Auckland").timezone == "Pacific/Auckland"


def test_week_boundaries_and_programme_range() -> None:
    assert weeks.week_bounds(START, 2) == (date(2026, 9, 8), date(2026, 9, 14))
    assert weeks.week_for_date(START, 4, date(2026, 8, 31)) is None
    assert weeks.week_for_date(START, 4, date(2026, 9, 28)) == 4
    assert weeks.week_for_date(START, 4, date(2026, 9, 29)) is None
    with pytest.raises(ValueError):
        plan(5)
    assert weeks.paused_weeks(date(2026, 9, 1), date(2026, 9, 1)) == 0
    assert weeks.paused_weeks(date(2026, 9, 1), date(2026, 9, 6)) == 0  # under a week
    assert weeks.paused_weeks(date(2026, 9, 1), date(2026, 9, 9)) == 1
    assert weeks.paused_weeks(date(2026, 9, 1), date(2026, 9, 15)) == 2


def test_frequency_fatigue_period_reduces_planned_sessions_for_those_weeks_only() -> None:
    data = definition(schedule=False)
    data["fatigue_periods"] = [{"weeks": [2], "sessions_per_week": 2, "mode": "frequency"}]
    counts = [
        len(
            weeks.plan_week(
                data, start_date=START, duration_weeks=4, week_number=week, timezone_name="UTC"
            ).exposures
        )
        for week in (1, 2, 3)
    ]
    assert counts == [3, 2, 3]


@settings(max_examples=100, deadline=None)
@given(st.lists(st.one_of(st.none(), st.integers(1, 3)), max_size=20), st.integers(0, 5))
def test_property_rotation_pointer_only_follows_known_completions(
    completed: list[int | None], extra: int
) -> None:
    days = definition()["days"]
    pointer = weeks.rolling_pointer(days, completed)
    known = [item for item in completed if item is not None]
    assert 0 <= pointer < 3
    assert pointer == ((known[-1]) % 3 if known else 0)
    week = weeks.plan_week(
        definition(schedule=False),
        start_date=START,
        duration_weeks=4,
        week_number=1,
        timezone_name="UTC",
        rotation_pointer=pointer + 3 * extra,
    )
    positions = [item.routine_position for item in week.exposures]
    assert positions == [(pointer + offset) % 3 + 1 for offset in range(3)]
