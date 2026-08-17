"""Fail-closed security boundary for the web dashboard.

The core application can run anonymously for local development, but only when
that mode is explicitly enabled. Production deployments must provide a complete
Google OAuth configuration and a strong session secret before the ASGI app is
allowed to start.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

_PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SUPPORTED_AUTH_MODES = frozenset({"google"})
_SECRET_MIN_LENGTH = 32
_SECRET_MIN_UNIQUE_CHARACTERS = 8

PRIVATE_CACHE_CONTROL = "private, no-store, max-age=0"
_AUTH_VARIABLES = (
    "WEB_AUTH_SECRET",
    "WEB_GOOGLE_CLIENT_ID",
    "WEB_GOOGLE_CLIENT_SECRET",
    "ALLOWED_EMAILS",
)


class SecurityConfigurationError(RuntimeError):
    """Raised when the web process is not safe to start."""


def _value(environ: Mapping[str, str], name: str) -> str:
    return str(environ.get(name, "")).strip()


def _enabled(environ: Mapping[str, str], name: str) -> bool:
    return _value(environ, name).lower() in _TRUE_VALUES


def environment_name(environ: Mapping[str, str]) -> str:
    """Return the configured environment using supported deployment aliases."""
    for name in ("APP_ENV", "ENVIRONMENT", "WEB_ENVIRONMENT"):
        value = _value(environ, name)
        if value:
            return value.lower()
    return "development"


def is_production(environ: Mapping[str, str]) -> bool:
    """Whether the supplied environment represents a production deployment."""
    return environment_name(environ) in _PRODUCTION_ENVIRONMENTS


def _validate_google_auth(environ: Mapping[str, str], problems: list[str]) -> None:
    secret = _value(environ, "WEB_AUTH_SECRET")
    if not secret:
        problems.append("WEB_AUTH_SECRET is required")
    elif len(secret) < _SECRET_MIN_LENGTH:
        problems.append(
            f"WEB_AUTH_SECRET must be at least {_SECRET_MIN_LENGTH} characters",
        )
    elif len(set(secret)) < _SECRET_MIN_UNIQUE_CHARACTERS:
        problems.append("WEB_AUTH_SECRET does not contain enough entropy")

    if not _value(environ, "WEB_GOOGLE_CLIENT_ID"):
        problems.append("WEB_GOOGLE_CLIENT_ID is required")
    if not _value(environ, "WEB_GOOGLE_CLIENT_SECRET"):
        problems.append("WEB_GOOGLE_CLIENT_SECRET is required")

    allowed_emails = [
        email.strip()
        for email in _value(environ, "ALLOWED_EMAILS").split(",")
        if email.strip()
    ]
    if not allowed_emails:
        problems.append("ALLOWED_EMAILS must contain at least one address")
    elif any(
        "@" not in email or email.startswith("@") or email.endswith("@")
        for email in allowed_emails
    ):
        problems.append("ALLOWED_EMAILS contains an invalid address")


def validate_web_security(environ: Mapping[str, str]) -> None:
    """Reject unsafe auth modes without exposing any configured secret value."""
    production = is_production(environ)
    anonymous = _enabled(environ, "WEB_ALLOW_ANONYMOUS")
    configured_auth = any(_value(environ, name) for name in _AUTH_VARIABLES)
    auth_mode = _value(environ, "WEB_AUTH_MODE").lower() or "google"
    problems: list[str] = []

    if auth_mode not in _SUPPORTED_AUTH_MODES:
        problems.append(
            "WEB_AUTH_MODE must name a supported authentication boundary (google)",
        )

    if production and anonymous:
        problems.append("WEB_ALLOW_ANONYMOUS cannot be enabled in production")
    elif anonymous and configured_auth:
        problems.append(
            "WEB_ALLOW_ANONYMOUS cannot be combined with authentication settings",
        )

    if production or configured_auth:
        if auth_mode == "google":
            _validate_google_auth(environ, problems)
    elif not anonymous:
        problems.append(
            "set WEB_ALLOW_ANONYMOUS=1 explicitly for local development",
        )

    if problems:
        details = "\n".join(f"  - {problem}" for problem in problems)
        raise SecurityConfigurationError(
            "Unsafe web configuration; refusing to start:\n" + details,
        )


def validate_current_environment() -> None:
    """Validate the process environment used by the running application."""
    import os

    validate_web_security(os.environ)


def _set_header(
    headers: list[tuple[bytes, bytes]],
    name: bytes,
    value: bytes,
) -> None:
    headers[:] = [(key, current) for key, current in headers if key.lower() != name]
    headers.append((name, value))


def _append_vary_cookie(headers: list[tuple[bytes, bytes]]) -> None:
    vary_values = [
        value.decode("latin-1")
        for key, value in headers
        if key.lower() == b"vary"
    ]
    tokens = {
        token.strip()
        for value in vary_values
        for token in value.split(",")
        if token.strip()
    }
    if not any(token.lower() == "cookie" for token in tokens):
        tokens.add("Cookie")
    value = ", ".join(sorted(tokens, key=str.lower)).encode("latin-1")
    _set_header(headers, b"vary", value)


class PrivateResponseHeadersMiddleware:
    """Prevent browsers and shared caches from storing personalised responses."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or path.startswith("/static/"):
            await self.app(scope, receive, send)
            return

        async def send_with_private_headers(
            message: MutableMapping[str, Any],
        ) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                _set_header(
                    headers,
                    b"cache-control",
                    PRIVATE_CACHE_CONTROL.encode("ascii"),
                )
                _set_header(headers, b"pragma", b"no-cache")
                _set_header(headers, b"expires", b"0")
                _append_vary_cookie(headers)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_private_headers)
