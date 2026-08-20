"""Operational health probes for the production web service.

Public probes deliberately expose only an aggregate status. Provider and AI
configuration is available only through the authenticated diagnostics endpoint.
No health probe makes an outbound provider request.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from connectors.builtin import build_builtin_registry

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[dict[str, Any], Receive, Send], Awaitable[None]]

# These tables are created by the current init_db migration path and represent
# the minimum schema needed to serve authenticated product traffic safely.
_REQUIRED_TABLES = frozenset(
    {
        "users",
        "user_preferences",
        "programmes",
        "workout_history",
        "notifications",
        "hevy_meta",
    }
)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """Open an existing SQLite database without creating or mutating it."""

    if not db_path or not db_path.strip():
        raise ValueError("database path is not configured")
    if db_path == ":memory:":
        # A new connection would create a different in-memory database, so an
        # in-memory database cannot be probed safely from a separate request.
        raise ValueError("in-memory database is not readiness-probeable")

    path = Path(db_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("database does not exist")

    # mode=ro guarantees that a readiness request cannot create a database or
    # turn a failed deployment into a superficially healthy one.
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.execute("PRAGMA query_only = ON")
    return conn


def database_readiness(db_path: str) -> tuple[bool, str]:
    """Check database reachability and the minimum current schema.

    The returned reason is an internal, stable category and never contains an
    exception message, filesystem path, SQL text, credential, or provider data.
    """

    try:
        with _open_readonly(db_path) as conn:
            row = conn.execute("SELECT 1").fetchone()
            if row != (1,):
                return False, "database_query_failed"
            tables = {
                str(item[0])
                for item in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not _REQUIRED_TABLES.issubset(tables):
                return False, "schema_incomplete"
    except (OSError, sqlite3.Error, ValueError):
        return False, "database_unavailable"
    return True, "ok"


def readiness_status(db_path: str) -> tuple[int, dict[str, str]]:
    """Return the intentionally terse public readiness response."""

    ready, _reason = database_readiness(db_path)
    if ready:
        return 200, {"status": "ready"}
    return 503, {"status": "not_ready"}


def _safe_user_dependency_state(db_path: str, user_id: str) -> dict[str, Any]:
    """Read non-secret per-user dependency configuration for diagnostics."""

    with _open_readonly(db_path) as conn:
        preferred_row = conn.execute(
            "SELECT preferred_ai FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        preferred_ai = (
            str(preferred_row[0]).strip().lower()
            if preferred_row and preferred_row[0]
            else "gemini"
        )
        key_rows = conn.execute(
            "SELECT provider FROM user_api_keys "
            "WHERE user_id = ? AND api_key IS NOT NULL AND api_key <> ''",
            (user_id,),
        ).fetchall()
        configured = {str(row[0]).strip().lower() for row in key_rows if row[0]}

    registry = build_builtin_registry()
    connector_providers = set(registry.providers())
    configured_connectors = connector_providers.intersection(configured)

    ai_configured = preferred_ai in configured
    if preferred_ai == "gemini" and os.environ.get("GEMINI_API_KEY", "").strip():
        ai_configured = True

    return {
        "ai": {
            "status": "configured" if ai_configured else "not_configured",
            "provider": preferred_ai,
        },
        "connectors": {
            "status": "available",
            "registered": len(connector_providers),
            "configured": len(configured_connectors),
        },
    }


def authenticated_diagnostics(db_path: str, user_id: str) -> tuple[int, dict[str, Any]]:
    """Return safe aggregate dependency health for one authenticated user."""

    db_ready, db_reason = database_readiness(db_path)
    payload: dict[str, Any] = {
        "status": "ok" if db_ready else "degraded",
        "components": {
            "database": {
                "status": "ok" if db_ready else "error",
                "reason": db_reason,
            },
            # The current web deployment has no external queue/worker dependency;
            # scheduled agent work shares SQLite but is not required to serve an
            # authenticated HTTP request. Keep this explicit instead of inventing
            # a Redis/Celery dependency that does not exist.
            "worker_dependencies": {"status": "not_applicable"},
        },
    }

    if db_ready:
        try:
            payload["components"].update(_safe_user_dependency_state(db_path, user_id))
        except (OSError, sqlite3.Error, ValueError):
            payload["status"] = "degraded"
            payload["components"]["dependencies"] = {"status": "error"}

    return (200 if payload["status"] == "ok" else 503), payload


class OperationalHealthMiddleware:
    """Serve public liveness/readiness before authentication middleware.

    This middleware is installed only at the canonical production ASGI
    entrypoint and is wrapped by SecurityHeadersMiddleware, so probes remain
    public while still receiving the normal security headers.
    """

    def __init__(self, app: ASGIApp, *, db_path: str) -> None:
        self.app = app
        self.db_path = db_path

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("method") == "GET":
            path = scope.get("path")
            if path == "/livez":
                response = JSONResponse(
                    {"status": "ok"},
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
            if path == "/readyz":
                status_code, payload = readiness_status(self.db_path)
                response = JSONResponse(
                    payload,
                    status_code=status_code,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def install_authenticated_diagnostics(app: FastAPI, *, db_path: str) -> None:
    """Install the tenant-scoped diagnostics route exactly once."""

    path = "/api/diagnostics/health"
    if any(getattr(route, "path", None) == path for route in app.routes):
        return

    @app.get(path, include_in_schema=False)
    def diagnostics(request: Request) -> JSONResponse:
        # Deliberately stricter than legacy anonymous mode. Dependency details
        # are never available without an authenticated, tenant-scoped session.
        user_id = request.session.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        status_code, payload = authenticated_diagnostics(db_path, user_id)
        return JSONResponse(
            payload,
            status_code=status_code,
            headers={"Cache-Control": "private, no-store"},
        )
