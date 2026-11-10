"""Global authentication boundary for the Workout Agent web application.

Personalised routes fail closed by default.  The only anonymous HTTP surface is
an explicit set of login/OAuth/health routes plus narrowly recognised static
assets needed to render the login shell.  The boundary is installed at the
FastAPI middleware-stack level so future HTML, JSON, streaming, download, or
diagnostic routes inherit authentication without route-by-route opt-in.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from webapp.runtime_security import validate_web_runtime

# Keep this list intentionally small.  OAuth callbacks must be reachable before
# identity is established; liveness/readiness endpoints contain no tenant data.
PUBLIC_EXACT_PATHS = frozenset(
    {
        "/login",
        "/login/google",
        "/auth",
        
        "/livez",
        "/readyz",
        "/favicon.ico",
        "/sw.js",
    }
)

PUBLIC_STATIC_PREFIXES = ("/static/", "/assets/")

# Angular can emit fingerprinted bundles at the web root.  Recognise only a
# root-level fingerprinted asset filename.  Never treat a nested path or a
# generic suffix such as `.json` as public, because future user exports may use
# those suffixes.
_ROOT_FINGERPRINTED_ASSET = re.compile(
    r"^/[A-Za-z0-9_.-]+[-.][A-Za-z0-9]{8,64}"
    r"\.(?:css|js|mjs|map|png|jpg|jpeg|gif|webp|svg|ico|woff|woff2|ttf)$",
    re.IGNORECASE,
)


def is_public_path(path: str) -> bool:
    """Return whether *path* is part of the deliberate anonymous surface."""

    if path in PUBLIC_EXACT_PATHS:
        return True
    if path in {"/static", "/assets"} or path.startswith(PUBLIC_STATIC_PREFIXES):
        return True
    return bool(_ROOT_FINGERPRINTED_ASSET.fullmatch(path))


def has_verified_identity(session: Mapping[str, Any] | None) -> bool:
    """Require the signed session to contain both user identity components."""

    if not session:
        return False

    user_id = session.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return False

    user = session.get("user")
    if isinstance(user, str):
        return bool(user.strip())
    if isinstance(user, Mapping):
        return bool(user.get("email") or user.get("id"))
    return False


def _database_path() -> str:
    return os.environ.get("DATABASE_PATH", "workout_agent.db").strip() or "workout_agent.db"


def _client_ip(scope: Scope) -> str:
    state = scope.get("state")
    if isinstance(state, dict):
        validated = state.get("validated_client_ip")
        if isinstance(validated, str) and validated and validated != "unknown":
            return validated
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return ""


def _active_session(scope: Scope, session: dict[str, Any]) -> bool:
    """Validate, adopt, or reject the server-side session for this request.

    Sessions created before the registry existed have no ``sid`` yet. They are
    adopted once so an existing deployment keeps working and then becomes
    revocable like any new session. Adoption stops once the user has revoked
    any session, so a copied legacy cookie cannot be replayed after revocation. A present but unknown, revoked, expired, or
    foreign session id fails closed.
    """

    from session_registry import (
        SESSION_COOKIE_KEY,
        SessionRegistryError,
        create_session,
        legacy_session_adoptable,
        request_metadata,
        validate_session,
    )

    user_id = str(session.get("user_id", "")).strip()
    if not user_id:
        return False

    session_id = session.get(SESSION_COOKIE_KEY)
    if isinstance(session_id, str) and session_id:
        try:
            return validate_session(user_id, session_id, _database_path())
        except sqlite3.Error:
            return False

    try:
        if not legacy_session_adoptable(user_id, _database_path()):
            return False
    except sqlite3.Error:
        return False

    user_agent, client_ip, country_code = request_metadata(
        Headers(scope=scope),
        _client_ip(scope),
    )
    try:
        record = create_session(
            user_id,
            user_agent=user_agent,
            client_ip=client_ip,
            country_code=country_code,
            db_path=_database_path(),
        )
    except (sqlite3.Error, SessionRegistryError):
        return False
    session[SESSION_COOKIE_KEY] = record["sid"]
    return True


class AuthenticationBoundaryMiddleware:
    """Fail closed for every non-public HTTP request without a verified user."""

    def __init__(self, app: ASGIApp, *, anonymous_enabled: bool = False) -> None:
        self.app = app
        self.anonymous_enabled = anonymous_enabled

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http" or self.anonymous_enabled:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "/")
        if is_public_path(path):
            await self.app(scope, receive, send)
            return

        session = scope.get("session")
        if isinstance(session, dict) and has_verified_identity(session):
            if _active_session(scope, session):
                await self.app(scope, receive, send)
                return
            session.clear()

        response: Any
        if path.startswith("/api/"):
            response = JSONResponse(
                {"detail": "Not authenticated"},
                status_code=401,
            )
        else:
            response = RedirectResponse("/login", status_code=303)
        await response(scope, receive, send)


def _contains_boundary(app: FastAPI) -> bool:
    return any(
        getattr(item, "cls", None) is AuthenticationBoundaryMiddleware
        for item in app.user_middleware
    )


def _install_on_workout_app(app: FastAPI) -> None:
    """Place the boundary immediately inside SessionMiddleware.

    Starlette applies ``user_middleware`` from the end of the list inward.  The
    session middleware therefore needs to remain outside this boundary so the
    signed cookie has already been decoded into ``scope['session']``.
    """

    if app.title != "Workout Agent" or _contains_boundary(app):
        return

    session_index = next(
        (
            index
            for index, item in enumerate(app.user_middleware)
            if getattr(item, "cls", None) is SessionMiddleware
        ),
        None,
    )
    if session_index is None:
        raise RuntimeError(
            "Workout Agent authentication boundary requires SessionMiddleware"
        )

    runtime = validate_web_runtime()
    app.user_middleware.insert(
        session_index + 1,
        Middleware(
            AuthenticationBoundaryMiddleware,
            anonymous_enabled=runtime.anonymous_enabled,
        ),
    )


def install_authentication_boundary() -> None:
    """Install the process-wide FastAPI stack hook once.

    ``webapp.app`` constructs its FastAPI object after the package bootstrap has
    run.  Hooking stack construction lets us add a global boundary without
    depending on every route author remembering an authentication decorator.
    """

    if getattr(FastAPI, "_workout_auth_boundary_installed", False):
        return

    original = FastAPI.build_middleware_stack

    def guarded_build_middleware_stack(self: FastAPI) -> ASGIApp:
        _install_on_workout_app(self)
        return original(self)

    FastAPI._workout_auth_boundary_original_build = original  # type: ignore[attr-defined]
    FastAPI.build_middleware_stack = guarded_build_middleware_stack  # type: ignore[method-assign]
    FastAPI._workout_auth_boundary_installed = True  # type: ignore[attr-defined]


__all__ = [
    "PUBLIC_EXACT_PATHS",
    "PUBLIC_STATIC_PREFIXES",
    "AuthenticationBoundaryMiddleware",
    "has_verified_identity",
    "install_authentication_boundary",
    "is_public_path",
]
