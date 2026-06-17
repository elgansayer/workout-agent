"""Smoke tests for the internal web app.

Skipped automatically when FastAPI is not installed (it lives in
requirements-web.txt, separate from the agent's core dependencies).
"""

from __future__ import annotations

import importlib
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from database import (  # noqa: E402
    init_db,
    save_body_metrics,
    save_checkin,
    save_daily_log,
    save_progress,
)
from hevy_parser import ExerciseSummary, WorkoutSummary  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "web.db")
    init_db(db_path)
    save_checkin(1, "2026-03-01", 24, 4, "Check-in 1: solid block.", db_path)
    save_body_metrics({"weight_kg": 82.0, "body_fat_pct": 16.0}, "2026-03-01", db_path)
    save_body_metrics({"weight_kg": 81.4, "body_fat_pct": 15.6}, "2026-03-08", db_path)
    save_daily_log("2026-03-02", 1, "Back, Deadlifts & Chest", "high", "plan", "life", db_path)
    summary = WorkoutSummary(
        title="Day 1",
        date="2026-03-02",
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


def test_dashboard_ok(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Block" in response.text
    assert "Week" in response.text


def test_progress_ok(client):
    response = client.get("/progress")
    assert response.status_code == 200
    assert "Progress" in response.text


def test_checkins_shows_saved_checkin(client):
    response = client.get("/checkins")
    assert response.status_code == 200
    assert "Check-in 1" in response.text
    assert "solid block" in response.text


def test_nudge_button_and_endpoint_removed(client):
    # Motivation is automated now: no button on the page, no /nudge route.
    page = client.get("/")
    assert "nudge-btn" not in page.text
    assert client.get("/nudge").status_code == 404


def test_dashboard_shows_automated_quote_and_charts(client):
    response = client.get("/")
    assert response.status_code == 200
    # The daily quote is rendered automatically and an SVG ring is present.
    assert "svg-ring" in response.text
    assert "day streak" in response.text


def test_progress_renders_svg_charts(client):
    response = client.get("/progress")
    assert response.status_code == 200
    assert "svg-chart" in response.text


def test_stats_ok(client):
    response = client.get("/stats")
    assert response.status_code == 200
    assert "Personal records" in response.text


def test_plan_ok(client):
    response = client.get("/plan")
    assert response.status_code == 200
    assert "Periodisation" in response.text


def test_history_ok(client):
    response = client.get("/history")
    assert response.status_code == 200
    assert "Training calendar" in response.text


def test_stats_shows_projection_and_muscle_breakdown(client):
    response = client.get("/stats")
    assert response.status_code == 200
    assert "muscle group" in response.text.lower()


def test_pwa_manifest_and_service_worker(client):
    page = client.get("/")
    assert "manifest.webmanifest" in page.text
    assert "/sw.js" in page.text

    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["service-worker-allowed"] == "/"

    manifest = client.get("/static/manifest.webmanifest")
    assert manifest.status_code == 200
    assert "Workout Agent" in manifest.text


def test_settings_page_and_nav(client):
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Google Health" in page.text
    # Unconfigured in tests: shows the setup hint, not a live connect button.
    assert "GOOGLE_HEALTH_CLIENT_ID" in page.text
    assert "/settings" in client.get("/").text


def test_google_health_connect_unconfigured_redirects(client):
    resp = client.get("/google-health/connect", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?gh=unconfigured"


def test_google_health_disconnect_clears_token(client):
    from database import get_meta, set_meta

    db_path = os.environ["DATABASE_PATH"]
    set_meta("google_health_refresh_token", "tok", db_path)
    resp = client.post("/google-health/disconnect", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?gh=disconnected"
    assert not get_meta("google_health_refresh_token", db_path)


def _configured_app(tmp_path, monkeypatch):
    db_path = str(tmp_path / "web.db")
    init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_SECRET", "secret")
    import webapp.app as app_module

    importlib.reload(app_module)
    return app_module, db_path


def test_google_health_connect_redirects_to_google(tmp_path, monkeypatch):
    app_module, _ = _configured_app(tmp_path, monkeypatch)
    with TestClient(app_module.app) as c:
        resp = c.get("/google-health/connect", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "client_id=cid" in location


def test_google_health_callback_stores_refresh_token(tmp_path, monkeypatch):
    from database import get_meta, set_meta

    app_module, db_path = _configured_app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        app_module, "exchange_code", lambda *a, **k: {"refresh_token": "rt-123"}
    )
    set_meta("google_health_oauth_state", "st-1", db_path)
    with TestClient(app_module.app) as c:
        resp = c.get(
            "/google-health/callback?code=abc&state=st-1", follow_redirects=False
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?gh=connected"
    assert get_meta("google_health_refresh_token", db_path) == "rt-123"


def test_google_health_callback_rejects_bad_state(tmp_path, monkeypatch):
    from database import get_meta, set_meta

    app_module, db_path = _configured_app(tmp_path, monkeypatch)
    set_meta("google_health_oauth_state", "real-state", db_path)
    with TestClient(app_module.app) as c:
        resp = c.get(
            "/google-health/callback?code=abc&state=forged", follow_redirects=False
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?gh=error"
    assert not get_meta("google_health_refresh_token", db_path)

