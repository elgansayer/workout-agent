"""Global authentication boundary for the Workout Agent web application.

Personalised routes fail closed by default.  The only anonymous HTTP surface is
an explicit set of login/OAuth/health routes plus narrowly recognised static
assets needed to render the login shell.  The boundary is installed at the
FastAPI middleware-stack level so future HTML, JSON, streaming, download, or
diagnostic routes inherit authentication without route-by-route opt-in.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
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
        "/google-health/callback",
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
        if not isinstance(session, Mapping) or not has_verified_identity(session):
            if path.startswith("/api/"):
                response = JSONResponse(
                    {"detail": "Not authenticated"},
                    status_code=401,
                )
            else:
                response = RedirectResponse("/login", status_code=303)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


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
    "AuthenticationBoundaryMiddleware",
    "PUBLIC_EXACT_PATHS",
    "PUBLIC_STATIC_PREFIXES",
    "has_verified_identity",
    "install_authentication_boundary",
    "is_public_path",
]
