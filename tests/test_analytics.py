"""Tests for the pure analytics helpers."""

from __future__ import annotations

import analytics


def test_epley_1rm():
    assert analytics.epley_1rm(100, 5) == round(100 * (1 + 5 / 30), 1)
    assert analytics.epley_1rm(None, 5) is None
    assert analytics.epley_1rm(100, 0) is None


def test_dots_score_is_positive_and_scales_with_load():
    light = analytics.dots_score(80, 150)
    heavy = analytics.dots_score(80, 200)
    assert light and heavy
    assert heavy > light


def test_dots_score_rejects_bad_input():
    assert analytics.dots_score(0, 100) is None
    assert analytics.dots_score(80, 0) is None
    assert analytics.dots_score(None, None) is None


def test_linear_fit_recovers_known_line():
    fit = analytics.linear_fit([0, 1, 2, 3], [1, 3, 5, 7])  # y = 2x + 1
    assert fit is not None
    slope, intercept = fit
    assert round(slope, 6) == 2.0
    assert round(intercept, 6) == 1.0


def test_linear_fit_needs_variance():
    assert analytics.linear_fit([1, 1, 1], [2, 3, 4]) is None
    assert analytics.linear_fit([1], [2]) is None


def test_project_extrapolates():
    points = [(0.0, 100.0), (10.0, 110.0)]  # +1/day
    assert analytics.project(points, 20) == 120.0
    assert analytics.project([(0.0, 1.0)], 5) is None


def test_muscle_group_classification():
    cases = {
        "Deadlift (Barbell)": "Back",
        "Strict Pull-Ups": "Back",
        "Chest-Supported T-Bar Rows": "Back",
        "Incline Dumbbell Flyes": "Chest",
        "Incline Smith Machine Press": "Chest",
        "Cable Lateral Raises": "Shoulders",
        "Reverse Pec Deck Flyes": "Shoulders",
        "Incline Dumbbell Curls": "Arms",
        "Tricep Overhead Cable Extensions": "Arms",
        "Reverse-Grip Cable Curls": "Arms",
        "Lying Leg Curls": "Legs",
        "Leg Press": "Legs",
        "Leg Extensions": "Legs",
        "Leg Press Calf Raises": "Legs",
        "Hanging Leg Raises": "Core",
        "Kneeling Cable Crunches": "Core",
    }
    for name, expected in cases.items():
        assert analytics.muscle_group_for(name) == expected, name


def test_group_volumes_sums_by_group():
    rows = [
        {"exercise": "Deadlift (Barbell)", "volume": 1000.0},
        {"exercise": "Chest-Supported T-Bar Rows", "volume": 500.0},
        {"exercise": "Leg Press", "volume": 800.0},
        {"exercise": "Unknown Move", "volume": 0.0},
    ]
    totals = analytics.group_volumes(rows)
    assert totals["Back"] == 1500.0
    assert totals["Legs"] == 800.0
    assert "Other" not in totals  # zero volume is dropped
