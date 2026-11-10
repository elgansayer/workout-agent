"""The tenant-resolution policy: no shared fallback user outside local mode."""

from __future__ import annotations

import pytest
import tenancy
from tenancy import (
    LEGACY_TENANT_EMAIL,
    NON_PRODUCTION_ENVIRONMENTS,
    PRODUCTION_ENVIRONMENTS,
    implicit_single_user_allowed,
    normalise_email,
)

_TRUTHY = ("1", "true", "TRUE", "yes", "on", " 1 ", "On")


@pytest.mark.parametrize("app_env", sorted(PRODUCTION_ENVIRONMENTS))
@pytest.mark.parametrize("flag", _TRUTHY)
def test_production_never_allows_the_implicit_tenant(app_env: str, flag: str) -> None:
    assert not implicit_single_user_allowed(
        {"APP_ENV": app_env, "ALLOW_ANONYMOUS_WEB": flag}
    )


@pytest.mark.parametrize("environ", [{}, {"ALLOW_ANONYMOUS_WEB": "1"}, {"APP_ENV": ""}])
def test_unset_app_env_fails_closed(environ: dict[str, str]) -> None:
    assert not implicit_single_user_allowed({**environ, "ALLOW_ANONYMOUS_WEB": "1"})


@pytest.mark.parametrize("app_env", ["staging", "qa", "prod ", "Production!", "devel"])
def test_unrecognised_app_env_is_treated_like_production(app_env: str) -> None:
    allowed = implicit_single_user_allowed(
        {"APP_ENV": app_env, "ALLOW_ANONYMOUS_WEB": "1"}
    )
    # ``"prod "`` is recognised as production after trimming; the rest are unknown.
    assert allowed is False


@pytest.mark.parametrize("flag", ["", "0", "false", "no", "off", "maybe", "2", "y"])
@pytest.mark.parametrize("app_env", sorted(NON_PRODUCTION_ENVIRONMENTS))
def test_local_mode_needs_the_explicit_anonymous_flag(app_env: str, flag: str) -> None:
    assert not implicit_single_user_allowed(
        {"APP_ENV": app_env, "ALLOW_ANONYMOUS_WEB": flag}
    )


@pytest.mark.parametrize("app_env", sorted(NON_PRODUCTION_ENVIRONMENTS))
@pytest.mark.parametrize("flag", _TRUTHY)
def test_local_mode_requires_both_gates(app_env: str, flag: str) -> None:
    assert implicit_single_user_allowed(
        {"APP_ENV": app_env, "ALLOW_ANONYMOUS_WEB": flag}
    )


def test_environment_names_are_trimmed_and_case_insensitive() -> None:
    assert implicit_single_user_allowed(
        {"APP_ENV": "  TeSt ", "ALLOW_ANONYMOUS_WEB": " TRUE "}
    )
    assert not implicit_single_user_allowed(
        {"APP_ENV": " PRODUCTION ", "ALLOW_ANONYMOUS_WEB": "1"}
    )


def test_policy_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    assert implicit_single_user_allowed()
    monkeypatch.setenv("APP_ENV", "production")
    assert not implicit_single_user_allowed()
    monkeypatch.delenv("APP_ENV")
    assert not implicit_single_user_allowed()


def test_policy_agrees_with_the_web_runtime_boundary() -> None:
    """Persistence and the web boundary must not disagree about "production"."""
    from webapp import runtime_security

    assert (
        frozenset(runtime_security._PRODUCTION_ENVIRONMENTS) == PRODUCTION_ENVIRONMENTS
    )
    assert (
        frozenset(runtime_security._NON_PRODUCTION_ENVIRONMENTS)
        == NON_PRODUCTION_ENVIRONMENTS
    )
    assert frozenset(runtime_security._TRUE_VALUES) == tenancy._TRUE_VALUES
    assert not PRODUCTION_ENVIRONMENTS & NON_PRODUCTION_ENVIRONMENTS


def test_normalise_email_is_the_single_canonical_form() -> None:
    assert normalise_email("  Person@Example.COM ") == "person@example.com"
    assert normalise_email("a@b") == "a@b"


def test_the_placeholder_address_is_reserved_and_stable() -> None:
    # The schema triggers hard-code this literal; renaming it is a migration.
    assert LEGACY_TENANT_EMAIL == "legacy@local"
    assert normalise_email(LEGACY_TENANT_EMAIL) == LEGACY_TENANT_EMAIL


def test_tenant_required_error_is_a_runtime_error() -> None:
    assert issubclass(tenancy.TenantRequiredError, RuntimeError)
