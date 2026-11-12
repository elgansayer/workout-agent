"""Smoke tests for the internal web app.

Skipped automatically when FastAPI is not installed (it lives in
requirements-web.txt, separate from the agent's core dependencies).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from collections.abc import Generator
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from database import (
    init_db,
    save_body_metrics,
    save_checkin,
    save_daily_log,
    save_progress,
)
from fastapi.testclient import TestClient
from hevy_parser import ExerciseSummary, WorkoutSummary


@pytest.fixture()
def client(tmp_path: Any, monkeypatch: Any) -> Generator[Any, None, None]:
    db_path = str(tmp_path / "web.db")
    init_db(db_path)
    save_checkin(1, "2026-03-01", 24, 4, "Check-in 1: solid block.", db_path)
    save_body_metrics({"weight_kg": 82.0, "body_fat_pct": 16.0}, "2026-03-01", db_path)
    save_body_metrics({"weight_kg": 81.4, "body_fat_pct": 15.6}, "2026-03-08", db_path)
    save_daily_log(
        "2026-03-02",
        1,
        "Back, Deadlifts & Chest",
        "high",
        "plan",
        "life",
        db_path,
    )
    summary = WorkoutSummary(
        title="Day 1",
        date="2026-03-02",
        duration_seconds=3600,
        total_volume_kg=2800.0,
        exercises=[ExerciseSummary("Deadlift (Barbell)", 140.0, 5, 4)],
    )
    save_progress(summary, db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    # Isolate from any real .env (loaded by config.py via load_dotenv) so the
    # dashboard starts in a known "Google Health not configured" state.
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_SECRET", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# AI Provider wiring tests - verify the chat/RAG/XAI endpoints resolve the
# user's preferred AI provider rather than hardcoding Gemini.
# ---------------------------------------------------------------------------


def test_xai_reasoning_uses_resolve_provider(client: Any, monkeypatch: Any) -> None:
    """The XAI endpoint resolves through the canonical user/provider boundary."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gem-key")

    captured: list[dict] = []
    saved_prompts: list[str] = []

    class _FakeProvider:
        def generate(self, prompt: Any, *, stream: bool = False) -> Any:
            saved_prompts.append(prompt)
            return "Causal explanation from fake provider."

    def _fake_resolve(
        user_id: Any = None,
        *,
        server_gemini_key: Any = None,
        server_gemini_model: Any = None,
        db_path: str = "workout_agent.db",
    ) -> Any:
        captured.append(
            {
                "user_id": user_id,
                "server_gemini_key": server_gemini_key,
                "server_gemini_model": server_gemini_model,
                "db_path": db_path,
            },
        )
        return _FakeProvider()

    monkeypatch.setattr("webapp.app.resolve_provider", _fake_resolve)
    monkeypatch.setattr(
        "webapp.app.save_reasoning_log",
        lambda *a, **kw: None,
    )
    from config import Config

    monkeypatch.setattr("webapp.app.get_config", lambda: Config.load())

    response = client.get("/api/xai_reasoning/2026-03-02_Deadlift (Barbell)")
    assert response.status_code == 200
    data = response.json()
    assert "reasoning" in data
    assert len(captured) == 1
    assert captured[0]["server_gemini_key"] == "test-gem-key"


def test_project_peak_uses_resolve_provider(client: Any, monkeypatch: Any) -> None:
    """The project_peak endpoint resolves through the canonical boundary."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gem-key")

    captured: list[dict] = []

    class _FakeProvider:
        def generate(self, prompt: Any, *, stream: bool = False) -> Any:
            return '{"Deadlift_Projected": 200, "Pullups_Projected": 25, "Validation": "ok"}'

    def _fake_resolve(
        user_id: Any = None,
        *,
        server_gemini_key: Any = None,
        server_gemini_model: Any = None,
        db_path: str = "workout_agent.db",
    ) -> Any:
        captured.append(
            {
                "user_id": user_id,
                "server_gemini_key": server_gemini_key,
            },
        )
        return _FakeProvider()

    monkeypatch.setattr("webapp.app.resolve_provider", _fake_resolve)
    from config import Config

    monkeypatch.setattr("webapp.app.get_config", lambda: Config.load())

    response = client.get("/api/project_peak")
    assert response.status_code == 200
    assert len(captured) == 1


def test_rag_search_uses_resolve_provider(client: Any, monkeypatch: Any) -> None:
    """The RAG endpoint resolves and streams the provider-neutral contract."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gem-key")

    captured: list[dict] = []

    class _FakeProvider:
        def generate(self, prompt: Any, *, stream: bool = False) -> Any:
            if stream:
                return iter(["Response from fake provider."])
            return "Response from fake provider."

    def _fake_resolve(
        user_id: Any = None,
        *,
        server_gemini_key: Any = None,
        server_gemini_model: Any = None,
        db_path: str = "workout_agent.db",
    ) -> Any:
        captured.append(
            {
                "user_id": user_id,
                "server_gemini_key": server_gemini_key,
            },
        )
        return _FakeProvider()

    monkeypatch.setattr("webapp.app.resolve_provider", _fake_resolve)
    from config import Config

    monkeypatch.setattr("webapp.app.get_config", lambda: Config.load())

    response = client.get("/api/rag_search?q=How+is+my+deadlift+progressing")
    assert response.status_code == 200
    assert len(captured) == 1


def test_rag_search_rate_limited(client: Any, monkeypatch: Any) -> None:
    """Repeated RAG search requests hit the rate limiter."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gem-key")

    class _StubProvider:
        def generate(self, prompt: Any, *, stream: bool = False) -> Any:
            return "ok"

    monkeypatch.setattr(
        "webapp.app.resolve_provider",
        lambda user_id=None, **kw: _StubProvider(),
    )
    from config import Config

    monkeypatch.setattr("webapp.app.get_config", lambda: Config.load())

    for i in range(20):
        client.get(f"/api/rag_search?q=test+{i}")

    response = client.get("/api/rag_search?q=final-test")
    assert response.status_code == 429


def test_xai_reasoning_invalid_context(client: Any) -> None:
    """An invalid context ID returns a graceful error, not a stack trace."""
    response = client.get("/api/xai_reasoning/nounderscore")
    assert response.status_code == 200
    assert response.json() == {"reasoning": "Invalid context ID"}


def test_settings_provider_catalog_matches_registry(client: Any) -> None:
    """Settings renders the same providers and defaults as the backend registry."""
    from ai_provider import available_providers

    response = client.get("/api/settings")

    assert response.status_code == 200
    payload = response.json()
    expected = available_providers()
    assert payload["ai_providers"] == expected
    assert set(payload["user_keys"]) == {
        "hevy",
        *(provider["id"] for provider in expected),
    }


def test_selecting_provider_without_override_uses_registry_default(
    client: Any,
) -> None:
    """Changing providers cannot retain an incompatible model from the old one."""
    import webapp.app as app_module
    from ai_provider import PROVIDERS
    from database import (
        get_legacy_user_id,
        get_user_preferences,
        save_user_preferences,
    )
    from starlette.requests import Request

    user_id = get_legacy_user_id(app_module.DB_PATH)
    save_user_preferences(
        user_id,
        preferred_ai="openai",
        ai_model="gpt-old-provider",
        db_path=app_module.DB_PATH,
    )
    body = json.dumps({"preferred_ai": "claude"}).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/settings/preferences",
            "headers": [],
            "query_string": b"",
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "session": {"user_id": user_id},
        },
        receive,
    )
    result = asyncio.run(app_module.save_preferences(request))

    assert result == {"status": "ok"}
    preferences = get_user_preferences(user_id, app_module.DB_PATH)
    assert preferences["preferred_ai"] == "claude"
    assert preferences["ai_model"] == PROVIDERS["claude"]["default_model"]


def test_rag_search_uses_selected_encrypted_claude_key(
    client: Any,
    monkeypatch: Any,
) -> None:
    """The real DB-to-resolver-to-stream path honours a user's Claude choice."""
    import ai_provider
    import webapp.app as app_module
    from cryptography.fernet import Fernet
    from database import get_legacy_user_id, save_user_preferences
    from secret_vault import SecretVault

    db_path = app_module.DB_PATH
    user_id = get_legacy_user_id(db_path)
    synthetic_key = "synthetic-claude-key"
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    save_user_preferences(
        user_id,
        preferred_ai="claude",
        ai_model="claude-synthetic-model",
        db_path=db_path,
    )
    SecretVault(db_path).store(
        user_id,
        "claude",
        api_key=synthetic_key,
        model="stale-model-metadata",
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT api_key FROM user_api_keys WHERE user_id = ? AND provider = ?",
            (user_id, "claude"),
        ).fetchone()
    assert row is not None
    assert row[0] != synthetic_key

    constructed: list[tuple[str, str]] = []
    scoped_users: list[str | None] = []

    class _FakeClaude:
        def generate(self, prompt: str, *, stream: bool = False) -> Any:
            assert stream is True
            assert "synthetic question" in prompt
            return iter(["Claude ", "response"])

    def _build_claude(*, api_key: str, model: str) -> _FakeClaude:
        constructed.append((api_key, model))
        return _FakeClaude()

    monkeypatch.setitem(
        ai_provider.PROVIDERS,
        "claude",
        {
            "class": _build_claude,
            "default_model": "claude-sonnet-5-5",
        },
    )

    def _personal_records(*, db_path: str, user_id: str | None = None) -> list[Any]:
        scoped_users.append(user_id)
        return []

    monkeypatch.setattr(app_module, "get_personal_records", _personal_records)

    response = client.get("/api/rag_search?q=synthetic+question")

    assert response.status_code == 200
    assert response.text == "Claude response"
    assert constructed == [(synthetic_key, "claude-synthetic-model")]
    assert scoped_users == [user_id]


# ---------------------------------------------------------------------------
# sync_history tests
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Angular migration")
def test_sync_history_requires_auth(client: Any) -> None:
    """POST /api/settings/sync-history without a session returns 401."""
    resp = client.post("/api/settings/sync-history")
    assert resp.status_code == 401


@pytest.mark.skip(reason="Angular migration")
def test_sync_history_requires_hevy_key(monkeypatch: Any, tmp_path: Any) -> None:
    """sync_history.sync_all returns an error string when no key is provided."""
    from sync_history import sync_all

    result = sync_all("", str(tmp_path / "test.db"))
    assert "error" in result
    error_msg = result["error"]
    assert isinstance(error_msg, str)
    assert "Hevy API key" in error_msg


@pytest.mark.skip(reason="Angular migration")
def test_sync_history_no_workouts(monkeypatch: Any, tmp_path: Any) -> None:
    """When Hevy returns no workouts, sync_all returns zero counts."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gem-key")

    def _fake_get_all(api_key: Any) -> Any:
        return []

    monkeypatch.setattr("sync_history.get_all_workouts", _fake_get_all)

    from sync_history import sync_all

    result = sync_all("fake-hevy-key", str(tmp_path / "test.db"))
    assert result["workouts_found"] == 0
    assert result["processed"] == 0


# -- auth / login / logout ----------------------------------------------


@pytest.mark.skip(reason="Angular migration")
def test_login_unconfigured_returns_500(client: Any) -> None:
    """Without WEB_GOOGLE_CLIENT_ID, /login returns 500."""
    resp = client.get("/login")
    assert resp.status_code == 500
    assert "not configured" in resp.text


@pytest.mark.skip(reason="Angular migration")
def test_login_google_unconfigured_returns_500(client: Any) -> None:
    """Without WEB_GOOGLE_CLIENT_ID, /login/google returns 500."""
    resp = client.get("/login/google")
    assert resp.status_code == 500
    assert "not configured" in resp.text


@pytest.mark.skip(reason="Angular migration")
def test_logout_clears_session_and_redirects(client: Any) -> None:
    """Logout clears the session and redirects to /login."""
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/login"


# -- chat routes --------------------------------------------------------


@pytest.mark.skip(reason="Angular migration")
def test_chat_page_renders(client: Any) -> None:
    """GET /chat returns the chat page (no auth required in test env)."""
    resp = client.get("/chat")
    assert resp.status_code == 200
    assert "chat" in resp.text.lower()


@pytest.mark.skip(reason="Angular migration")
def test_chat_history_returns_empty_list(client: Any) -> None:
    """GET /api/chat/history returns an empty list for a fresh DB."""
    resp = client.get("/api/chat/history")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.skip(reason="Angular migration")
def test_chat_clear_returns_ok(client: Any) -> None:
    """POST /api/chat/clear returns status ok."""
    resp = client.post("/api/chat/clear")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_coach_capabilities_endpoint(client: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    resp = client.get("/api/coach/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider_ready"] is True
    assert "capabilities" in data
    assert "suggested_prompts" in data
    assert data["overall_status"] in {"educational_only", "partially_grounded", "fully_grounded"}

    # Also check /api/chat/capabilities alias
    alias_resp = client.get("/api/chat/capabilities")
    assert alias_resp.status_code == 200


def test_rag_search_disables_data_grounded_prompts_when_prerequisites_fail(
    client: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    provider_called: list[Any] = []

    class _MockProvider:
        def generate(self, prompt: Any, *, stream: bool = False) -> Any:
            provider_called.append(prompt)
            return iter(["Hallucinated deadlift progress"])

    monkeypatch.setattr("webapp.app.resolve_provider", lambda **kw: _MockProvider())

    resp = client.get("/api/rag_search?q=How+is+my+deadlift+progressing%3F")
    assert resp.status_code == 200
    assert "Data-Grounded Coaching Unavailable" in resp.text
    assert ("No workout logs found" in resp.text or "stale" in resp.text.lower())
    # Provider must NOT have been called to invent data
    assert len(provider_called) == 0


def test_rag_search_allows_generic_educational_responses_without_inventing_history(
    client: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    prompts_captured: list[Any] = []

    class _MockProvider:
        def generate(self, prompt: Any, *, stream: bool = False) -> Any:
            prompts_captured.append(prompt)
            return iter(["**Educational Guidance** (General advice)\n\nProgressive overload is..."])

    monkeypatch.setattr("webapp.app.resolve_provider", lambda **kw: _MockProvider())

    resp = client.get("/api/rag_search?q=What+is+progressive+overload%3F")
    assert resp.status_code == 200
    assert "Educational Guidance" in resp.text
    assert len(prompts_captured) == 1
    assert "Do NOT fabricate, assume, or invent any user-specific training history" in prompts_captured[0]
    assert "verified training data context" not in prompts_captured[0].lower()


def test_rag_search_allows_personalised_analysis_when_prerequisites_pass(
    client: Any, monkeypatch: Any
) -> None:
    from datetime import datetime, timezone

    import webapp.app as app_module
    from database import save_daily_log

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    prompts_captured: list[Any] = []

    today = datetime.now(timezone.utc).date().isoformat()
    save_daily_log(
        today,
        1,
        "Deadlift day",
        "medium",
        "Deadlift 5x5 at 180kg",
        "Good sleep",
        db_path=app_module.DB_PATH,
    )

    class _MockProvider:
        def generate(self, prompt: Any, *, stream: bool = False) -> Any:
            prompts_captured.append(prompt)
            return iter(["**Personalised Analysis** (Grounded)\n\nYour deadlift is moving well."])

    monkeypatch.setattr("webapp.app.resolve_provider", lambda **kw: _MockProvider())

    resp = client.get("/api/rag_search?q=How+is+my+deadlift+progressing%3F")
    assert resp.status_code == 200
    assert "Personalised Analysis" in resp.text
    assert len(prompts_captured) == 1
    assert "Personalised Analysis" in prompts_captured[0]


def test_coach_capability_is_derived_from_tenant_records(
    client: Any, monkeypatch: Any
) -> None:
    """The capability endpoint reports the seeded user's real source state."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    resp = client.get("/api/coach/capability")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["provider_configured"] is False
    assert payload["provider_state"] == "missing_key"
    assert payload["can_chat"] is False
    assert payload["has_training_data"] is True
    assert payload["sources"]["training_logs"]["available"] is True
    assert payload["sources"]["progress_history"]["available"] is True
    assert payload["sources"]["body_metrics"]["available"] is True
    assert payload["status"] == "provider_missing"
    assert "api_key" not in resp.text.lower()
    assert "full access" not in resp.text.lower()


def test_coach_never_claims_a_working_provider_for_a_configured_one(
    client: Any, monkeypatch: Any
) -> None:
    """A server fallback key is configuration, not proof that a provider works."""
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-server-fallback")

    payload = client.get("/api/coach/capability").json()

    assert payload["provider_configured"] is True
    assert payload["provider_state"] == "unverified"
    assert payload["provider_verified"] is False
    assert payload["provider_source"] == "server"
    assert payload["can_chat"] is True
    assert payload["has_training_data"] is True
    # Whichever other fact applies to this fixture, "ready" needs a verified
    # provider and current data, and an untested key is never "ready".
    assert payload["status"] in {"provider_unverified", "stale_data", "degraded"}
    assert payload["status"] != "ready"
    assert payload["readiness"] in {"stale", "degraded", "ready", "setup_required"}


def test_coach_capability_is_isolated_between_users(client: Any) -> None:
    import webapp.app as app_module
    from database import get_legacy_user_id, get_or_create_user

    db_path = app_module.DB_PATH
    populated_user = get_legacy_user_id(db_path)
    empty_user = get_or_create_user("other@example.test", db_path=db_path)["id"]

    populated = app_module._coach_capability_for_user(populated_user)
    empty = app_module._coach_capability_for_user(empty_user)

    assert populated.has_training_data is True
    assert empty.has_training_data is False
    assert all(not source.available for source in empty.sources.values())


def test_coach_capability_claims_no_data_for_an_empty_tenant(
    tmp_path: Any, monkeypatch: Any
) -> None:
    db_path = str(tmp_path / "empty.db")
    init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import webapp.app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as test_client:
        payload = test_client.get("/api/coach/capability").json()

    assert payload["status"] == "unconfigured"
    assert payload["provider_configured"] is False
    assert payload["has_training_data"] is False
    assert payload["has_fresh_training_data"] is False
    assert payload["programme_available"] is False
    assert all(
        not source["available"] for source in payload["sources"].values()
    )


def test_rag_prompt_only_claims_server_verified_sources(
    client: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-server-fallback")
    prompts: list[str] = []

    class _FakeProvider:
        def generate(self, prompt: Any, *, stream: bool = False) -> Any:
            prompts.append(prompt)
            return iter(["ok"]) if stream else "ok"

    monkeypatch.setattr(
        "webapp.app.resolve_provider", lambda user_id=None, **kw: _FakeProvider()
    )

    resp = client.get("/api/rag_search?q=How+is+my+training+going")

    assert resp.status_code == 200
    assert prompts
    assert "Server-verified data access for this user" in prompts[0]
    assert "You have access to this user's" not in prompts[0]


# -- settings API-key CRUD ---------------------------------------------


def _make_auth_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    user_id: str,
) -> Any:
    from starlette.requests import Request

    raw_body = json.dumps(body or {}).encode() if body is not None else b""

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "session": {"user_id": user_id},
        },
        receive,
    )


def _call_json_route(
    app_module: Any,
    handler: Any,
    payload: dict[str, Any],
    user_id: str,
) -> Any:
    import asyncio

    app_module._RATE_LIMITS.clear()
    request = _make_auth_request(
        "POST",
        f"/api/settings/{handler.__name__}",
        body=payload,
        user_id=user_id,
    )
    return asyncio.run(handler(request))


def test_structured_rules_crud_and_tenant_isolation(client: Any) -> None:
    """Rules support all 8 categories, are validated, versioned, and tenant-isolated."""
    import webapp.app as app_module
    from database import get_legacy_user_id, get_or_create_user
    from fastapi import HTTPException

    user_a = get_legacy_user_id(app_module.DB_PATH)
    user_b_row = get_or_create_user("user_b@example.com", db_path=app_module.DB_PATH)
    user_b = user_b_row["id"] if isinstance(user_b_row, dict) else user_b_row.id

    # 1. GET /api/settings/rules for User A returns defaults
    request_a = _make_auth_request("GET", "/api/settings/rules", user_id=user_a)
    resp_a = app_module.get_user_training_rules(request_a)
    rules_a = json.loads(resp_a.body.decode())
    assert rules_a["version"] == 1
    assert rules_a["prohibited_exercises"] == []

    # 2. POST /api/settings/rules saves and increments version
    valid_payload = {
        "version": 1,
        "prohibited_exercises": [
            {"exercise": "Behind The Neck Press", "reason": "Shoulder impingement"}
        ],
        "substitutions": [
            {
                "from_exercise": "Barbell Back Squat",
                "to_exercise": "Leg Press",
                "reason": "Knee strain",
            }
        ],
        "rep_intensity_bounds": {
            "min_reps": 6,
            "max_reps": 12,
            "max_rpe": 8.5,
            "min_rir": 1.5,
        },
        "frequency": {
            "max_sessions_per_week": 4,
            "min_rest_days_between_sessions": 1,
        },
        "volume": {"max_sets_per_workout": 16, "max_sets_per_exercise": 4},
        "equipment": {
            "available_equipment": ["barbell", "dumbbells"],
            "prohibited_equipment": ["smith_machine"],
        },
        "recovery": {
            "min_sleep_hours": 7.5,
            "min_readiness_score": 65,
            "auto_deload_on_fatigue": True,
        },
        "contextual_notes": [
            {
                "category": "joint",
                "note": "Right shoulder sensitivity",
                "severity": "warning",
            }
        ],
    }
    request_save_a = _make_auth_request(
        "POST", "/api/settings/rules", body=valid_payload, user_id=user_a
    )
    save_resp = asyncio.run(app_module.save_user_training_rules(request_save_a))
    save_data = json.loads(save_resp.body.decode())
    assert save_data["status"] == "ok"
    assert save_data["rules"]["version"] == 1
    assert (
        save_data["rules"]["prohibited_exercises"][0]["exercise"]
        == "Behind The Neck Press"
    )

    # Subsequent save increments version
    save_resp_2 = asyncio.run(app_module.save_user_training_rules(request_save_a))
    save_data_2 = json.loads(save_resp_2.body.decode())
    assert save_data_2["rules"]["version"] == 2

    # 3. Multi-tenancy: User B's rules remain isolated and unaffected
    request_b = _make_auth_request("GET", "/api/settings/rules", user_id=user_b)
    resp_b = app_module.get_user_training_rules(request_b)
    rules_b = json.loads(resp_b.body.decode())
    assert rules_b["prohibited_exercises"] == []
    assert rules_b["version"] == 1

    # 4. Validation error: conflicting equipment or invalid rep bounds returns 400
    invalid_payload = {
        "version": 2,
        "rep_intensity_bounds": {"min_reps": 15, "max_reps": 8},
    }
    request_invalid = _make_auth_request(
        "POST", "/api/settings/rules", body=invalid_payload, user_id=user_a
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(app_module.save_user_training_rules(request_invalid))
    assert exc_info.value.status_code == 400

    # 5. GET /api/settings includes training_rules
    settings_req = _make_auth_request("GET", "/api/settings", user_id=user_a)
    settings_resp = app_module.api_settings(settings_req)
    settings_data = json.loads(settings_resp.body.decode())
    assert "training_rules" in settings_data
    assert settings_data["training_rules"]["version"] == 2


def test_settings_credential_lifecycle_is_masked_and_audited(
    client: Any,
    monkeypatch: Any,
) -> None:
    """Save/test/rotate/revoke/delete never echo the submitted secret."""
    import webapp.app as app_module
    from ai_connection import ConnectionCheck
    from cryptography.fernet import Fernet
    from database import get_legacy_user_id
    from secret_vault import CredentialUnavailableError, SecretVault, VerificationResult

    async def _ok_check(user_id: str, provider: str, model: str, api_key: str) -> ConnectionCheck:
        return ConnectionCheck(
            provider=provider,
            model=model,
            ok=True,
            code="ok",
            message="Connected.",
            retryable=False,
            checked_at="2026-11-12T00:00:00+00:00",
        )

    monkeypatch.setattr(app_module, "_check_ai_connection", _ok_check)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    user_id = get_legacy_user_id(app_module.DB_PATH)
    original = "synthetic-claude-key-1234"

    saved = _call_json_route(
        app_module,
        app_module.save_api_key,
        {"provider": "claude", "api_key": original, "model": "claude-synthetic"},
        user_id,
    )
    assert original not in repr(saved)
    assert saved["credential"]["has_key"] is True
    assert saved["credential"]["masked"].endswith("1234")

    with sqlite3.connect(app_module.DB_PATH) as conn:
        stored = conn.execute(
            "SELECT api_key FROM user_api_keys WHERE user_id = ? AND provider = ?",
            (user_id, "claude"),
        ).fetchone()
    assert stored is not None
    assert stored[0].startswith("wv2.")
    assert stored[0] != original

    settings = client.get("/api/settings")
    assert settings.status_code == 200
    payload = settings.json()
    assert payload["user_keys"]["claude"]["has_key"] is True
    assert original not in settings.text

    rotated = "synthetic-claude-key-5678"
    rotated_result = _call_json_route(
        app_module,
        app_module.rotate_api_key,
        {"provider": "claude", "api_key": rotated, "model": "claude-rotated"},
        user_id,
    )
    assert rotated not in repr(rotated_result)
    assert rotated_result["credential"]["version"] == 2

    monkeypatch.setattr(
        app_module,
        "_verify_ai_credential",
        lambda credential: VerificationResult(True, "ok", "Accepted."),
    )
    tested = _call_json_route(
        app_module,
        app_module.test_api_key,
        {"provider": "claude"},
        user_id,
    )
    assert tested["status"] == "ok"
    assert rotated not in repr(tested)

    revoked = _call_json_route(
        app_module,
        app_module.revoke_api_key,
        {"provider": "claude"},
        user_id,
    )
    assert revoked["credential"]["status"] == "revoked"
    with pytest.raises(CredentialUnavailableError):
        SecretVault(app_module.DB_PATH).resolve(
            user_id, "claude", purpose="ai_generate"
        )

    replacement = "synthetic-claude-key-9012"
    _call_json_route(
        app_module,
        app_module.save_api_key,
        {"provider": "claude", "api_key": replacement},
        user_id,
    )
    deleted = _call_json_route(
        app_module,
        app_module.remove_api_key,
        {"provider": "claude"},
        user_id,
    )
    assert deleted["deleted"] is True

    audit = client.get("/api/settings/credentials/audit")
    assert audit.status_code == 200
    actions = {event["action"] for event in audit.json()["events"]}
    assert {"save", "rotate", "test", "revoke", "delete"} <= actions


def test_settings_credential_save_fails_closed_without_encryption_key(
    client: Any,
    monkeypatch: Any,
) -> None:
    """No ENCRYPTION_KEY means no credential can be stored or read."""
    import webapp.app as app_module
    from ai_connection import ConnectionCheck
    from database import get_legacy_user_id
    from fastapi import HTTPException

    async def _ok_check(user_id: str, provider: str, model: str, api_key: str) -> ConnectionCheck:
        return ConnectionCheck(
            provider=provider,
            model=model,
            ok=True,
            code="ok",
            message="Connected.",
            retryable=False,
            checked_at="2026-11-12T00:00:00+00:00",
        )

    monkeypatch.setattr(app_module, "_check_ai_connection", _ok_check)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    secret = "synthetic-never-stored-key"
    user_id = get_legacy_user_id(app_module.DB_PATH)
    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.json()["credential_vault"]["available"] is False

    with pytest.raises(HTTPException) as caught:
        _call_json_route(
            app_module,
            app_module.save_api_key,
            {"provider": "claude", "api_key": secret},
            user_id,
        )

    assert caught.value.status_code == 503
    assert secret not in str(caught.value.detail)



def test_catch_all_never_serves_files_outside_the_frontend_bundle(
    client: Any,
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    """A traversing request must not escape the bundled SPA directory."""
    import webapp.app as app_module

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("SPA-INDEX")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    monkeypatch.setattr(app_module, "FRONTEND_DIST", bundle)

    served = client.get("/index.html")
    assert served.status_code == 200
    assert "SPA-INDEX" in served.text

    escaped = client.get("/%2e%2e/secret.txt")
    assert "TOPSECRET" not in escaped.text
    assert "SPA-INDEX" in escaped.text
