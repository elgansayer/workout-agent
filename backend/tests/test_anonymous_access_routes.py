"""Regression coverage for anonymous access to every FastAPI route.

The route inventory is discovered from the live FastAPI application.  Public
paths come from the same canonical authentication policy used in production, so
a newly-added personalised route cannot become public by updating a duplicated
test allowlist.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from webapp.auth_boundary import (
    PUBLIC_EXACT_PATHS,
    PUBLIC_STATIC_PREFIXES,
    has_verified_identity,
    is_public_path,
)

SENSITIVE_BODY_MARKERS = (
    "api_key",
    "refresh_token",
    "access_token",
    "authorization",
    "workout_history",
    "body_fat_pct",
    "personal_records",
    "chat_messages",
)


def _representative_path(path: str) -> str:
    """Replace Starlette path parameters with harmless deterministic values."""

    def replace(match: re.Match[str]) -> str:
        spec = match.group(1)
        _name, _, converter = spec.partition(":")
        if converter in {"int", "float"}:
            return "1"
        if converter == "path":
            return "sample"
        return "test"

    return re.sub(r"\{([^{}]+)\}", replace, path)


def _http_routes(app: Any) -> Iterator[APIRoute]:
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route


@pytest.fixture()
def authenticated_boundary_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    # app.py mounts the Angular dist directory at import time.  The route tests
    # do not need a built SPA, but StaticFiles requires the directory to exist.
    repo_root = Path(__file__).resolve().parents[2]
    (repo_root / "frontend" / "dist" / "frontend" / "browser").mkdir(
        parents=True,
        exist_ok=True,
    )

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "anonymous-route-test.db"))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    monkeypatch.setenv("WEB_AUTH_SECRET", "test-secret-that-is-long-enough-for-session-signing")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "anonymous-route-test-client")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "anonymous-route-test-secret")
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    return app_module.app


def test_route_inventory_is_non_empty(authenticated_boundary_app: Any) -> None:
    routes = list(_http_routes(authenticated_boundary_app))
    assert routes, "FastAPI route discovery unexpectedly returned no routes"
    assert any(route.path.startswith("/api/") for route in routes)


def test_every_personalised_route_fails_closed_when_logged_out(
    authenticated_boundary_app: Any,
) -> None:
    failures: list[str] = []

    with TestClient(authenticated_boundary_app) as client:
        for route in _http_routes(authenticated_boundary_app):
            if is_public_path(route.path):
                continue

            path = _representative_path(route.path)
            for method in sorted((route.methods or {"GET"}) - {"HEAD", "OPTIONS"}):
                response = client.request(method, path, follow_redirects=False)
                if route.path.startswith("/api/"):
                    allowed = {401, 403}
                else:
                    allowed = {301, 302, 303, 307, 308, 401, 403}

                if response.status_code not in allowed:
                    failures.append(
                        f"{method} {route.path} -> {response.status_code}; expected logged-out denial"
                    )
                    continue

                body = response.text.lower()
                leaked = [marker for marker in SENSITIVE_BODY_MARKERS if marker in body]
                if leaked:
                    failures.append(
                        f"{method} {route.path} leaked sensitive markers: {', '.join(leaked)}"
                    )

    assert not failures, "\n".join(failures)


def test_public_allowlist_is_explicit_and_small(authenticated_boundary_app: Any) -> None:
    discovered_public = {
        route.path
        for route in _http_routes(authenticated_boundary_app)
        if is_public_path(route.path)
    }
    unexpected_application_routes = discovered_public - PUBLIC_EXACT_PATHS
    assert not unexpected_application_routes


def test_static_policy_never_uses_generic_suffixes() -> None:
    assert is_public_path("/static/manifest.webmanifest")
    assert is_public_path("/assets/icons/icon-192.png")
    assert is_public_path("/main-ABCDEF12.js")
    assert not is_public_path("/api/export.json")
    assert not is_public_path("/reports/user-chart.svg")
    assert not is_public_path("/downloads/programme.pdf")
    assert PUBLIC_STATIC_PREFIXES == ("/static/", "/assets/")


def test_verified_identity_requires_complete_session() -> None:
    assert not has_verified_identity(None)
    assert not has_verified_identity({})
    assert not has_verified_identity({"user": "person@example.com"})
    assert not has_verified_identity({"user_id": "user-123"})
    assert not has_verified_identity({"user": "", "user_id": "user-123"})
    assert has_verified_identity(
        {"user": "person@example.com", "user_id": "user-123"}
    )
    assert has_verified_identity(
        {"user": {"email": "person@example.com"}, "user_id": "user-123"}
    )


def test_future_personalised_suffix_route_fails_closed(
    authenticated_boundary_app: Any,
) -> None:
    """A route that old suffix-based auth would expose must default to private."""

    @authenticated_boundary_app.get("/api/future-export.json")
    def future_export() -> dict[str, str]:
        return {"refresh_token": "must-never-be-anonymous"}

    with TestClient(authenticated_boundary_app) as client:
        response = client.get("/api/future-export.json", follow_redirects=False)

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}
    assert "must-never-be-anonymous" not in response.text
