"""Tests for the stage-prep lifestyle guidance pillars."""

from __future__ import annotations

import lifestyle


def test_high_carb_on_deadlift_days():
    for day in (1, 4):
        guidance = lifestyle.daily_guidance(day, is_rest=False)
        assert guidance.carb_tier == "high"
        assert "70%" in guidance.nutrition


def test_moderate_carb_on_leg_days():
    for day in (3, 6):
        guidance = lifestyle.daily_guidance(day, is_rest=False)
        assert guidance.carb_tier == "moderate"


def test_low_carb_on_upper_and_rest_days():
    for day in (2, 5):
        assert lifestyle.daily_guidance(day, is_rest=False).carb_tier == "low"
    assert lifestyle.daily_guidance(None, is_rest=True).carb_tier == "low"


def test_zone2_only_on_light_and_rest_days():
    # Light upper days and rest days get joint-friendly steady-state cardio.
    assert "Zone 2" in lifestyle.daily_guidance(2, is_rest=False).cardio
    assert "Zone 2" in lifestyle.daily_guidance(None, is_rest=True).cardio
    # Heavy pulling and leg days skip steady-state to protect recovery.
    assert "Skip steady-state" in lifestyle.daily_guidance(1, is_rest=False).cardio
    assert "Skip steady-state" in lifestyle.daily_guidance(3, is_rest=False).cardio


def test_no_running_or_stairmaster_mentioned_for_cardio_days():
    cardio = lifestyle.daily_guidance(5, is_rest=False).cardio
    assert "No stair-master or running" in cardio


def test_protein_target_from_bodyweight():
    guidance = lifestyle.daily_guidance(1, is_rest=False, recovery={"weight_kg": 82})
    # 82 kg * 2.2 = 180.4, rounded to 180.
    assert guidance.protein_target == "Protein: about 180 g today (2.2 g per kg)."


def test_protein_target_absent_without_weight():
    assert lifestyle.daily_guidance(1, is_rest=False).protein_target is None
    assert (
        lifestyle.daily_guidance(1, is_rest=False, recovery={"sleep_hours": 7}).protein_target
        is None
    )


def test_as_text_includes_all_pillars():
    text = lifestyle.daily_guidance(1, is_rest=False, recovery={"weight_kg": 80}).as_text()
    assert text.startswith("Today's lifestyle:")
    for marker in ("Nutrition", "Protein", "Cardio and steps", "Recovery"):
        assert marker in text
    assert "Posing" not in text


def test_training_names_the_session():
    # A training day names the day's focus.
    assert lifestyle.daily_guidance(3, is_rest=False).training == "Legs & Abs"
    lines = lifestyle.daily_guidance(3, is_rest=False).as_lines()
    assert lines[0] == "Train: Legs & Abs"


def test_training_on_rest_day():
    guidance = lifestyle.daily_guidance(None, is_rest=True)
    assert "no lifting" in guidance.training
    assert guidance.as_lines()[0].startswith("Train: Rest")

