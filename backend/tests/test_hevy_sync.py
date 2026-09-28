"""Tests for building Hevy routine payloads from the programme."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import hevy_sync
import pytest
from config import Config
from program import BLOCKS, Exercise, day_exercises


def test_every_programme_exercise_has_a_template_id() -> None:
    for day in (1, 2, 3):
        for exercise in day_exercises(day, BLOCKS[1]):
            assert exercise.template_id is not None, exercise.name


def test_parse_rep_range_single_and_range() -> None:
    assert hevy_sync._parse_rep_range("12") == (12, None)
    assert hevy_sync._parse_rep_range("12-15") == (12, 15)
    assert hevy_sync._parse_rep_range("15-20") == (15, 20)
    assert hevy_sync._parse_rep_range("") == (None, None)


def test_build_set_uses_rep_range_for_ranges() -> None:
    single = hevy_sync._build_set("12")
    assert single["reps"] == 12
    assert "rep_range" not in single

    spread = hevy_sync._build_set("10-12")
    assert spread["reps"] is None
    assert spread["rep_range"] == {"start": 10, "end": 12}


def test_build_exercise_sets_count_and_template() -> None:
    exercise = Exercise("Leg Press", 3, "10-12", "feet flat", "C7973E0E")
    built = hevy_sync._build_exercise(exercise)
    assert built is not None
    assert built["exercise_template_id"] == "C7973E0E"
    assert built["rest_seconds"] == hevy_sync.DEFAULT_REST_SECONDS
    assert built["notes"] == "feet flat"
    assert len(built["sets"]) == 3


def test_build_exercise_skips_unmapped() -> None:
    built = hevy_sync._build_exercise(Exercise("Made Up Lift", 3, "10"))
    assert built is None


def test_build_exercises_filters_unmapped() -> None:
    exercises = [
        Exercise("Leg Press", 3, "10-12", "", "C7973E0E"),
        Exercise("Made Up Lift", 3, "10"),
    ]
    built = hevy_sync._build_exercises(exercises)
    assert len(built) == 1
    assert built[0]["exercise_template_id"] == "C7973E0E"


def test_content_hash_is_stable_and_sensitive() -> None:
    a = hevy_sync._build_exercises(day_exercises(3, BLOCKS[1]))
    h1 = hevy_sync._content_hash("Legs & Abs", a, "notes")
    h2 = hevy_sync._content_hash("Legs & Abs", a, "notes")
    assert h1 == h2
    h3 = hevy_sync._content_hash("Legs & Abs", a, "different")
    assert h1 != h3


def test_routine_id_from_response_handles_all_shapes() -> None:
    assert hevy_sync._routine_id_from_response({"routine": [{"id": "abc"}]}) == "abc"
    assert hevy_sync._routine_id_from_response({"routine": {"id": "def"}}) == "def"
    assert hevy_sync._routine_id_from_response({"id": "ghi"}) == "ghi"
    assert hevy_sync._routine_id_from_response({"routine": []}) is None
    assert hevy_sync._routine_id_from_response(None) is None


def test_routine_id_from_response_string_not_dict() -> None:
    assert hevy_sync._routine_id_from_response("not a dict") is None  # type: ignore[arg-type]
    assert hevy_sync._routine_id_from_response({}) is None


def test_build_exercise_empty_note_becomes_null() -> None:
    built = hevy_sync._build_exercise(Exercise("Leg Press", 3, "10-12", "", "C7973E0E"))
    assert built is not None
    assert built["notes"] is None


def test_build_set_includes_prefilled_weight() -> None:
    s = hevy_sync._build_set("10-12", 80.0)
    assert s["weight_kg"] == 80.0
    single = hevy_sync._build_set("12", 40.0)
    assert single["weight_kg"] == 40.0
    assert single["reps"] == 12


def test_build_exercises_applies_named_weights() -> None:
    exercises = [
        Exercise("Leg Press", 3, "10-12", "", "C7973E0E"),
        Exercise("Leg Extensions", 4, "15", "", "75A4F6C4"),
    ]
    built = hevy_sync._build_exercises(exercises, {"Leg Press": 100.0})
    weights = {e["exercise_template_id"]: e["sets"][0]["weight_kg"] for e in built}
    assert weights["C7973E0E"] == 100.0  # Leg Press got its target
    assert weights["75A4F6C4"] is None  # Leg Extensions had no history


def test_latest_top_set_picks_recent_heaviest() -> None:
    history = [
        {"workout_start_time": "2026-01-01T08:00:00Z", "weight_kg": 90, "reps": 12},
        {"workout_start_time": "2026-02-01T08:00:00Z", "weight_kg": 95, "reps": 8},
        {"workout_start_time": "2026-02-01T08:00:00Z", "weight_kg": 100, "reps": 6},
    ]
    best = hevy_sync._latest_top_set(history)
    assert best is not None
    assert best["weight_kg"] == 100


def test_latest_top_set_empty_list() -> None:
    assert hevy_sync._latest_top_set([]) is None


def test_latest_top_set_missing_weight_kg() -> None:
    history = [
        {"workout_start_time": "2026-02-01T08:00:00Z", "no_weight": True, "reps": 12},
    ]
    best = hevy_sync._latest_top_set(history)
    # Should return the entry even without weight_kg for skill lifts
    assert best is not None
    assert best.get("weight_kg") is None


def test_target_weight_progresses_when_top_of_range_hit() -> None:
    # Leg Press range 10-12; last top set was 12 reps at 100kg -> bump.
    ex = Exercise("Leg Press", 3, "10-12", "", "C7973E0E")
    history = [
        {"workout_start_time": "2026-02-01T08:00:00Z", "weight_kg": 100, "reps": 12},
    ]
    assert hevy_sync._target_weight(ex, history) == 102.5


def test_target_weight_holds_when_below_top() -> None:
    ex = Exercise("Leg Press", 3, "10-12", "", "C7973E0E")
    history = [
        {"workout_start_time": "2026-02-01T08:00:00Z", "weight_kg": 100, "reps": 10},
    ]
    assert hevy_sync._target_weight(ex, history) == 100.0


def test_target_weight_none_without_history() -> None:
    assert (
        hevy_sync._target_weight(Exercise("Leg Press", 3, "10-12", "", "C7973E0E"), [])
        is None
    )


def test_target_weight_no_best_set() -> None:
    ex = Exercise("Leg Press", 3, "10-12", "", "C7973E0E")
    history = [
        {"different_key": True},
    ]
    assert hevy_sync._target_weight(ex, history) is None


def test_target_weight_single_rep_target() -> None:
    ex = Exercise("Leg Press", 3, "12", "", "C7973E0E")
    history = [
        {"workout_start_time": "2026-02-01T08:00:00Z", "weight_kg": 100, "reps": 12},
    ]
    assert hevy_sync._target_weight(ex, history) == 102.5


def test_target_weight_single_rep_target_below() -> None:
    ex = Exercise("Leg Press", 3, "12", "", "C7973E0E")
    history = [
        {"workout_start_time": "2026-02-01T08:00:00Z", "weight_kg": 100, "reps": 10},
    ]
    assert hevy_sync._target_weight(ex, history) == 100.0


def test_target_weight_weight_kg_is_none() -> None:
    ex = Exercise("Leg Press", 3, "10-12", "", "C7973E0E")
    history = [
        {"workout_start_time": "2026-02-01T08:00:00Z", "weight_kg": None, "reps": 12},
    ]
    assert hevy_sync._target_weight(ex, history) is None


def test_find_existing_routine_id_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hevy_sync,
        "get_routines",
        lambda api_key: [{"id": "r1", "title": "Chest"}, {"id": "r2", "title": "Legs"}],
    )
    result = hevy_sync._find_existing_routine_id("key", "Legs")
    assert result == "r2"


def test_find_existing_routine_id_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hevy_sync, "get_routines", lambda api_key: [{"id": "r1", "title": "Chest"}]
    )
    result = hevy_sync._find_existing_routine_id("key", "Arms")
    assert result is None


def test_find_existing_routine_id_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hevy_sync, "get_routines", lambda api_key: None)
    result = hevy_sync._find_existing_routine_id("key", "Arms")
    assert result is None


def test_target_weight_weights_is_float_when_above_top() -> None:
    """Ensure weight is always returned as float, not rounded."""
    ex = Exercise("Leg Press", 3, "10-12", "", "C7973E0E")
    history = [
        {"workout_start_time": "2026-02-01T08:00:00Z", "weight_kg": 100, "reps": 15},
    ]
    result = hevy_sync._target_weight(ex, history)
    assert isinstance(result, float)
    assert result == 102.5


def _config_for_sync(tmp_path: Path) -> Config:
    return Config(
        hevy_api_key="synthetic-hevy-key",
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        health_connect_file=None,
        database_path=str(tmp_path),
        hevy_sync_routines=True,
        hevy_prefill_weights=False,
        checkin_enabled=False,
        lifestyle_enabled=False,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=False,
        self_review_weekday=6,
    )


def test_sync_routines_threads_user_id_through_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config_for_sync(tmp_path)
    captured: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        hevy_sync,
        "_migrate_titles",
        lambda _config, *, user_id=None: captured.append(("migrate", user_id)),
    )
    monkeypatch.setattr(
        hevy_sync,
        "get_programme_start_date",
        lambda _db, *, user_id=None: (
            captured.append(("programme", user_id)) or date(2026, 9, 18)
        ),
    )
    monkeypatch.setattr(
        hevy_sync,
        "_ensure_folder",
        lambda _config, *, user_id=None: captured.append(("folder", user_id)) or None,
    )
    monkeypatch.setattr(
        hevy_sync,
        "_sync_session",
        lambda _config, title, built, folder_id, notes, *, user_id=None: (
            captured.append((title, user_id)) or f"{title}: up to date"
        ),
    )

    statuses = hevy_sync.sync_routines(config, user_id="user-a")

    assert len(statuses) == 3
    assert captured
    assert all(user_id == "user-a" for _operation, user_id in captured)
