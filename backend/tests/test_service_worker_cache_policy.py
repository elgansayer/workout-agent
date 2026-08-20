"""Regression tests for tenant-safe service-worker caching."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKERS = (
    (ROOT / "backend/webapp/static/sw.js", "/static/offline.html"),
    (ROOT / "frontend/public/sw.js", "/offline.html"),
)
OFFLINE_SHELLS = (
    ROOT / "backend/webapp/static/offline.html",
    ROOT / "frontend/public/offline.html",
)
REQUIRED_NETWORK_ONLY_PREFIXES = (
    "/api/",
    "/settings",
    "/chat",
    "/history",
    "/metrics",
    "/exports",
    "/login",
    "/logout",
    "/auth",
    "/google-health/",
)


@pytest.mark.parametrize(("worker_path", "offline_url"), WORKERS)
def test_worker_precaches_only_the_safe_offline_shell(
    worker_path: Path,
    offline_url: str,
) -> None:
    source = worker_path.read_text(encoding="utf-8")

    assert f'const OFFLINE_URL = "{offline_url}";' in source
    assert "const PRECACHE = [OFFLINE_URL];" in source
    assert "cache.addAll(PRECACHE)" in source
    assert 'const SHELL = ["/"' not in source


@pytest.mark.parametrize(("worker_path", "offline_url"), WORKERS)
def test_navigation_is_network_first_and_never_written_to_cache(
    worker_path: Path,
    offline_url: str,
) -> None:
    source = worker_path.read_text(encoding="utf-8")
    navigation_guard = source.index('if (request.mode === "navigate")')
    sensitive_guard = source.index("if (isSensitivePath(url.pathname)) return;")
    cache_write = source.index("cache.put(request, copy)")

    assert navigation_guard < sensitive_guard < cache_write
    assert "fetch(request).catch" in source[navigation_guard:sensitive_guard]
    assert "caches.match(OFFLINE_URL)" in source[navigation_guard:sensitive_guard]
    assert offline_url in source


@pytest.mark.parametrize(("worker_path", "offline_url"), WORKERS)
def test_personalised_and_auth_routes_are_explicitly_network_only(
    worker_path: Path,
    offline_url: str,
) -> None:
    del offline_url
    source = worker_path.read_text(encoding="utf-8")
    sensitive_guard = source.index("if (isSensitivePath(url.pathname)) return;")
    cache_write = source.index("cache.put(request, copy)")

    for prefix in REQUIRED_NETWORK_ONLY_PREFIXES:
        assert f'"{prefix}"' in source
    assert sensitive_guard < cache_write


@pytest.mark.parametrize(("worker_path", "offline_url"), WORKERS)
def test_runtime_cache_accepts_only_content_hashed_static_assets(
    worker_path: Path,
    offline_url: str,
) -> None:
    del offline_url
    source = worker_path.read_text(encoding="utf-8")

    assert "const VERSIONED_ASSET_RE" in source
    assert "{8,}" in source
    assert "url.origin === self.location.origin" in source
    assert "if (!isVersionedStaticAsset(url)) return;" in source
    assert source.index("if (!isVersionedStaticAsset(url)) return;") < source.index(
        "cache.put(request, copy)"
    )


@pytest.mark.parametrize(("worker_path", "offline_url"), WORKERS)
def test_activation_purges_all_previous_workout_agent_caches(
    worker_path: Path,
    offline_url: str,
) -> None:
    del offline_url
    source = worker_path.read_text(encoding="utf-8")

    assert 'const CACHE_PREFIX = "workout-agent-";' in source
    assert 'const CACHE = "workout-agent-static-v3";' in source
    assert "key.startsWith(CACHE_PREFIX) && key !== CACHE" in source
    assert ".map((key) => caches.delete(key))" in source


@pytest.mark.parametrize("shell_path", OFFLINE_SHELLS)
def test_offline_shell_contains_no_personalised_state(shell_path: Path) -> None:
    shell = shell_path.read_text(encoding="utf-8")

    assert 'name="workout-agent-offline-shell" content="safe-static"' in shell
    assert "Personalised workout, health, account, and credential data" in shell
    assert "{{" not in shell
    assert "api_key" not in shell
    assert "user_id" not in shell
