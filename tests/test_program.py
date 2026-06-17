"""Tests for the structured programme data and weekday scheduling."""

from __future__ import annotations

from datetime import date

import pytest

import program as p


def test_day_focus_all_six_days():
    expected = {
        1: "Back, Deadlifts & Chest",
        2: "Shoulders & Arms",
        3: "Legs & Abs",
        4: "Back, Deadlifts & Chest",
        5: "Shoulders & Arms",
        6: "Legs & Abs",
    }
    for day, focus in expected.items():
        assert p.day_focus(day) == focus


def test_format_day_all_six_days():
    block = p.BLOCKS[1]
    for day in range(1, 7):
        rendered = p.format_day(day, block)
        assert rendered.startswith(p.day_focus(day))
        # First line is the focus; the rest are exercises.
        assert rendered.count("\n") == len(p.day_exercises(day, block))


def test_day_exercises_returns_copies():
    block = p.BLOCKS[1]
    first = p.day_exercises(1, block)
    first.clear()
    assert len(p.day_exercises(1, block)) > 0  # internal data untouched


def test_back_day_includes_block_main_lifts():
    block = p.BLOCKS[2]
    exercises = p.day_exercises(1, block)
    assert exercises[0].name == "Deadlift (Barbell)"
    assert exercises[0].sets == block.deadlift.sets
    assert exercises[0].rep_range == block.deadlift.rep_range
    assert exercises[0].template_id == block.deadlift.template_id
    assert exercises[1].name == "Strict Pull-Ups"
    assert exercises[1].template_id == block.pullups.template_id


def test_block_for_week_cycles():
    assert p.block_for_week(1).number == 1
    assert p.block_for_week(4).number == 1
    assert p.block_for_week(5).number == 2
    assert p.block_for_week(8).number == 2
    assert p.block_for_week(9).number == 3
    assert p.block_for_week(12).number == 3
    # Wraps after the 12-week cycle.
    assert p.block_for_week(13).number == 1


def test_week_in_cycle():
    start = date(2026, 1, 5)  # a Monday
    assert p.week_in_cycle(start, date(2026, 1, 5)) == 1
    assert p.week_in_cycle(start, date(2026, 1, 12)) == 2
    # 12 weeks later wraps back to week 1.
    assert p.week_in_cycle(start, date(2026, 3, 30)) == 1
    # A date before the start clamps to week 1.
    assert p.week_in_cycle(start, date(2025, 12, 1)) == 1


def test_weekday_mapping_matches_split():
    # Monday=0 ... Saturday=5 map to days 1..6; Sunday=6 is rest.
    assert p.day_for_weekday(0) == 1
    assert p.day_for_weekday(3) == 4
    assert p.day_for_weekday(5) == 6
    assert p.day_for_weekday(6) is None


def test_is_rest_day():
    assert p.is_rest_day(6) is True
    for weekday in range(6):
        assert p.is_rest_day(weekday) is False


def test_today_day_for_known_dates():
    assert p.today_day(date(2026, 6, 17)) == 3  # a Wednesday
    assert p.today_day(date(2026, 6, 21)) is None  # a Sunday


def test_rep_targets_extracts_top_of_range():
    targets = p.rep_targets()
    assert targets["incline dumbbell flyes"] == 15
    assert targets["cable lateral raises"] == 20
    assert targets["deadlift (barbell)"] == 8  # Block 1 default


def test_rep_targets_respects_block():
    targets = p.rep_targets(p.BLOCKS[2])
    assert targets["deadlift (barbell)"] == 5  # Block 2 is 3-5


@pytest.mark.parametrize("day", [0, 7, 99])
def test_day_focus_rejects_invalid_day(day):
    with pytest.raises(KeyError):
        p.day_focus(day)
