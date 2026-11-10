"""Startup regression tests for the fail-closed production web entrypoint."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _run_entrypoint(
    tmp_path: Path,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WEB_", "ALLOW_ANONYMOUS_WEB", "APP_ENV"))
    }
    environment.update(
        {
            "DATABASE_PATH": str(tmp_path / "startup-security.db"),
            "PYTHONPATH": str(_BACKEND_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    environment.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", "import webapp.secure_app"],
        cwd=_BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def test_production_without_authentication_refuses_to_start(tmp_path: Path) -> None:
    result = _run_entrypoint(
        tmp_path,
        APP_ENV="production",
        WEB_PUBLIC_URL="https://workout.example",
    )

    assert result.returncode != 0
    assert "Production web startup blocked" in result.stderr


def test_production_with_partial_authentication_refuses_to_start(
    tmp_path: Path,
) -> None:
    result = _run_entrypoint(
        tmp_path,
        APP_ENV="production",
        WEB_PUBLIC_URL="https://workout.example",
        WEB_AUTH_SECRET="test-secret-that-is-long-enough-for-session-signing",
    )

    assert result.returncode != 0
    assert "Incomplete web authentication configuration" in result.stderr


def test_production_with_weak_session_secret_refuses_to_start(
    tmp_path: Path,
) -> None:
    result = _run_entrypoint(
        tmp_path,
        APP_ENV="production",
        WEB_PUBLIC_URL="https://workout.example",
        WEB_AUTH_SECRET="too-short",
        WEB_GOOGLE_CLIENT_ID="startup-client",
        WEB_GOOGLE_CLIENT_SECRET="startup-secret",
    )

    assert result.returncode != 0
    assert "at least 32 characters" in result.stderr


def test_local_startup_requires_explicit_anonymous_opt_in(tmp_path: Path) -> None:
    result = _run_entrypoint(
        tmp_path,
        APP_ENV="development",
        ALLOW_ANONYMOUS_WEB="0",
    )

    assert result.returncode != 0
    assert "Anonymous web startup blocked" in result.stderr


def test_complete_production_configuration_starts(tmp_path: Path) -> None:
    result = _run_entrypoint(
        tmp_path,
        APP_ENV="production",
        WEB_PUBLIC_URL="https://workout.example",
        WEB_AUTH_SECRET="test-secret-that-is-long-enough-for-session-signing",
        WEB_GOOGLE_CLIENT_ID="startup-client",
        WEB_GOOGLE_CLIENT_SECRET="startup-secret",
    )

    assert result.returncode == 0, result.stderr
