"""Tests for optional, programme-driven lifestyle guidance."""

from __future__ import annotations

import lifestyle


def test_high_carb_for_hinge_or_pull_focus() -> None:
    guidance = lifestyle.daily_guidance("Back and hinges", is_rest=False)
    assert guidance.carb_tier == "high"


def test_moderate_carb_for_lower_body_focus() -> None:
    guidance = lifestyle.daily_guidance("Legs and trunk", is_rest=False)
    assert guidance.carb_tier == "moderate"


def test_low_carb_for_other_and_rest_days() -> None:
    assert lifestyle.daily_guidance("Shoulders and arms", is_rest=False).carb_tier == (
        "low"
    )
    assert lifestyle.daily_guidance(None, is_rest=True).carb_tier == "low"


def test_zone2_only_on_light_and_rest_days() -> None:
    assert "Zone 2" in lifestyle.daily_guidance(
        "Shoulders and arms", is_rest=False
    ).cardio
    assert "Zone 2" in lifestyle.daily_guidance(None, is_rest=True).cardio
    assert "Skip steady-state" in lifestyle.daily_guidance(
        "Legs and trunk", is_rest=False
    ).cardio


def test_protein_target_from_bodyweight() -> None:
    guidance = lifestyle.daily_guidance(
        "Back and hinges", is_rest=False, recovery={"weight_kg": 82}
    )
    assert guidance.protein_target == "Protein: about 180 g today (2.2 g per kg)."


def test_protein_target_absent_without_weight() -> None:
    assert (
        lifestyle.daily_guidance("Back and hinges", is_rest=False).protein_target
        is None
    )


def test_as_text_includes_all_pillars() -> None:
    text = lifestyle.daily_guidance(
        "Back and hinges",
        is_rest=False,
        recovery={"weight_kg": 80},
    ).as_text()
    assert text.startswith("Today's lifestyle:")
    for marker in ("Nutrition", "Protein", "Cardio and steps", "Recovery"):
        assert marker in text


def test_training_names_the_programme_focus() -> None:
    guidance = lifestyle.daily_guidance("Legs and trunk", is_rest=False)
    assert guidance.training == "Legs and trunk"
    assert guidance.as_lines()[0] == "Train: Legs and trunk"


def test_training_on_rest_day() -> None:
    guidance = lifestyle.daily_guidance(None, is_rest=True)
    assert "no lifting" in guidance.training
    assert guidance.as_lines()[0].startswith("Train: Rest")
