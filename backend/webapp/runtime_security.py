"""Fail-closed runtime validation for the web dashboard.

The dashboard contains health data and user-supplied credentials. Importing the
web package therefore validates that either complete Google authentication is
configured or anonymous access has been explicitly enabled for a trusted
non-production environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

_AUTH_VARIABLES = (
    "WEB_AUTH_SECRET",
    "WEB_GOOGLE_CLIENT_ID",
    "WEB_GOOGLE_CLIENT_SECRET",
)
_PRODUCTION_ENVIRONMENTS = {"prod", "production"}
_NON_PRODUCTION_ENVIRONMENTS = {"dev", "development", "local", "test", "testing"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_MINIMUM_SESSION_SECRET_LENGTH = 32


class WebRuntimeConfigurationError(RuntimeError):
    """Raised before startup when the web security boundary is unsafe."""


@dataclass(frozen=True, slots=True)
class WebRuntimeSecurity:
    """Validated, non-secret web runtime state."""

    environment: str
    authentication_enabled: bool
    anonymous_enabled: bool


def _value(environ: Mapping[str, str], name: str) -> str:
    return environ.get(name, "").strip()


def _parse_bool(environ: Mapping[str, str], name: str) -> bool:
    raw = _value(environ, name).lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise WebRuntimeConfigurationError(
        f"{name} must be one of: 1, 0, true, false, yes, no, on, off."
    )


def validate_web_runtime(
    environ: Mapping[str, str] | None = None,
) -> WebRuntimeSecurity:
    """Validate the web authentication boundary before application startup.

    Production always requires a complete Google OAuth configuration. Anonymous
    mode is available only when both a recognised non-production ``APP_ENV`` and
    ``ALLOW_ANONYMOUS_WEB=1`` are set explicitly.
    """

    source = os.environ if environ is None else environ
    environment = _value(source, "APP_ENV").lower()
    supported = _PRODUCTION_ENVIRONMENTS | _NON_PRODUCTION_ENVIRONMENTS

    if not environment:
        raise WebRuntimeConfigurationError(
            "APP_ENV must be set explicitly before the web dashboard starts. "
            f"Supported values: {', '.join(sorted(supported))}."
        )
    if environment not in supported:
        raise WebRuntimeConfigurationError(
            f"Unsupported APP_ENV '{environment}'. "
            f"Supported values: {', '.join(sorted(supported))}."
        )

    configured = {name: bool(_value(source, name)) for name in _AUTH_VARIABLES}
    present = [name for name, is_set in configured.items() if is_set]
    missing = [name for name, is_set in configured.items() if not is_set]

    if present and missing:
        raise WebRuntimeConfigurationError(
            "Incomplete web authentication configuration. Missing variable(s): "
            + ", ".join(missing)
            + "."
        )

    authentication_enabled = not missing
    if authentication_enabled:
        secret = _value(source, "WEB_AUTH_SECRET")
        if len(secret) < _MINIMUM_SESSION_SECRET_LENGTH:
            raise WebRuntimeConfigurationError(
                "WEB_AUTH_SECRET must contain at least "
                f"{_MINIMUM_SESSION_SECRET_LENGTH} characters."
            )
        return WebRuntimeSecurity(
            environment=environment,
            authentication_enabled=True,
            anonymous_enabled=False,
        )

    anonymous_enabled = _parse_bool(source, "ALLOW_ANONYMOUS_WEB")
    if environment in _PRODUCTION_ENVIRONMENTS:
        raise WebRuntimeConfigurationError(
            "Production web startup blocked: configure WEB_AUTH_SECRET, "
            "WEB_GOOGLE_CLIENT_ID, and WEB_GOOGLE_CLIENT_SECRET. Anonymous "
            "dashboard access is never permitted in production."
        )
    if not anonymous_enabled:
        raise WebRuntimeConfigurationError(
            "Anonymous web startup blocked. Configure Google authentication or "
            "set ALLOW_ANONYMOUS_WEB=1 only for a trusted non-production runtime."
        )

    return WebRuntimeSecurity(
        environment=environment,
        authentication_enabled=False,
        anonymous_enabled=True,
    )
