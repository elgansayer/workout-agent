"""Tests for main.py: the daily agent entry-point orchestration."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from main import (
    _compose,
    _parse_args,
    _scope_config_to_user,
    run,
)
from programme_runtime import ProgrammeRuntime

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
# Pure helpers
# ---------------------------------------------------------------------------


def test_compose_with_guidance() -> None:
    from lifestyle import DailyGuidance

    guidance = DailyGuidance(
        training="Legs and trunk",
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
    assert _compose("Rest day message", None, "") == "Rest day message"


def test_parse_args_default_no_preview() -> None:
    assert _parse_args([]).preview is False


def test_parse_args_preview_flag() -> None:
    assert _parse_args(["--preview"]).preview is True


def test_parse_args_unknown_raises() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--nonexistent"])


# ---------------------------------------------------------------------------
# _scope_config_to_user
# ---------------------------------------------------------------------------


def test_scope_config_to_user_uses_only_that_users_connector_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from config import Config
    from cryptography.fernet import Fernet
    from database import get_legacy_user_id, get_or_create_user, init_db
    from secret_vault import SecretVault

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
    SecretVault(db_path).store(
        user["id"],
        "hevy",
        api_key="synthetic-user-hevy-key",
    )
    config = Config(
        hevy_api_key="synthetic-server-hevy-key",
        gemini_api_key=None,
        gemini_model="gemini-3.8-flash",
        health_connect_file="/synthetic/legacy-health.json",
        database_path=db_path,
        hevy_sync_routines=False,
        hevy_prefill_weights=False,
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
    assert scoped.google_health_client_id == "synthetic-client-id"
    assert scoped.google_health_refresh_token is None

    unconfigured = _scope_config_to_user(config, user_without_key["id"])
    assert unconfigured.hevy_api_key is None
    assert unconfigured.google_health_refresh_token is None

    legacy = _scope_config_to_user(config, legacy_user_id)
    assert legacy.hevy_api_key == "synthetic-server-hevy-key"
    assert legacy.health_connect_file == "/synthetic/legacy-health.json"


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def _seed_run_user(monkeypatch: pytest.MonkeyPatch, db_path: str) -> str:
    from database import get_or_create_user, init_db

    init_db(db_path)
    user = get_or_create_user("synthetic-run@example.com", "Synthetic Runner", db_path)
    monkeypatch.setattr("main._resolve_run_user", lambda path: user["id"])
    return user["id"]


def _make_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    checkin_enabled: bool = False,
    lifestyle_enabled: bool = False,
) -> Config:
    from config import Config

    db_path = str(tmp_path / "test_main.db")
    config = Config(
        hevy_api_key="hevy-key",
        gemini_api_key="test-gemini-key",
        gemini_model="gemini-3.8-flash",
        health_connect_file=None,
        database_path=db_path,
        hevy_sync_routines=False,
        hevy_prefill_weights=False,
        checkin_enabled=checkin_enabled,
        lifestyle_enabled=lifestyle_enabled,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=False,
        self_review_weekday=6,
    )
    monkeypatch.setattr("main.Config.load", lambda: config)
    monkeypatch.setattr("main.init_db", lambda *args, **kw: None)
    _seed_run_user(monkeypatch, db_path)
    monkeypatch.setattr("main._scope_config_to_user", lambda config, user_id: config)
    monkeypatch.setattr("main.local_today", lambda *a, **kw: date(2026, 9, 10))
    return config


def _active_runtime() -> ProgrammeRuntime:
    day = {
        "number": 1,
        "routine_id": "routine-chest-back",
        "focus": "Chest and back",
        "exercises": [
            {
                "name": "Incline press",
                "template_id": "press",
                "sets": 3,
                "rep_range": "8-12",
                "scheme": "3 × 8-12",
                "role": "primary_strength",
                "prescriptions": [
                    {"block_number": 1, "sets": 3, "rep_range": "8-12"},
                ],
            }
        ],
    }
    return ProgrammeRuntime(
        status="active",
        message="Your workout for today.",
        name="Synthetic programme",
        goal="hypertrophy",
        source="hevy",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 10, 26),
        week=2,
        cycle_weeks=8,
        blocks=[
            {"number": 1, "name": "Foundation", "start_week": 1, "end_week": 4}
        ],
        block={"number": 1, "name": "Foundation", "start_week": 1, "end_week": 4},
        days=[day],
        next_day=day,
    )


def _patch_optional_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "main.google_health_client.sync_body_metrics",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("main.read_recovery_metrics", lambda *a, **kw: None)
    monkeypatch.setattr("main.body_metrics_from_recovery", lambda rec: rec)
    monkeypatch.setattr("main.save_body_metrics", lambda *a, **kw: None)
    monkeypatch.setattr("main.fetch_latest_workout", lambda key: None)
    monkeypatch.setattr("main.parse_workout", lambda raw, targets: None)
    monkeypatch.setattr("main.save_workout", lambda *a, **kw: None)
    monkeypatch.setattr("main.save_progress", lambda *a, **kw: None)
    monkeypatch.setattr("main.get_recent_bests", lambda *a, **kw: {})
    monkeypatch.setattr(
        "main.get_progress_history",
        lambda **kw: {},
    )
    monkeypatch.setattr("main.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("main.get_daily_logs", lambda **kw: [])
    monkeypatch.setattr("main.get_training_rules", lambda *a, **kw: None)
    from insights import RecoveryInsight, TrainingInsights

    monkeypatch.setattr(
        "main.insights_engine.build_insights",
        lambda *a, **kw: TrainingInsights(
            lifts=[],
            recovery=RecoveryInsight(
                sleep_hours=None,
                resting_hr=None,
                resting_hr_trend=None,
                weight_kg=None,
                weight_trend=None,
                body_fat_pct=None,
                body_fat_trend=None,
                muscle_pct=None,
                is_catabolic=False,
                status="unknown",
                directive="",
            ),
            headline="No data yet.",
        ),
    )


def test_run_without_programme_sends_setup_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "main.load_programme_runtime",
        lambda *a, **kw: ProgrammeRuntime(status="setup_required"),
    )
    saved: list[dict] = []
    delivered: list[dict] = []
    monkeypatch.setattr(
        "main.save_daily_log",
        lambda *a, **kw: saved.append({"args": a, "kwargs": kw}),
    )
    monkeypatch.setattr(
        "main._deliver",
        lambda *a, **kw: delivered.append({"args": a, "kwargs": kw}) or 0,
    )

    assert run(preview=False) == 0
    assert saved
    assert delivered
    assert "programme" in delivered[0]["kwargs"]["title"].lower()
    assert delivered[0]["kwargs"]["link"] == "/programmes"


def test_run_with_paused_programme_does_not_prescribe_a_workout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "main.load_programme_runtime",
        lambda *a, **kw: ProgrammeRuntime(
            status="paused",
            message="This programme is paused.",
        ),
    )
    delivered: list[dict] = []
    monkeypatch.setattr("main.save_daily_log", lambda *a, **kw: None)
    monkeypatch.setattr(
        "main._deliver",
        lambda *a, **kw: delivered.append({"args": a, "kwargs": kw}) or 0,
    )
    monkeypatch.setattr(
        "main.generate_next_workout",
        lambda *a, **kw: pytest.fail("must not generate a workout"),
    )
    monkeypatch.setattr("main.checkin.record", lambda *a, **kw: None)

    assert run(preview=False) == 0
    assert "paused" in delivered[0]["args"][1].lower()


def test_run_active_programme_uses_runtime_prescription(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path)
    runtime = _active_runtime()
    monkeypatch.setattr("main.load_programme_runtime", lambda *a, **kw: runtime)
    _patch_optional_io(monkeypatch)
    monkeypatch.setattr(
        "main._resolve_provider",
        lambda config, **kw: MagicMock(name="test-provider"),
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "main.generate_next_workout",
        lambda provider, **kw: (
            calls.append(kw)
            or "Chest and back - Week 2\nIncline press: 3 x 8-12"
        ),
    )
    saved: list[dict] = []
    monkeypatch.setattr(
        "main.save_daily_log",
        lambda *a, **kw: saved.append({"args": a, "kwargs": kw}),
    )
    monkeypatch.setattr("main._deliver", lambda *a, **kw: 0)

    assert run(preview=False) == 0
    assert calls
    assert calls[0]["runtime"] is runtime
    assert calls[0]["day"] is runtime.next_day
    assert saved
    assert saved[0]["args"][2] == "Chest and back"


def test_run_active_programme_degrades_to_deterministic_plan_without_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_config(monkeypatch, tmp_path)
    runtime = _active_runtime()
    monkeypatch.setattr("main.load_programme_runtime", lambda *a, **kw: runtime)
    _patch_optional_io(monkeypatch)
    monkeypatch.setattr(
        "main._resolve_provider",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("no provider")),
    )
    monkeypatch.setattr(
        "main.generate_next_workout",
        lambda *a, **kw: pytest.fail("must not call the explainer without a provider"),
    )
    delivered: list[dict] = []
    monkeypatch.setattr("main.save_daily_log", lambda *a, **kw: None)
    monkeypatch.setattr(
        "main._deliver",
        lambda *a, **kw: delivered.append({"args": a, "kwargs": kw}) or 0,
    )

    assert run(preview=False) == 0
    assert "Incline press: 3 x 8-12" in delivered[0]["args"][1].replace("×", "x")


def test_run_config_error_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    from config import ConfigError

    monkeypatch.setattr(
        "main.Config.load",
        lambda: (_ for _ in ()).throw(ConfigError("missing config")),
    )
    assert run(preview=True) == 1
