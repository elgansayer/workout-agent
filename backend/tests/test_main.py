"""Tests for main.py: the daily agent entry-point orchestration."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from main import (
    _compose,
    _parse_args,
    _rep_targets_from_days,
    _scope_config_to_user,
    run,
)

if TYPE_CHECKING:
    from config import Config


def _active_programme(start: date) -> dict[str, Any]:
    return {
        "id": 1,
        "user_id": "test-user-id",
        "source": "hevy",
        "template_key": "hevy:test",
        "definition": {
            "name": "General strength · Hevy routines",
            "programme_spec": {
                "start_date": start.isoformat(),
                "duration_weeks": 12,
            },
            "blocks": [
                {
                    "number": 1,
                    "name": "Foundation",
                    "start_week": 1,
                    "end_week": 4,
                }
            ],
            "days": [
                {
                    "number": 1,
                    "focus": "Upper Strength",
                    "routine_id": "routine-1",
                    "exercises": [
                        {
                            "name": "Bench Press",
                            "template_id": "tmpl-bench",
                            "sets": 4,
                            "rep_range": "5-8",
                            "scheme": "4 × 5-8",
                        }
                    ],
                }
            ],
            "rules": ["Progress load safely."],
        },
        "active": True,
        "created_at": start.isoformat(),
        "updated_at": start.isoformat(),
    }


# ---------------------------------------------------------------------------
# _compose
# ---------------------------------------------------------------------------


def test_compose_with_guidance() -> None:
    from lifestyle import DailyGuidance

    guidance = DailyGuidance(
        training="Upper Strength",
        carb_tier="moderate",
        nutrition="Moderate carbs.",
        cardio="Walk.",
        recovery="Sleep well.",
        protein_target=None,
    )
    result = _compose("Workout plan here", guidance, "")
    assert result.startswith("Workout plan here")
    assert "Today's lifestyle:" in result


def test_compose_without_guidance() -> None:
    assert _compose("Plan", None, "") == "Plan"


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


def test_parse_args_default_no_preview() -> None:
    args = _parse_args([])
    assert args.preview is False


def test_parse_args_preview_flag() -> None:
    args = _parse_args(["--preview"])
    assert args.preview is True


def test_parse_args_unknown_raises() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--nonexistent"])


# ---------------------------------------------------------------------------
# _rep_targets_from_days
# ---------------------------------------------------------------------------


def test_rep_targets_from_days_uses_top_of_range() -> None:
    targets = _rep_targets_from_days(_active_programme(date(2026, 8, 3))["definition"]["days"])
    assert targets
    assert list(targets.values()) == [8]


# ---------------------------------------------------------------------------
# _scope_config_to_user
# ---------------------------------------------------------------------------


def test_scope_config_to_user_uses_only_that_users_connector_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from config import Config
    from cryptography.fernet import Fernet
    from database import (
        get_legacy_user_id,
        get_or_create_user,
        init_db,
        save_user_api_key,
    )

    db_path = str(tmp_path / "tenant-config.db")
    init_db(db_path)
    legacy_user_id = get_legacy_user_id(db_path)
    user = get_or_create_user(
        "synthetic-user@example.com",
        "Synthetic User",
        db_path,
    )
    user_without_key = get_or_create_user(
        "synthetic-no-key@example.com",
        "Synthetic User Without Key",
        db_path,
    )
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    save_user_api_key(
        user["id"],
        "hevy",
        "synthetic-user-hevy-key",
        db_path=db_path,
    )
    config = Config(
        hevy_api_key="synthetic-server-hevy-key",
        gemini_api_key=None,
        gemini_model="gemini-3.8-flash",
        health_connect_file="/synthetic/legacy-health.json",
        database_path=db_path,
        checkin_enabled=False,
        lifestyle_enabled=False,
        google_health_client_id="synthetic-client-id",
        google_health_client_secret="synthetic-client-secret",
        google_health_refresh_token="synthetic-legacy-refresh-token",
        self_review_enabled=False,
        self_review_weekday=6,
    )

    scoped = _scope_config_to_user(config, user["id"])
    assert scoped.hevy_api_key == "synthetic-user-hevy-key"
    assert scoped.health_connect_file is None
    assert scoped.google_health_client_id is None

    unconfigured = _scope_config_to_user(config, user_without_key["id"])
    assert unconfigured.hevy_api_key is None
    assert unconfigured.google_health_client_id is None

    legacy = _scope_config_to_user(config, legacy_user_id)
    assert legacy.hevy_api_key == "synthetic-server-hevy-key"
    assert legacy.health_connect_file == "/synthetic/legacy-health.json"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


_DEFAULT_ACTIVE = object()


def _make_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    checkin_enabled: bool = False,
    lifestyle_enabled: bool = False,
    self_review_enabled: bool = False,
    active: dict[str, Any] | None | object = _DEFAULT_ACTIVE,
) -> tuple[Config, dict[str, Any] | None, list[dict[str, Any]]]:
    """Patch Config.load and external boundaries for a controlled run."""
    from config import Config

    db_path = str(tmp_path / "test_main.db")

    config = Config(
        hevy_api_key="hevy-key",
        gemini_api_key="test-gemini-key",
        gemini_model="gemini-3.8-flash",
        health_connect_file=None,
        database_path=db_path,
        checkin_enabled=checkin_enabled,
        lifestyle_enabled=lifestyle_enabled,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=self_review_enabled,
        self_review_weekday=6,
    )
    resolved: Any = (
        _active_programme(date(2026, 8, 3))
        if active is _DEFAULT_ACTIVE
        else active
    )

    monkeypatch.setattr("main.Config.load", lambda: config)
    monkeypatch.setattr("main.init_db", lambda *args, **kw: None)
    monkeypatch.setattr("main._resolve_run_user", lambda path: "test-user-id")
    monkeypatch.setattr("main._scope_config_to_user", lambda config, user_id: config)
    monkeypatch.setattr(
        "main._resolve_provider",
        lambda config, **kw: MagicMock(name="test-provider"),
    )
    monkeypatch.setattr("main.local_today", lambda name, now=None: date(2026, 8, 3))
    monkeypatch.setattr(
        "main.get_user_by_id",
        lambda user_id, db_path: {"id": user_id, "timezone": "UTC"},
    )
    monkeypatch.setattr(
        "main.get_active_programme",
        lambda user_id, **kw: resolved,
    )
    monkeypatch.setattr("main.get_recent_hevy_logs", lambda **kw: [])

    notifications: list[dict[str, Any]] = []

    def _save_notification(user_id: str, **kwargs: Any) -> int:
        notifications.append({"user_id": user_id, **kwargs})
        return 1

    monkeypatch.setattr("main.save_notification", _save_notification)

    monkeypatch.setattr(
        "main.google_health_client.sync_body_metrics",
        lambda *args: None,
    )
    monkeypatch.setattr("main.read_recovery_metrics", lambda *args, **kw: None)
    monkeypatch.setattr("main.body_metrics_from_recovery", lambda rec: rec)
    monkeypatch.setattr("main.save_body_metrics", lambda *args, **kw: None)
    monkeypatch.setattr(
        "main.fetch_latest_workout",
        lambda key: {"id": "1", "title": "Upper Strength", "exercises": []},
    )
    from hevy_parser import WorkoutSummary

    monkeypatch.setattr(
        "main.parse_workout",
        lambda raw, targets: WorkoutSummary(
            title="Upper Strength",
            date="2026-08-02",
            duration_seconds=3600,
            total_volume_kg=2500,
            exercises=[],
        ),
    )
    monkeypatch.setattr("main.save_workout", lambda *args, **kw: None)
    monkeypatch.setattr("main.save_progress", lambda *args, **kw: None)
    monkeypatch.setattr("main.get_recent_bests", lambda *args, **kw: {})
    monkeypatch.setattr("main.get_progress_history", lambda **kw: {})
    monkeypatch.setattr("main.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("main.get_daily_logs", lambda **kw: [])
    monkeypatch.setattr("main.save_daily_log", lambda *a, **kw: None)

    from insights import LiftInsight, RecoveryInsight, TrainingInsights

    fake_insights = TrainingInsights(
        lifts=[
            LiftInsight(
                name="Bench Press",
                trend="progressing",
                metric="kg",
                change_pct=5.0,
                sessions=4,
                latest=100.0,
                best=100.0,
                slope_per_session=1.25,
                sessions_since_best=0,
                intervention=None,
            ),
        ],
        recovery=RecoveryInsight(
            sleep_hours=7.5,
            resting_hr=58,
            resting_hr_trend="steady",
            weight_kg=82.0,
            weight_trend="steady",
            body_fat_pct=None,
            body_fat_trend=None,
            muscle_pct=None,
            is_catabolic=False,
            status="good",
            directive="Stay the course.",
        ),
        headline="All lifts progressing.",
    )
    monkeypatch.setattr(
        "main.insights_engine.build_insights",
        lambda *a, **kw: fake_insights,
    )
    monkeypatch.setattr(
        "main.generate_next_workout",
        lambda *args, **kw: "Upper Strength - Week 1\nBench Press: 4 x 5-8",
    )
    return config, resolved, notifications


def test_run_preview_training_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path)
    result = run(preview=True)
    assert result == 0


def test_run_skips_without_active_programme(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, _active, notifications = _make_config(
        monkeypatch,
        tmp_path,
        active=None,
    )
    result = run(preview=True)
    assert result == 0
    assert notifications == []  # preview mode does not persist a skip


def test_run_records_setup_required_notification_without_programme(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, _active, notifications = _make_config(
        monkeypatch,
        tmp_path,
        active=None,
    )
    generated: list[Any] = []

    def _generate(*args: Any, **kwargs: Any) -> str:
        generated.append(True)
        return "should not run"

    monkeypatch.setattr("main.generate_next_workout", _generate)

    result = run(preview=False)
    assert result == 0
    assert generated == []
    assert any(item["title"] == "Programme setup required" for item in notifications)


def test_run_checkin_due_is_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path, checkin_enabled=True)

    import checkin

    monkeypatch.setattr(
        "main.checkin.due",
        lambda cfg, **kw: checkin.CheckinDue(
            number=1,
            workouts_done=24,
            weeks_elapsed=4,
            total_count=64,
        ),
    )
    monkeypatch.setattr(
        "main.checkin.run_checkin",
        lambda *args, **kw: "Check-in 1: all good.",
    )
    monkeypatch.setattr("main.checkin.record", lambda *a, **kw: None)

    import io
    import sys

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    result = run(preview=True)
    assert result == 0
    assert "Check-in 1" in captured.getvalue()


def test_run_checkin_due_failure_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path, checkin_enabled=True)
    monkeypatch.setattr(
        "main.checkin.due",
        MagicMock(side_effect=RuntimeError("DB error")),
    )
    result = run(preview=True)
    assert result == 0


def test_run_lifestyle_guidance_is_included(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path, lifestyle_enabled=True)

    import io
    import sys

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    result = run(preview=True)
    assert result == 0
    assert "Today's lifestyle:" in captured.getvalue()


def test_run_deliver_sends_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-vapid")

    push_calls: list[dict[str, Any]] = []

    def _fake_send(sub: dict, payload: dict, vapid_key: str) -> bool:
        push_calls.append(payload)
        return True

    monkeypatch.setattr(
        "main.get_push_subscriptions", lambda user, db: [{"endpoint": "test"}]
    )
    monkeypatch.setattr("main.send_push_notification", _fake_send)

    result = run(preview=False)
    assert result == 0
    assert len(push_calls) == 1
    assert "Upper Strength" in push_calls[0]["body"]


def test_run_deliver_returns_2_when_push_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-vapid")
    monkeypatch.setattr(
        "main.get_push_subscriptions", lambda user, db: [{"endpoint": "test"}]
    )
    monkeypatch.setattr("main.send_push_notification", lambda *a, **kw: False)

    result = run(preview=False)
    assert result == 2


def test_run_self_review_on_configured_weekday(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path, self_review_enabled=True)
    monkeypatch.setattr(
        "main.datetime",
        MagicMock(
            now=lambda tz=None: datetime(2026, 8, 9, 7, 0, tzinfo=timezone.utc),
            spec=datetime,
        ),
    )

    import io
    import sys

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    result = run(preview=True)
    assert result == 0
    assert "Weekly self-review" in captured.getvalue()


def test_run_config_error_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    from config import ConfigError

    monkeypatch.setattr(
        "main.Config.load",
        MagicMock(side_effect=ConfigError("Missing vars")),
    )
    result = run(preview=True)
    assert result == 1
