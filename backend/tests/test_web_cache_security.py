"""Regression tests for personalised-response and credential cache safety."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from starlette.datastructures import Headers

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_ROOT.parent
_CACHE_MODULE_PATH = _BACKEND_ROOT / "webapp" / "cache_security.py"


def _load_cache_module() -> ModuleType:
    """Load the policy without importing webapp's runtime bootstrap."""

    spec = importlib.util.spec_from_file_location(
        "_cache_security_under_test",
        _CACHE_MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cache_security = _load_cache_module()


def _headers_for(
    path: str,
    *,
    status: int = 200,
    query: bytes = b"",
    initial_headers: list[tuple[bytes, bytes]] | None = None,
) -> Headers:
    scope: dict[str, Any] = {
        "type": "http",
        "path": path,
        "query_string": query,
    }
    message: dict[str, Any] = {
        "type": "http.response.start",
        "status": status,
        "headers": list(initial_headers or []),
    }
    cache_security._apply_cache_policy(scope, message)
    return Headers(raw=message["headers"])


@pytest.mark.parametrize(
    "path",
    [
        "/",  # dashboard shell
        "/settings",
        "/chat",  # coach UI
        "/history",
        "/exports/workouts.csv",
        "/api/dashboard",
        "/api/settings",
        "/api/chat/history",
    ],
)
def test_personalised_surfaces_are_private_no_store(path: str) -> None:
    headers = _headers_for(path)

    assert headers["cache-control"] == "private, no-store, max-age=0"
    assert headers["pragma"] == "no-cache"
    assert headers["expires"] == "0"
    assert headers["cdn-cache-control"] == "no-store"
    assert headers["surrogate-control"] == "no-store"
    vary = {item.strip().lower() for item in headers["vary"].split(",")}
    assert {"cookie", "authorization"} <= vary


def test_error_responses_are_private_no_store() -> None:
    headers = _headers_for(
        "/api/missing",
        status=404,
        initial_headers=[(b"vary", b"Origin")],
    )

    assert headers["cache-control"] == "private, no-store, max-age=0"
    vary = {item.strip().lower() for item in headers["vary"].split(",")}
    assert vary == {"origin", "cookie", "authorization"}


@pytest.mark.parametrize(
    "path",
    [
        "/login",
        "/login/google",
        "/logout",
        "/auth",
        "/api/settings/key",
        "/api/settings/key/delete",
        "/api/settings/verify-hevy",
        "/google-health/connect",
        "/google-health/callback",
        "/google-health/disconnect",
    ],
)
def test_credential_lifecycle_responses_are_never_cacheable(path: str) -> None:
    headers = _headers_for(path)
    assert "no-store" in headers["cache-control"]
    assert headers["cdn-cache-control"] == "no-store"


def test_only_explicitly_versioned_static_assets_are_immutable() -> None:
    assert (
        _headers_for("/static/style.css", query=b"v=deadbeef")["cache-control"]
        == "public, max-age=31536000, immutable"
    )
    assert (
        _headers_for("/assets/main-LWJRVJ2F.js")["cache-control"]
        == "public, max-age=31536000, immutable"
    )
    assert (
        _headers_for("/assets/logo-deadbeef.svg")["cache-control"]
        == "public, max-age=31536000, immutable"
    )
    assert (
        _headers_for("/static/style.css")["cache-control"]
        == "public, no-cache, max-age=0, must-revalidate"
    )
    assert (
        _headers_for("/sw.js")["cache-control"]
        == "public, no-cache, max-age=0, must-revalidate"
    )
    assert (
        _headers_for("/service-worker.js")["cache-control"]
        == "public, no-cache, max-age=0, must-revalidate"
    )
    assert (
        _headers_for("/workout-dashboard.js")["cache-control"]
        == "public, no-cache, max-age=0, must-revalidate"
    )


def test_nested_user_download_is_not_mistaken_for_public_static_asset() -> None:
    headers = _headers_for("/exports/user-chart.png")
    assert headers["cache-control"] == "private, no-store, max-age=0"


def test_web_security_bootstrap_installs_response_cache_guard() -> None:
    bootstrap = (_BACKEND_ROOT / "webapp" / "__init__.py").read_text(encoding="utf-8")
    assert "from webapp.cache_security import install_response_cache_guard" in bootstrap
    assert "install_response_cache_guard()" in bootstrap


def test_settings_api_returns_masks_not_plaintext_keys() -> None:
    """The settings response may expose only presence + a short masked suffix."""

    app_path = _BACKEND_ROOT / "webapp" / "app.py"
    source = app_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "api_settings"
    )
    segment = ast.get_source_segment(source, function)
    assert segment is not None

    assert 'masked = f"••••••••{key_str[-4:]}"' in segment
    assert 'user_keys[p] = {"has_key": True, "masked": masked}' in segment
    # The plaintext key is read only to derive the mask; it must not be placed
    # in the response mapping under an api_key/key/value field.
    assert '"api_key": key_str' not in segment
    assert '"key": key_str' not in segment
    assert '"value": key_str' not in segment


def test_settings_form_never_hydrates_password_inputs_from_server_masks() -> None:
    """Masks are placeholders only; password models remain empty on page load."""

    component_path = (
        _REPO_ROOT / "frontend" / "src" / "app" / "components" / "settings" / "settings.ts"
    )
    template_path = component_path.with_name("settings.html")
    component = component_path.read_text(encoding="utf-8")
    template = template_path.read_text(encoding="utf-8")

    load_settings = component.split("  loadSettings() {", 1)[1].split(
        "  selectProvider(", 1
    )[0]
    assert "hevyKeyInput =" not in load_settings
    assert "aiKeyInputs[" not in load_settings
    assert "hevyKeyInput = '';" in component
    assert "this.aiKeyInputs[provider] = '';" in component

    assert '[placeholder]="data.user_keys?.hevy?.masked' in template
    assert '[(ngModel)]="hevyKeyInput"' in template
    assert '[placeholder]="data.user_keys?.[p.id]?.masked' in template
    assert '[(ngModel)]="aiKeyInputs[p.id]"' in template
