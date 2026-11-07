from __future__ import annotations

import base64
import importlib
import json
from pathlib import Path
from typing import Any

import database
import pytest
from onboarding import build_onboarding_state

_SECRET = "onboarding-test-secret-that-is-long-enough"


def _db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "onboarding.db")
    database.init_db(db_path)
    return db_path


def _state(db_path: str, user_id: str) -> dict[str, Any]:
    return build_onboarding_state(user_id, db_path)


def test_fresh_user_gets_a_resumable_sequence_and_next_action(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    user_id = database.get_legacy_user_id(db_path)

    state = _state(db_path, user_id)

    assert [step["id"] for step in state["steps"]] == [
        "account",
        "hevy",
        "health",
        "ai",
        "programme",
        "notifications",
    ]
    assert state["ready"] is False
    assert state["next_required_action"]["id"] == "account"
    assert state["required_completed"] == 0
    assert state["resolved_count"] == 0
    health = next(step for step in state["steps"] if step["id"] == "health")
    assert health["required"] is False
    assert "recovery" in health["consequence"].lower()


def test_progress_is_derived_from_verified_server_state(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    user_id = database.get_legacy_user_id(db_path)

    database.save_user_preferences(
        user_id,
        goals=["strength"],
        db_path=db_path,
    )
    database.save_user_api_key(user_id, "hevy", "synthetic-hevy-key", db_path=db_path)
    database.set_active_programme(
        user_id,
        "hevy",
        "hevy:fixture",
        {"name": "Fixture programme"},
        db_path=db_path,
    )

    state = _state(db_path, user_id)

    by_id = {step["id"]: step for step in state["steps"]}
    assert by_id["account"]["status"] == "complete"
    assert by_id["hevy"]["status"] == "complete"
    assert by_id["programme"]["status"] == "complete"
    assert state["ready"] is True
    assert state["next_required_action"] is None
    assert state["complete"] is False


def test_optional_step_skip_is_explained_and_superseded_by_real_state(
    tmp_path: Path,
) -> None:
    db_path = _db(tmp_path)
    user_id = database.get_legacy_user_id(db_path)

    database.skip_onboarding_step(user_id, "notifications", db_path=db_path)
    health = next(
        step for step in _state(db_path, user_id)["steps"] if step["id"] == "notifications"
    )
    assert health["status"] == "skipped"
    assert health["skipped"] is True
    assert health["resolved"] is True

    database.save_push_subscription(
        user_id,
        endpoint="https://push.example.test/subscription",
        p256dh="synthetic-p256dh",
        auth="synthetic-auth",
        db_path=db_path,
    )
    health = next(
        step for step in _state(db_path, user_id)["steps"] if step["id"] == "notifications"
    )
    assert health["status"] == "complete"
    assert health["skipped"] is False


def test_required_steps_cannot_be_skipped_through_the_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("authlib")
    pytest.importorskip("httpx")

    db_path = _db(tmp_path)
    user_id = database.get_legacy_user_id(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "onboarding-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "onboarding-client-secret")

    import webapp.app as app_module

    importlib.reload(app_module)

    from starlette.testclient import TestClient

    payload = base64.b64encode(
        json.dumps({"user": "athlete@example.test", "user_id": user_id}).encode()
    )
    from itsdangerous import TimestampSigner

    cookie = TimestampSigner(_SECRET).sign(payload).decode()
    client = TestClient(app_module.app)
    client.cookies.set("session", cookie)

    state_response = client.get("/api/onboarding")
    assert state_response.status_code == 200
    assert state_response.json()["next_required_action"]["id"] == "account"

    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["onboarding"]["next_required_action"]["id"] == "account"

    rejected = client.post("/api/onboarding/skip", json={"step": "hevy"})
    assert rejected.status_code == 400

    accepted = client.post("/api/onboarding/skip", json={"step": "health"})
    assert accepted.status_code == 200
    health = next(
        step for step in accepted.json()["steps"] if step["id"] == "health"
    )
    assert health["status"] == "skipped"


def test_onboarding_progress_is_tenant_scoped(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    first = database.get_legacy_user_id(db_path)
    second = database.get_or_create_user("second@example.test", db_path=db_path)

    database.skip_onboarding_step(first, "health", db_path=db_path)

    first_state = _state(db_path, first)
    second_state = _state(db_path, second["id"])

    assert next(
        step for step in first_state["steps"] if step["id"] == "health"
    )["status"] == "skipped"
    assert next(
        step for step in second_state["steps"] if step["id"] == "health"
    )["status"] == "pending"
