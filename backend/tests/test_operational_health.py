"""Deterministic tests for production operational health endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from database import init_db
from webapp.health import (
    OperationalHealthMiddleware,
    database_readiness,
    install_authenticated_diagnostics,
    readiness_status,
)


def _probe_client(db_path: str) -> TestClient:
    app = FastAPI()
    return TestClient(OperationalHealthMiddleware(app, db_path=db_path))


def _diagnostic_client(db_path: str) -> TestClient:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.post("/__test/login/{user_id}")
    def login(user_id: str, request: Request) -> dict[str, str]:
        request.session["user_id"] = user_id
        return {"status": "ok"}

    install_authenticated_diagnostics(app, db_path=db_path)
    return TestClient(app)


def test_liveness_is_shallow_when_database_is_missing(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.db")
    with _probe_client(missing) as client:
        response = client.get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    assert not Path(missing).exists()


def test_readiness_is_terse_and_fails_closed_for_missing_database(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.db")
    with _probe_client(missing) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "database" not in response.text.lower()
    assert str(tmp_path) not in response.text
    assert not Path(missing).exists()


def test_readiness_requires_current_core_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    db_path.touch()

    ready, reason = database_readiness(str(db_path))
    status_code, public_payload = readiness_status(str(db_path))

    assert ready is False
    assert reason == "schema_incomplete"
    assert status_code == 503
    assert public_payload == {"status": "not_ready"}


def test_readiness_passes_after_database_initialisation(tmp_path: Path) -> None:
    db_path = str(tmp_path / "ready.db")
    init_db(db_path)

    with _probe_client(db_path) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["cache-control"] == "no-store"


def test_diagnostics_requires_authenticated_tenant_session(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    db_path = str(tmp_path / "diagnostics.db")
    init_db(db_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with _diagnostic_client(db_path) as client:
        response = client.get("/api/diagnostics/health")

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_authenticated_diagnostics_are_aggregate_and_secret_free(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    db_path = str(tmp_path / "diagnostics.db")
    init_db(db_path)
    monkeypatch.setenv("GEMINI_API_KEY", "never-return-this-secret")

    with _diagnostic_client(db_path) as client:
        assert client.post("/__test/login/user-a").status_code == 200
        response = client.get("/api/diagnostics/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["components"]["database"] == {"status": "ok", "reason": "ok"}
    assert payload["components"]["worker_dependencies"] == {"status": "not_applicable"}
    assert payload["components"]["ai"] == {
        "status": "configured",
        "provider": "gemini",
    }
    assert payload["components"]["connectors"]["registered"] >= 1
    assert payload["components"]["connectors"]["configured"] == 0
    assert response.headers["cache-control"] == "private, no-store"
    assert "never-return-this-secret" not in response.text
    assert str(tmp_path) not in response.text


def test_authenticated_diagnostics_degrade_without_database(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.db")

    with _diagnostic_client(missing) as client:
        assert client.post("/__test/login/user-a").status_code == 200
        response = client.get("/api/diagnostics/health")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["components"]["database"]["status"] == "error"
    assert payload["components"]["database"]["reason"] == "database_unavailable"
    assert "ai" not in payload["components"]
    assert "connectors" not in payload["components"]
