"""Tests for parsing raw Hevy payloads into compact summaries."""

from __future__ import annotations

from hevy_parser import normalise_name, parse_workout


def _payload() -> dict:
    return {
        "workouts": [
            {
                "title": "Legs & Abs",
                "start_time": "2026-06-17T07:00:00Z",
                "exercises": [
                    {
                        "title": "Leg Press",
                        "sets": [
                            {"weight_kg": 100.0, "reps": 10},
                            {"weight_kg": 120.0, "reps": 12},
                            {"weight_kg": 110.0, "reps": 11},
                        ],
                    },
                    {
                        "title": "Hanging Leg Raises",
                        "sets": [
                            {"weight_kg": None, "reps": 15},
                            {"weight_kg": None, "reps": 12},
                        ],
                    },
                ],
            }
        ]
    }


def test_parse_none_returns_none():
    assert parse_workout(None) is None


def test_parse_empty_workouts_returns_none():
    assert parse_workout({"workouts": []}) is None


def test_parse_top_set_is_heaviest():
    summary = parse_workout(_payload())
    assert summary is not None
    leg_press = summary.exercises[0]
    assert leg_press.name == "Leg Press"
    assert leg_press.top_weight_kg == 120.0
    assert leg_press.top_reps == 12
    assert leg_press.sets == 3


def test_parse_bodyweight_uses_reps():
    summary = parse_workout(_payload())
    raises = summary.exercises[1]
    assert raises.top_weight_kg is None
    assert raises.top_reps == 15


def test_hit_top_of_range_flag():
    targets = {"leg press": 12, "hanging leg raises": 15}
    summary = parse_workout(_payload(), targets)
    assert summary.exercises[0].hit_top_of_range is True
    assert summary.exercises[1].hit_top_of_range is True


def test_below_top_of_range_flag():
    targets = {"leg press": 15}
    summary = parse_workout(_payload(), targets)
    assert summary.exercises[0].hit_top_of_range is False


def test_parse_accepts_single_workout_dict():
    workout = _payload()["workouts"][0]
    summary = parse_workout(workout)
    assert summary is not None
    assert summary.title == "Legs & Abs"


def test_as_text_renders_lines():
    summary = parse_workout(_payload(), {"leg press": 12})
    text = summary.as_text()
    assert "Legs & Abs" in text
    assert "Leg Press: 120 kg x 12" in text
    assert "hit the top of the rep range" in text


def test_normalise_name():
    assert normalise_name("  Leg   PRESS ") == "leg press"
