"""Route-level readiness and capability honesty tests."""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

import database
from ai_resolver import ProviderInspection
from fastapi.testclient import TestClient
from hevy_parser import ExerciseSummary, WorkoutSummary


def _app(tmp_path: Any, monkeypatch: Any) -> Any:
    db_path = str(tmp_path / "readiness.db")
    database.init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.delenv("WEB_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("WEB_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_SECRET", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "ALLOW_ANONYMOUS_WEB", True)
    return app_module, db_path


def _client(app_module: Any) -> TestClient:
    return TestClient(app_module.app)


def _available_provider() -> ProviderInspection:
    return ProviderInspection("gemini", True, True)


def _unavailable_provider(reason: str) -> ProviderInspection:
    return ProviderInspection("gemini", True, False, reason)


def _save_hevy_key(db_path: str) -> str:
    user_id = database.get_legacy_user_id(db_path)
    database.save_user_api_key(user_id, "hevy", "synthetic-hevy-key", db_path=db_path)
    return user_id


def _save_training_data(db_path: str, user_id: str) -> None:
    today = datetime.now(tz=timezone.utc).date().isoformat()
    database.save_progress(
        WorkoutSummary(
            title="Day 1",
            date=today,
            duration_seconds=3600,
            total_volume_kg=2800.0,
            exercises=[ExerciseSummary("Deadlift (Barbell)", 140.0, 5, 4)],
        ),
        db_path,
        user_id=user_id,
    )
    database.save_daily_log(
        today,
        1,
        "Back, Deadlifts & Chest",
        "high",
        "plan",
        "life",
        db_path,
        user_id=user_id,
    )


def test_status_reports_setup_required_for_fresh_account(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, _db_path = _app(tmp_path, monkeypatch)
    with _client(app_module) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "setup_required"
    assert payload["data"]["available"] is False
    assert payload["provider"]["available"] is False
    assert payload["coach"]["grounded"] is False


def test_status_reports_ready_only_with_fresh_data_and_provider(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    user_id = _save_hevy_key(db_path)
    _save_training_data(db_path, user_id)
    monkeypatch.setattr(app_module.ai_resolver, "inspect_provider", lambda *a, **kw: _available_provider())

    with _client(app_module) as client:
        response = client.get("/api/status")

    payload = response.json()
    assert payload["phase"] == "ready"
    assert payload["data"]["available"] is True
    assert payload["provider"]["verified"] is True
    assert payload["coach"]["grounded"] is True


def test_data_without_provider_is_not_grounded(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    user_id = _save_hevy_key(db_path)
    _save_training_data(db_path, user_id)
    monkeypatch.setattr(
        app_module.ai_resolver,
        "inspect_provider",
        lambda *a, **kw: ProviderInspection(
            "gemini", False, False, "No gemini key configured."
        ),
    )

    with _client(app_module) as client:
        payload = client.get("/api/status").json()

    assert payload["phase"] == "setup_required"
    assert payload["data"]["available"] is True
    assert payload["coach"]["grounded"] is False


def test_provider_failure_is_degraded(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    user_id = _save_hevy_key(db_path)
    _save_training_data(db_path, user_id)
    monkeypatch.setattr(
        app_module.ai_resolver,
        "inspect_provider",
        lambda *a, **kw: _unavailable_provider("The gemini provider SDK is unavailable."),
    )

    with _client(app_module) as client:
        payload = client.get("/api/status").json()

    assert payload["phase"] == "degraded"
    assert "unavailable" in (payload["provider"]["reason"] or "").lower()


def test_every_personal_surface_exposes_the_same_readiness(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    user_id = database.get_legacy_user_id(db_path)
    _save_training_data(db_path, user_id)
    monkeypatch.setattr(app_module.ai_resolver, "inspect_provider", lambda *a, **kw: _available_provider())

    with _client(app_module) as client:
        status = client.get("/api/status").json()
        assert status["phase"] == "stale"
        for path in (
            "/api/dashboard",
            "/api/plan",
            "/api/progress",
            "/api/stats",
            "/api/history",
            "/api/checkins",
            "/api/settings",
            "/api/programmes",
            "/api/chat/history",
        ):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.json()["readiness"] == status, path


def test_grounded_reasoning_refuses_when_no_training_data(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, _db_path = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module.ai_resolver, "inspect_provider", lambda *a, **kw: _available_provider())

    with _client(app_module) as client:
        response = client.get("/api/xai_reasoning/2026-11-08_Deadlift")

    assert response.status_code == 409
    assert "training data" in response.json()["detail"].lower()


def test_chat_history_keeps_messages_and_reports_readiness(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, _db_path = _app(tmp_path, monkeypatch)

    with _client(app_module) as client:
        payload = client.get("/api/chat/history").json()

    assert payload["messages"] == []
    assert payload["readiness"]["phase"] == "setup_required"


def test_coach_prompt_does_not_claim_data_when_none_exists(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, _db_path = _app(tmp_path, monkeypatch)
    prompts: list[str] = []

    class _FakeProvider:
        def generate(self, prompt: str, *, stream: bool = False) -> Any:
            prompts.append(prompt)
            return iter(["general answer"])

    monkeypatch.setattr(
        app_module,
        "resolve_provider",
        lambda *a, **kw: _FakeProvider(),
    )
    monkeypatch.setattr(app_module.ai_resolver, "inspect_provider", lambda *a, **kw: _available_provider())

    with _client(app_module) as client:
        response = client.get("/api/rag_search?q=How+is+my+deadlift+progressing")

    assert response.status_code == 200
    assert "do NOT have access" in prompts[0]
    assert "You have access to this user's training logs" not in prompts[0]
