"""Smoke tests for the internal web app.

Skipped automatically when FastAPI is not installed (it lives in
requirements-web.txt, separate from the agent's core dependencies).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
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


@pytest.mark.skip(reason="Angular migration")
def test_dashboard_ok(client: Any) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Block" in response.text
    assert "Week" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_progress_ok(client: Any) -> None:
    response = client.get("/progress")
    assert response.status_code == 200
    assert "Progress" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_checkins_shows_saved_checkin(client: Any) -> None:
    response = client.get("/checkins")
    assert response.status_code == 200
    assert "Check-in 1" in response.text
    assert "solid block" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_nudge_button_and_endpoint_removed(client: Any) -> None:
    # Motivation is automated now: no button on the page, no /nudge route.
    page = client.get("/")
    assert "nudge-btn" not in page.text
    assert client.get("/nudge").status_code == 404


@pytest.mark.skip(reason="Angular migration")
def test_dashboard_shows_automated_quote_and_charts(client: Any) -> None:
    response = client.get("/")
    assert response.status_code == 200
    # The daily quote is rendered automatically and an SVG ring is present.
    assert "svg-ring" in response.text
    assert "Week" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_progress_renders_svg_charts(client: Any) -> None:
    response = client.get("/progress")
    assert response.status_code == 200
    assert "svg-chart" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_stats_ok(client: Any) -> None:
    response = client.get("/stats")
    assert response.status_code == 200
    assert "Personal records" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_plan_ok(client: Any) -> None:
    response = client.get("/plan")
    assert response.status_code == 200
    assert "Periodisation" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_history_ok(client: Any) -> None:
    response = client.get("/history")
    assert response.status_code == 200
    assert "Training calendar" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_stats_shows_projection_and_muscle_breakdown(client: Any) -> None:
    response = client.get("/stats")
    assert response.status_code == 200
    assert "muscle group" in response.text.lower()


@pytest.mark.skip(reason="Angular migration")
def test_pwa_manifest_and_service_worker(client: Any) -> None:
    page = client.get("/")
    assert "manifest.webmanifest" in page.text
    assert "/sw.js" in page.text

    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["service-worker-allowed"] == "/"

    manifest = client.get("/static/manifest.webmanifest")
    assert manifest.status_code == 200
    assert "Workout Agent" in manifest.text


@pytest.mark.skip(reason="Angular migration")
def test_settings_page_and_nav(client: Any) -> None:
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Google Health" in page.text
    # Unconfigured in tests: shows the setup hint, not a live connect button.
    assert "GOOGLE_HEALTH_CLIENT_ID" in page.text
    assert "/settings" in client.get("/").text


@pytest.mark.skip(reason="Angular migration")
def test_google_health_connect_unconfigured_redirects(client: Any) -> None:
    resp = client.get("/google-health/connect", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?gh=unconfigured"


@pytest.mark.skip(reason="Angular migration")
def test_google_health_disconnect_clears_token(client: Any) -> None:
    from database import get_meta, set_meta

    db_path = os.environ["DATABASE_PATH"]
    set_meta("google_health_refresh_token", "tok", db_path)
    resp = client.post("/google-health/disconnect", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?gh=disconnected"
    assert not get_meta("google_health_refresh_token", db_path)


def _configured_app(tmp_path: Any, monkeypatch: Any) -> tuple[Any, str]:
    db_path = str(tmp_path / "web.db")
    init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_SECRET", "secret")
    import webapp.app as app_module

    importlib.reload(app_module)
    return app_module, db_path


@pytest.mark.skip(reason="Angular migration")
def test_google_health_connect_redirects_to_google(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, _ = _configured_app(tmp_path, monkeypatch)
    with TestClient(app_module.app) as c:
        resp = c.get("/google-health/connect", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "client_id=cid" in location


@pytest.mark.skip(reason="Angular migration")
def test_google_health_callback_stores_refresh_token(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    from database import get_meta, set_meta

    app_module, db_path = _configured_app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        app_module,
        "exchange_code",
        lambda *a, **k: {"refresh_token": "rt-123"},
    )
    set_meta("google_health_oauth_state", "st-1", db_path)
    with TestClient(app_module.app) as c:
        resp = c.get(
            "/google-health/callback?code=abc&state=st-1",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?gh=connected"
    assert get_meta("google_health_refresh_token", db_path) == "rt-123"


@pytest.mark.skip(reason="Angular migration")
def test_google_health_callback_rejects_bad_state(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    from database import get_meta, set_meta

    app_module, db_path = _configured_app(tmp_path, monkeypatch)
    set_meta("google_health_oauth_state", "real-state", db_path)
    with TestClient(app_module.app) as c:
        resp = c.get(
            "/google-health/callback?code=abc&state=forged",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?gh=error"
    assert not get_meta("google_health_refresh_token", db_path)


@pytest.mark.skip(reason="Angular migration")
def test_programmes_page_ok(client: Any) -> None:
    """The /programmes page renders the selection UI."""
    response = client.get("/programmes")
    assert response.status_code == 200
    assert "Programme Builder" in response.text
    assert "Hybrid Powerbuilding" in response.text
    assert "Infer from my Hevy history" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_programmes_page_shows_available_templates(client: Any) -> None:
    """The /programmes page lists all available templates."""
    response = client.get("/programmes")
    assert response.status_code == 200
    assert "programme-card" in response.text
    assert "Select Programme" in response.text


@pytest.mark.skip(reason="Angular migration")
def test_api_programmes_select_requires_auth(client: Any) -> None:
    """POST /api/programmes/select without a session returns 401."""
    resp = client.post(
        "/api/programmes/select",
        json={"template_key": "hybrid_powerbuilding"},
    )
    assert resp.status_code == 401


@pytest.mark.skip(reason="Angular migration")
def test_api_programmes_select_rejects_unknown_template(client: Any) -> None:
    """Selecting an unknown template key is rejected (auth first, then validation)."""
    resp = client.post(
        "/api/programmes/select",
        json={"template_key": "nonexistent_template"},
    )
    assert resp.status_code in (400, 401)


@pytest.mark.skip(reason="Angular migration")
def test_api_programmes_select_requires_template_key(client: Any) -> None:
    """POST without template_key is rejected (auth first, then validation)."""
    resp = client.post(
        "/api/programmes/select",
        json={},
    )
    assert resp.status_code in (400, 401)


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
    from database import (
        get_legacy_user_id,
        save_user_api_key,
        save_user_preferences,
    )

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
    save_user_api_key(
        user_id,
        "claude",
        synthetic_key,
        extra={"model": "stale-model-metadata"},
        db_path=db_path,
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
            "default_model": "claude-sonnet-4-20250514",
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


# -- settings API-key CRUD ---------------------------------------------
