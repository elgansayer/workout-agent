"""Jobs, notifications, exports and AI prompts all honour per-user preferences."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import checkin
import insight_cron
import main
from config import Config
from data_classification import DataClass, classify_field, sanitize_for_export
from database import get_or_create_user, init_db
from gemini_engine import (
    _build_checkin_prompt,
    _build_prompt,
    _build_rest_prompt,
    _format_history,
    generate_next_workout,
)
from health_export import HealthExport
from hevy_parser import ExerciseSummary, WorkoutSummary
from insights import LiftInsight, RecoveryInsight, build_insights
from programme_runtime import ACTIVE, ProgrammeRuntime
from user_settings import UserSettings, export_preferences, update_user_settings

DAY = {
    "focus": "Pull",
    "exercises": [{"name": "Deadlift", "sets": 3, "rep_range": "5", "scheme": "3 x 5"}],
}
RUNTIME = ProgrammeRuntime(
    status=ACTIVE,
    name="Synthetic programme",
    week=1,
    cycle_weeks=12,
    block={"name": "Base", "start_week": 1, "end_week": 4},
    days=[DAY],
    next_day=DAY,
)

METRIC = UserSettings()
IMPERIAL_US = UserSettings(
    timezone="America/Chicago", locale="en-US", unit_system="imperial"
)


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# AI prompts (daily plan, rest day, check-in)
# ---------------------------------------------------------------------------


def _plan_prompt(settings: UserSettings | None = None, **overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "runtime": RUNTIME,
        "day": DAY,
        "workout_summary": None,
        "recovery": None,
        "history": {"Deadlift": {"top_weight_kg": 100, "top_reps": 5}},
    }
    arguments.update(overrides)
    return _build_prompt(**arguments, settings=settings)


def test_default_prompt_keeps_the_existing_british_metric_wording() -> None:
    prompt = _plan_prompt()
    assert "Deadlift: 100 kg x 5" in prompt
    assert '"(+2.5 kg)"' in prompt
    assert prompt.endswith("Use British English. Never use the em dash.")


def test_imperial_prompt_converts_history_and_load_steps() -> None:
    prompt = _plan_prompt(IMPERIAL_US)
    assert "Deadlift: 220.5 lb x 5" in prompt
    assert '"(+5 lb)"' in prompt
    assert "Deadlift: 100 kg" not in prompt
    assert "Convert to pounds" in prompt


def test_prompt_states_the_users_zone_locale_and_spelling() -> None:
    prompt = _plan_prompt(IMPERIAL_US)
    assert "Time zone: America/Chicago" in prompt
    assert "Locale: en-US" in prompt
    assert prompt.endswith("Use American English. Never use the em dash.")
    assert "British English" not in prompt


def test_prompt_summary_text_uses_the_users_units() -> None:
    summary = WorkoutSummary(
        title="Day 1",
        date="2026-03-02",
        duration_seconds=3600,
        total_volume_kg=2500.0,
        exercises=[ExerciseSummary("Deadlift", 140.0, 5, 3)],
    )
    metric = _plan_prompt(METRIC, workout_summary=summary)
    imperial = _plan_prompt(IMPERIAL_US, workout_summary=summary)
    assert "Volume: 2500 kg" in metric and "Deadlift: 140 kg x 5" in metric
    assert "Volume: 5511.6 lb" in imperial and "Deadlift: 308.6 lb x 5" in imperial


def test_generate_next_workout_passes_the_settings_to_the_prompt() -> None:
    provider = MagicMock()
    provider.generate.return_value = "plan text"

    generate_next_workout(
        provider,
        runtime=RUNTIME,
        day=DAY,
        history={"Deadlift": {"top_weight_kg": 100, "top_reps": 5}},
        settings=IMPERIAL_US,
    )

    assert "Deadlift: 220.5 lb x 5" in provider.generate.call_args.args[0]


def test_format_history_is_unit_aware() -> None:
    history: dict[str, dict[str, Any]] = {
        "Squat": {"top_weight_kg": 60.0, "top_reps": 8},
        "Pull-up": {"top_reps": 10},
    }
    assert _format_history(history) == "- Pull-up: 10 reps (bodyweight)\n- Squat: 60 kg x 8"
    assert "- Squat: 132.3 lb x 8" in _format_history(history, "imperial")


def test_rest_day_and_checkin_prompts_carry_the_same_context() -> None:
    rest = _build_rest_prompt(RUNTIME, {"sleep_hours": 7}, settings=IMPERIAL_US)
    check = _build_checkin_prompt(
        RUNTIME, 1, 4, 24, "- Deadlift: latest 220.5 lb x 5", settings=IMPERIAL_US
    )
    for prompt in (rest, check):
        assert "Time zone: America/Chicago" in prompt
        assert "Convert to pounds" in prompt
        assert prompt.endswith("Use American English. Never use the em dash.")
    default = _build_rest_prompt(RUNTIME, None)
    assert default.endswith("Use British English. Never use the em dash.")
    assert "Time zone: UTC" in default


# ---------------------------------------------------------------------------
# Deterministic text that becomes notifications
# ---------------------------------------------------------------------------


def _insights_for(unit_system: str) -> Any:
    history = {
        "Bench": [
            {"top_weight_kg": 80.0, "top_reps": 5},
            {"top_weight_kg": 82.5, "top_reps": 5},
            {"top_weight_kg": 85.0, "top_reps": 5},
        ]
    }
    body = [{"weight_kg": 82.0, "date": "2026-03-01"}]
    return build_insights(history, body, {"sleep_hours": 8}, unit_system=unit_system)


def test_self_review_text_is_metric_by_default_and_imperial_on_request() -> None:
    metric = _insights_for("metric")
    imperial = _insights_for("imperial")

    assert "Bodyweight 82 kg" in metric.recovery.as_text()
    assert " kg e1RM" in metric.as_text()
    assert "Bodyweight 180.8 lb" in imperial.recovery.as_text()
    assert " lb e1RM" in imperial.as_text()
    assert "kg" not in imperial.as_message(week=2, block_name="Block")
    assert "lb" in imperial.as_message(week=2, block_name="Block")


def test_bodyweight_lifts_stay_in_reps_whatever_the_unit_system() -> None:
    lift = LiftInsight(
        name="Pull-up",
        metric="reps",
        sessions=3,
        latest=12.0,
        best=12.0,
        change_pct=0.1,
        slope_per_session=1.0,
        trend="progressing",
        sessions_since_best=0,
        intervention=None,
        unit_system="imperial",
    )
    assert "latest 12 reps" in lift.as_line()


def test_recovery_text_without_weight_is_unaffected() -> None:
    recovery = RecoveryInsight(
        sleep_hours=7.0,
        resting_hr=55,
        resting_hr_trend=None,
        weight_kg=None,
        weight_trend=None,
        body_fat_pct=None,
        body_fat_trend=None,
        muscle_pct=None,
        is_catabolic=False,
        status="good",
        directive="Push.",
        unit_system="imperial",
    )
    assert "Bodyweight" not in recovery.as_text()


def test_checkin_text_uses_the_users_units() -> None:
    history = [
        {"workout_start_time": "2026-01-01T08:00:00Z", "weight_kg": 100, "reps": 8},
        {"workout_start_time": "2026-01-08T08:00:00Z", "weight_kg": 110, "reps": 8},
        {"workout_start_time": "2026-01-15T08:00:00Z", "weight_kg": 110, "reps": 8},
    ]
    metric = checkin._review_exercise("Deadlift", "4 x 5-8", "5-8", history)
    imperial = checkin._review_exercise("Deadlift", "4 x 5-8", "5-8", history, "imperial")
    assert metric.latest == "110 kg x 8"
    assert imperial.latest == "242.5 lb x 8"
    # The comparison itself is always done on the stored kilograms.
    assert imperial.change_kg == metric.change_kg == 10

    assert "weight change +10 kg" in checkin._analysis_text([metric])
    assert "weight change +22 lb" in checkin._analysis_text([imperial], "imperial")
    due = checkin.CheckinDue(1, 24, 4, None)
    assert "up +10 kg" in checkin._fallback_message(due, RUNTIME, [metric])
    assert "up +22 lb" in checkin._fallback_message(due, RUNTIME, [imperial], "imperial")


def test_checkin_dates_follow_the_acting_users_local_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "checkin.db")
    init_db(db)
    uid = get_or_create_user("checkin@example.test", None, db)["id"]
    update_user_settings(uid, {"timezone": "Pacific/Auckland"}, db)
    config = MagicMock(database_path=db)
    seen: list[str] = []

    def fake_today(zone: str, now: datetime | None = None) -> date:
        seen.append(zone)
        return date(2026, 3, 3)

    monkeypatch.setattr(checkin, "local_today", fake_today)

    assert checkin._today(config, uid) == date(2026, 3, 3)
    assert checkin._today(config, None) == date(2026, 3, 3)
    assert seen == ["Pacific/Auckland", "UTC"]


# ---------------------------------------------------------------------------
# Jobs: the daily run and insight cron use the user's local day
# ---------------------------------------------------------------------------


def _job_config(db: str, **overrides: Any) -> Config:
    values: dict[str, Any] = {
        "hevy_api_key": None,
        "gemini_api_key": "synthetic-key",
        "gemini_model": "gemini-3.8-flash",
        "health_connect_file": None,
        "database_path": db,
        "hevy_sync_routines": False,
        "hevy_prefill_weights": False,
        "checkin_enabled": False,
        "lifestyle_enabled": False,
        "google_health_client_id": None,
        "google_health_client_secret": None,
        "google_health_refresh_token": None,
        "self_review_enabled": False,
        "self_review_weekday": 6,
    }
    values.update(overrides)
    return Config(**values)


def _daily_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    changes: dict[str, str],
) -> dict[str, Any]:
    from localisation import local_today as real_local_today

    db = str(tmp_path / "run.db")
    init_db(db)
    uid = get_or_create_user("run@example.test", None, db)["id"]
    if changes:
        update_user_settings(uid, changes, db)
    seen: dict[str, Any] = {}
    monkeypatch.setattr("main.Config.load", lambda: _job_config(db))
    monkeypatch.setattr("main.init_db", lambda *_a, **_k: None)
    monkeypatch.setattr("main._resolve_provider", lambda _c, **_k: MagicMock())
    monkeypatch.setattr("main.local_today", lambda zone, _now=None: real_local_today(zone, now))

    def fake_runtime(_uid: str, _db: str, *, today: date, **_k: Any) -> ProgrammeRuntime:
        seen["today"] = today
        return RUNTIME

    monkeypatch.setattr("main.load_programme_runtime", fake_runtime)
    monkeypatch.setattr(
        "main.generate_next_workout",
        lambda *_a, **kw: seen.update(kind="training", **kw) or "Planned session",
    )
    assert main.run(preview=True, user_id=uid) == 0
    return seen


def test_the_daily_run_dates_the_programme_in_the_users_local_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sunday 23:00 UTC is already Monday in Auckland, but still Sunday in UTC.
    sunday_night = _utc(2026, 8, 2, 23, 0)

    utc_user = _daily_run(tmp_path, monkeypatch, sunday_night, {})
    assert utc_user["today"] == date(2026, 8, 2)

    other = tmp_path / "auckland"
    other.mkdir()
    auckland = _daily_run(other, monkeypatch, sunday_night, {"timezone": "Pacific/Auckland"})
    assert auckland["today"] == date(2026, 8, 3)


def test_the_daily_run_hands_the_users_settings_to_prompt_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _daily_run(
        tmp_path,
        monkeypatch,
        _utc(2026, 8, 3, 12, 0),
        {"timezone": "America/Chicago", "locale": "en-US", "unit_system": "imperial"},
    )
    assert seen["kind"] == "training"
    settings = seen["settings"]
    assert (settings.timezone, settings.locale, settings.unit_system) == (
        "America/Chicago",
        "en-US",
        "imperial",
    )


def test_self_review_runs_on_the_users_local_sunday_in_their_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "review.db")
    init_db(db)
    uid = get_or_create_user("review@example.test", None, db)["id"]
    update_user_settings(uid, {"timezone": "Asia/Tokyo", "unit_system": "imperial"}, db)
    calls: list[str] = []
    review = MagicMock(lifts=[], headline="none yet")

    def fake_build(*_a: Any, unit_system: str = "metric", **_k: Any) -> Any:
        calls.append(unit_system)
        return review

    monkeypatch.setattr("main.insights_engine.build_insights", fake_build)
    config = _job_config(db, self_review_enabled=True)

    # Saturday evening in UTC, already Sunday morning in Tokyo.
    monkeypatch.setattr(
        "main.datetime", MagicMock(now=lambda tz=None: _utc(2026, 3, 7, 23, 0), spec=datetime)
    )
    main._maybe_self_review(config, None, RUNTIME, True, user_id=uid)
    assert calls == ["imperial"]

    # The same instant is not Sunday for a UTC user, so nothing is sent.
    utc_uid = get_or_create_user("utc-review@example.test", None, db)["id"]
    main._maybe_self_review(config, None, RUNTIME, True, user_id=utc_uid)
    assert calls == ["imperial"]


def test_insight_windows_are_counted_in_the_users_local_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        insight_cron, "datetime", MagicMock(now=lambda tz=None: _utc(2026, 3, 1, 23, 0), spec=datetime)
    )
    assert insight_cron._local_cutoff(UserSettings(timezone="UTC"), 7) == "2026-02-22"
    # 23:00Z on 1 March is already 2 March in Auckland, so the window moves with it.
    assert insight_cron._local_cutoff(UserSettings(timezone="Pacific/Auckland"), 7) == "2026-02-23"


def test_insight_prompts_include_regional_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "insight.db")
    init_db(db)
    uid = get_or_create_user("insight@example.test", None, db)["id"]
    update_user_settings(uid, {"locale": "en-AU", "unit_system": "imperial"}, db)
    provider = MagicMock()
    provider.generate.return_value = "not json"
    provider.name.return_value = "test"
    monkeypatch.setattr(insight_cron, "resolve_provider", lambda **_k: provider)
    config = _job_config(db)

    insight_cron.generate_daily_header(config, user_id=uid)
    insight_cron.generate_weekly_correlations(config, user_id=uid)

    daily, weekly = (call.args[0] for call in provider.generate.call_args_list)
    for prompt in (daily, weekly):
        assert "Locale: en-AU" in prompt and "Convert to pounds" in prompt
    assert "Write the text values in Australian English" in daily


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def test_regional_preferences_survive_export_sanitisation() -> None:
    block = export_preferences(
        UserSettings(timezone="Europe/London", unit_system="imperial", motion="reduce")
    )
    exported = sanitize_for_export(block)
    for field in ("timezone", "locale", "unit_system", "motion", "contrast", "text_size"):
        assert exported[field] == block[field]


def test_accessibility_choices_are_treated_as_health_adjacent() -> None:
    for field in ("motion", "contrast", "text_size"):
        assert classify_field(field) is DataClass.HEALTH
    for field in ("timezone", "locale", "unit_system", "units"):
        assert classify_field(field) is DataClass.IDENTIFIER


def test_health_export_carries_the_owners_preferences() -> None:
    preferences = export_preferences(UserSettings(timezone="Europe/London"))
    export = HealthExport(
        1, _utc(2026, 3, 2), 1, {"metrics": []}, preferences=preferences
    )
    export.validate(authenticated_user_id=1)
    assert export.preferences["weight_unit"] == "kg"


@pytest.mark.parametrize(
    "preferences",
    [
        {"timezone": "Mars/Base", "unit_system": "metric"},
        {"timezone": "UTC", "unit_system": "stone"},
        {"unit_system": "metric"},
    ],
)
def test_health_export_rejects_unusable_preferences(preferences: dict[str, str]) -> None:
    export = HealthExport(1, _utc(2026, 3, 2), 1, {}, preferences=preferences)
    with pytest.raises(ValueError):
        export.validate(authenticated_user_id=1)


def test_health_export_requires_utc_aware_timestamps() -> None:
    export = HealthExport(1, datetime(2026, 3, 2), 1, {})  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="timezone-aware"):
        export.validate(authenticated_user_id=1)


def test_health_export_without_preferences_is_still_valid() -> None:
    HealthExport(1, _utc(2026, 3, 2), 1, {}).validate(authenticated_user_id=1)
