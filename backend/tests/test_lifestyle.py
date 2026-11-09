"""Tests for the lifestyle guidance pillars."""

from __future__ import annotations

import lifestyle


def test_moderate_carb_on_training_days() -> None:
    guidance = lifestyle.daily_guidance("Upper Strength", is_rest=False)
    assert guidance.carb_tier == "moderate"
    assert "carbs" in guidance.nutrition


def test_low_carb_on_rest_days() -> None:
    assert lifestyle.daily_guidance(None, is_rest=True).carb_tier == "low"


def test_zone2_available_on_rest_days() -> None:
    assert "Zone 2" in lifestyle.daily_guidance(None, is_rest=True).cardio
    assert "Zone 2" in lifestyle.daily_guidance("Upper Strength", is_rest=False).cardio


def test_protein_target_from_bodyweight() -> None:
    guidance = lifestyle.daily_guidance(
        "Upper Strength",
        is_rest=False,
        recovery={"weight_kg": 82},
    )
    # 82 kg * 2.2 = 180.4, rounded to 180.
    assert guidance.protein_target == "Protein: about 180 g today (2.2 g per kg)."


def test_protein_target_absent_without_weight() -> None:
    assert lifestyle.daily_guidance("Upper Strength", is_rest=False).protein_target is None
    assert (
        lifestyle.daily_guidance(
            "Upper Strength",
            is_rest=False,
            recovery={"sleep_hours": 7},
        ).protein_target
        is None
    )


def test_as_text_includes_all_pillars() -> None:
    text = lifestyle.daily_guidance(
        "Upper Strength",
        is_rest=False,
        recovery={"weight_kg": 80},
    ).as_text()
    assert text.startswith("Today's lifestyle:")
    for marker in ("Nutrition", "Protein", "Cardio and steps", "Recovery"):
        assert marker in text


def test_training_names_the_selected_routine() -> None:
    guidance = lifestyle.daily_guidance("Upper Strength", is_rest=False)
    assert guidance.training == "Upper Strength"
    assert guidance.as_lines()[0] == "Train: Upper Strength"


def test_training_on_rest_day() -> None:
    guidance = lifestyle.daily_guidance(None, is_rest=True)
    assert "no lifting" in guidance.training
    assert guidance.as_lines()[0].startswith("Train: Rest")
