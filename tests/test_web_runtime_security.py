"""Tests for the fail-closed web runtime security contract."""

from __future__ import annotations

import pytest

from webapp.runtime_security import (
    WebRuntimeConfigurationError,
    validate_web_runtime,
)


def _authenticated(environment: str = "production") -> dict[str, str]:
    return {
        "APP_ENV": environment,
        "ALLOW_ANONYMOUS_WEB": "0",
        "WEB_AUTH_SECRET": "a" * 32,
        "WEB_GOOGLE_CLIENT_ID": "client-id",
        "WEB_GOOGLE_CLIENT_SECRET": "client-secret",
    }


def test_production_requires_complete_authentication() -> None:
    with pytest.raises(
        WebRuntimeConfigurationError,
        match="Production web startup blocked",
    ):
        validate_web_runtime(
            {"APP_ENV": "production", "ALLOW_ANONYMOUS_WEB": "0"}
        )


def test_production_ignores_anonymous_override() -> None:
    with pytest.raises(
        WebRuntimeConfigurationError,
        match="never permitted in production",
    ):
        validate_web_runtime(
            {"APP_ENV": "production", "ALLOW_ANONYMOUS_WEB": "1"}
        )


def test_complete_production_authentication_is_accepted() -> None:
    result = validate_web_runtime(_authenticated())

    assert result.environment == "production"
    assert result.authentication_enabled is True
    assert result.anonymous_enabled is False


def test_partial_authentication_configuration_is_rejected() -> None:
    with pytest.raises(
        WebRuntimeConfigurationError,
        match="WEB_GOOGLE_CLIENT_ID, WEB_GOOGLE_CLIENT_SECRET",
    ):
        validate_web_runtime(
            {
                "APP_ENV": "production",
                "ALLOW_ANONYMOUS_WEB": "0",
                "WEB_AUTH_SECRET": "a" * 32,
            }
        )


def test_weak_session_secret_is_rejected() -> None:
    environment = _authenticated()
    environment["WEB_AUTH_SECRET"] = "too-short"

    with pytest.raises(WebRuntimeConfigurationError, match="at least 32"):
        validate_web_runtime(environment)


def test_anonymous_development_requires_explicit_flag() -> None:
    with pytest.raises(
        WebRuntimeConfigurationError,
        match="Anonymous web startup blocked",
    ):
        validate_web_runtime(
            {"APP_ENV": "development", "ALLOW_ANONYMOUS_WEB": "0"}
        )


def test_explicit_anonymous_development_is_accepted() -> None:
    result = validate_web_runtime(
        {"APP_ENV": "development", "ALLOW_ANONYMOUS_WEB": "true"}
    )

    assert result.environment == "development"
    assert result.authentication_enabled is False
    assert result.anonymous_enabled is True


def test_environment_must_be_explicit() -> None:
    with pytest.raises(WebRuntimeConfigurationError, match="APP_ENV must be set"):
        validate_web_runtime({"ALLOW_ANONYMOUS_WEB": "1"})


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(WebRuntimeConfigurationError, match="Unsupported APP_ENV"):
        validate_web_runtime(
            {"APP_ENV": "stagingg", "ALLOW_ANONYMOUS_WEB": "1"}
        )


def test_invalid_anonymous_boolean_is_rejected() -> None:
    with pytest.raises(
        WebRuntimeConfigurationError,
        match="ALLOW_ANONYMOUS_WEB must be one of",
    ):
        validate_web_runtime(
            {"APP_ENV": "test", "ALLOW_ANONYMOUS_WEB": "sometimes"}
        )
