"""Regression tests for the fail-closed browser session cookie policy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
_BACKEND_ROOT = Path(__file__).resolve().parents[1]

_INSPECT_MIDDLEWARE = """
import json
import webapp.app as app_module
from starlette.middleware.sessions import SessionMiddleware

middleware = next(
    item
    for item in app_module.app.user_middleware
    if isinstance(item.cls, type) and issubclass(item.cls, SessionMiddleware)
)
options = middleware.kwargs
config = options.get("config")
if config is not None:
    cookie = config.cookie
    result = {
        "https_only": cookie.secure,
        "same_site": cookie.same_site,
        "path": cookie.path,
        "session_cookie": cookie.name,
        "max_age": cookie.max_age,
    }
else:
    result = {
        "https_only": options["https_only"],
        "same_site": options["same_site"],
        "path": options["path"],
        "session_cookie": options["session_cookie"],
        "max_age": options["max_age"],
    }
print(json.dumps(result))
"""


def _session_cookie_options(
    tmp_path: Path,
    **overrides: str,
) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WEB_", "ALLOW_ANONYMOUS_WEB", "APP_ENV"))
    }
    environment.update(
        {
            "DATABASE_PATH": str(tmp_path / "session-cookie-test.db"),
            "PYTHONPATH": str(_BACKEND_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    environment.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", _INSPECT_MIDDLEWARE],
        cwd=_BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_production_session_cookie_is_secure_and_bounded(tmp_path: Path) -> None:
    options = _session_cookie_options(
        tmp_path,
        APP_ENV="production",
        ALLOW_ANONYMOUS_WEB="0",
        WEB_PUBLIC_URL="https://workout.example",
        WEB_AUTH_SECRET="test-secret-that-is-long-enough-for-session-signing",
        WEB_GOOGLE_CLIENT_ID="session-cookie-test-client",
        WEB_GOOGLE_CLIENT_SECRET="session-cookie-test-secret",
    )

    assert options["https_only"] is True
    assert options["same_site"] == "lax"
    assert options["path"] == "/"
    assert options["session_cookie"] in {"session", "__Host-session"}
    assert options["max_age"] == SESSION_MAX_AGE_SECONDS


def test_local_anonymous_session_cookie_remains_usable_over_http(
    tmp_path: Path,
) -> None:
    options = _session_cookie_options(
        tmp_path,
        APP_ENV="development",
        ALLOW_ANONYMOUS_WEB="1",
    )

    assert options["https_only"] is False
    assert options["same_site"] == "lax"


def test_csrf_signer_lifetime_matches_the_browser_cookie() -> None:
    from webapp.csrf_security import SESSION_MAX_AGE_SECONDS as CSRF_SESSION_MAX_AGE

    assert CSRF_SESSION_MAX_AGE == SESSION_MAX_AGE_SECONDS
