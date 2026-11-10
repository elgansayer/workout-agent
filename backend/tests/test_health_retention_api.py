from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Generator
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from database import get_legacy_user_id, get_meta, init_db, set_meta
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request


@pytest.fixture()
def client(
    tmp_path: Any, monkeypatch: Any
) -> Generator[tuple[TestClient, str, Any], None, None]:
    db_path = str(tmp_path / "retention.db")
    init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.delenv("WEB_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_SECRET", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as test_client:
        yield test_client, db_path, app_module


def _login(test_client: TestClient, app_module: Any, user_id: str) -> None:
    """Establish the complete signed session mutation security requires."""

    def login(request: Request) -> dict[str, str]:
        request.session["user"] = "synthetic@example.test"
        request.session["user_id"] = user_id
        return {"status": "ok"}

    app_module.app.router.routes.insert(
        0, APIRoute("/_test/retention-login", login, methods=["GET"])
    )
    assert test_client.get("/_test/retention-login").status_code == 200


def _seed(db_path: str, user_id: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "INSERT INTO health_connections "
            "(id, user_id, provider, state, granted_capabilities) VALUES (?, ?, ?, ?, ?)",
            [
                ("conn-1", user_id, "withings", "connected", "[]"),
                ("conn-2", user_id, "oura", "connected", "[]"),
                ("other-conn", "another-user", "withings", "connected", "[]"),
            ],
        )
        connection.executemany(
            "INSERT INTO health_metrics "
            "(user_id, metric_type, value, unit, observed_at, provider, connection_id, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (user_id, "weight_kg", 80.0, "kg", "2026-08-01T00:00:00+00:00", "withings", "conn-1", "a"),
                (user_id, "sleep_duration_min", 420.0, "min", "2026-08-01T00:00:00+00:00", "oura", "conn-2", "b"),
                ("another-user", "weight_kg", 90.0, "kg", "2026-08-01T00:00:00+00:00", "withings", "other-conn", "c"),
            ],
        )
        connection.execute(
            "INSERT INTO dashboard_insights (user_id, date, insight_json) VALUES (?, ?, ?)",
            (user_id, "2026-08-01", '{"weight": 80}'),
        )
        connection.execute(
            "INSERT INTO workout_history (user_id, hevy_payload, date) VALUES (?, ?, ?)",
            (user_id, "User entered workout", "2026-08-01"),
        )
        connection.commit()


def test_connections_expose_retention_choices(client: tuple[TestClient, str]) -> None:
    test_client, db_path, app_module = client
    user_id = get_legacy_user_id(db_path)
    _seed(db_path, user_id)
    _login(test_client, app_module, user_id)

    response = test_client.get("/api/health/connections")

    assert response.status_code == 200
    payload = response.json()
    assert {option["mode"] for option in payload["retention_options"]} == {
        "revoke_only",
        "retain",
        "purge",
    }
    connection = next(item for item in payload["connections"] if item["id"] == "conn-1")
    assert connection["provider"] == "withings"
    assert connection["retention_options"]


def test_purge_route_removes_imported_and_derived_but_keeps_user_records(
    client: tuple[TestClient, str],
) -> None:
    test_client, db_path, app_module = client
    user_id = get_legacy_user_id(db_path)
    _seed(db_path, user_id)
    _login(test_client, app_module, user_id)

    response = test_client.delete(
        "/api/health/connections/conn-1",
        params={"mode": "purge", "confirmed": "true"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "purge"
    assert result["purged"]["imported_metrics_deleted"] == 1
    assert result["purged"]["derived_insights_deleted"] == 1

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT provider FROM health_metrics WHERE user_id = ?", (user_id,)
        ).fetchall() == [("oura",)]
        assert connection.execute(
            "SELECT provider FROM health_metrics WHERE user_id = 'another-user'"
        ).fetchall() == [("withings",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM dashboard_insights WHERE user_id = ?", (user_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT hevy_payload FROM workout_history WHERE user_id = ?", (user_id,)
        ).fetchall() == [("User entered workout",)]


def test_purge_route_requires_confirmation(client: tuple[TestClient, str]) -> None:
    test_client, db_path, app_module = client
    user_id = get_legacy_user_id(db_path)
    _seed(db_path, user_id)
    _login(test_client, app_module, user_id)

    response = test_client.delete(
        "/api/health/connections/conn-1",
        params={"mode": "purge"},
    )

    assert response.status_code == 400


def test_connection_from_another_tenant_is_not_found(client: tuple[TestClient, str]) -> None:
    test_client, db_path, app_module = client
    user_id = get_legacy_user_id(db_path)
    _seed(db_path, user_id)
    _login(test_client, app_module, user_id)

    response = test_client.delete(
        "/api/health/connections/other-conn",
        params={"mode": "revoke_only"},
    )

    assert response.status_code == 404
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM health_metrics WHERE user_id = 'another-user'"
        ).fetchone()[0] == 1


def test_revoke_only_keeps_connection_and_data(client: tuple[TestClient, str]) -> None:
    test_client, db_path, app_module = client
    user_id = get_legacy_user_id(db_path)
    _seed(db_path, user_id)
    _login(test_client, app_module, user_id)

    response = test_client.delete(
        "/api/health/connections/conn-1",
        params={"mode": "revoke_only"},
    )

    assert response.status_code == 200
    assert response.json()["purged"] is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM health_connections WHERE id = 'conn-1'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM health_metrics WHERE user_id = ? AND provider = 'withings'",
            (user_id,),
        ).fetchone()[0] == 1


def test_google_health_purge_removes_only_its_imported_metrics(
    client: tuple[TestClient, str, Any],
) -> None:
    test_client, db_path, app_module = client
    user_id = get_legacy_user_id(db_path)
    _login(test_client, app_module, user_id)

    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "INSERT INTO body_metrics (date, weight_kg, user_id, source) VALUES (?, ?, ?, ?)",
            [
                ("2026-08-01", 80.0, user_id, "google_health"),
                ("2026-08-01", 79.5, user_id, "legacy"),
            ],
        )
        connection.execute(
            "INSERT INTO dashboard_insights (user_id, date, insight_json) VALUES (?, ?, ?)",
            (user_id, "2026-08-01", '{"weight": 80}'),
        )
        connection.commit()
    set_meta("google_health_refresh_token", "synthetic-token", db_path, user_id=user_id)

    response = test_client.post(
        "/google-health/disconnect",
        params={"mode": "purge", "confirmed": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "retention=purge" in response.headers["location"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT source FROM body_metrics WHERE user_id = ? ORDER BY source",
            (user_id,),
        ).fetchall() == [("legacy",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM dashboard_insights WHERE user_id = ?", (user_id,)
        ).fetchone()[0] == 0
    assert not get_meta("google_health_refresh_token", db_path, user_id=user_id)
