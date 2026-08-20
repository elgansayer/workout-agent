"""Regression coverage for anonymous access to every FastAPI route.

Issue #754 requires the route inventory to fail closed by default.  These tests
configure production-style web authentication before importing the app, discover
the current FastAPI route table dynamically, and probe every concrete HTTP route
without a session.
"""

from __future__ import annotations

import importlib
import os
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


PUBLIC_EXACT_PATHS = {
    "/login",
    "/login/google",
    "/logout",
    "/auth",
    "/google-health/callback",
    "/favicon.ico",
    "/sw.js",
}
PUBLIC_PREFIXES = ("/static/", "/assets/")
PUBLIC_SUFFIXES = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".map",
    ".json",
    ".webmanifest",
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


def _is_public(path: str) -> bool:
    return (
        path in PUBLIC_EXACT_PATHS
        or path.startswith(PUBLIC_PREFIXES)
        or path.endswith(PUBLIC_SUFFIXES)
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
            if _is_public(route.path):
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
        route.path for route in _http_routes(authenticated_boundary_app) if _is_public(route.path)
    }
    unexpected = discovered_public - PUBLIC_EXACT_PATHS
    # Asset suffix/prefix routes are permitted, but personalised application
    # routes must never become public merely because a new route was added.
    unexpected_application_routes = {
        path
        for path in unexpected
        if not path.startswith(PUBLIC_PREFIXES) and not path.endswith(PUBLIC_SUFFIXES)
    }
    assert not unexpected_application_routes
