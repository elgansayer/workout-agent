"""Fail-safe browser and intermediary cache policy for the web application.

The web UI serves health data, coaching history, OAuth redirects, and credential
lifecycle endpoints. Those responses are private by default. Only explicitly
versioned static assets may opt into long-lived immutable caching.

The guard is installed from :mod:`webapp`'s security bootstrap so it wraps every
FastAPI response, including redirects, exception responses, streams, and mounted
static files. Keeping the policy outside route handlers prevents new endpoints
from accidentally becoming cacheable.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

_PRIVATE_NO_STORE = "private, no-store, max-age=0"
_PUBLIC_REVALIDATE = "public, no-cache, max-age=0, must-revalidate"
_PUBLIC_IMMUTABLE = "public, max-age=31536000, immutable"

_PUBLIC_ASSET_SUFFIXES = (
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".webmanifest",
)
_VERSION_QUERY_RE = re.compile(r"^[A-Fa-f0-9]{8,64}$")
# Angular/Vite style content hashes are commonly hexadecimal or base36/base64url
# chunks between separators, e.g. main-LWJRVJ2F.js or styles.a1b2c3d4.css.
_HASHED_FILENAME_RE = re.compile(
    r"(?:^|[._-])[A-Za-z0-9_-]{8,64}(?=\.(?:css|js|mjs|map|png|jpg|jpeg|gif|webp|svg|ico|woff|woff2|ttf)$)",
    re.IGNORECASE,
)

_ORIGINAL_FASTAPI_CALL: Callable[
    [FastAPI, Scope, Receive, Send], Awaitable[None]
] | None = None


def _path(scope: Scope) -> str:
    return str(scope.get("path") or "/")


def _is_public_asset(path: str) -> bool:
    if path.startswith("/api/"):
        return False
    if path.startswith(("/static/", "/assets/")):
        return True
    # Angular emits its hashed bundles at the web root. Do not classify nested
    # arbitrary downloads as public merely because they end in an image/script
    # suffix: a future /exports/user-chart.png must remain private by default.
    lowered = path.lower()
    return path.count("/") == 1 and lowered.endswith(_PUBLIC_ASSET_SUFFIXES)


def _has_version_query(scope: Scope) -> bool:
    raw = scope.get("query_string", b"")
    try:
        query = parse_qs(raw.decode("ascii"), keep_blank_values=True)
    except (UnicodeDecodeError, AttributeError):
        return False
    return any(_VERSION_QUERY_RE.fullmatch(value) for value in query.get("v", []))


def _is_explicitly_versioned_asset(scope: Scope) -> bool:
    path = _path(scope)
    if not _is_public_asset(path):
        return False
    if _has_version_query(scope):
        return True
    filename = path.rsplit("/", 1)[-1]
    return bool(_HASHED_FILENAME_RE.search(filename))


def _merge_vary(headers: MutableHeaders, *values: str) -> None:
    existing = headers.get("vary", "")
    ordered: list[str] = []
    seen: set[str] = set()
    for value in [*existing.split(","), *values]:
        item = value.strip()
        if not item:
            continue
        key = item.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(item)
    if ordered:
        headers["Vary"] = ", ".join(ordered)


def _apply_cache_policy(scope: Scope, message: Message) -> None:
    if message.get("type") != "http.response.start":
        return

    headers = MutableHeaders(scope=message)
    path = _path(scope)

    if _is_public_asset(path):
        headers["Cache-Control"] = (
            _PUBLIC_IMMUTABLE
            if _is_explicitly_versioned_asset(scope)
            else _PUBLIC_REVALIDATE
        )
        # Static assets are public and never contain per-user state. Remove
        # stale defensive headers a downstream response might have supplied.
        for header in ("Pragma", "Expires", "CDN-Cache-Control", "Surrogate-Control"):
            if header in headers:
                del headers[header]
        return

    headers["Cache-Control"] = _PRIVATE_NO_STORE
    headers["Pragma"] = "no-cache"
    headers["Expires"] = "0"
    headers["CDN-Cache-Control"] = "no-store"
    headers["Surrogate-Control"] = "no-store"
    _merge_vary(headers, "Cookie", "Authorization")


async def _guarded_fastapi_call(
    app: FastAPI,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    original = _ORIGINAL_FASTAPI_CALL
    if original is None:  # pragma: no cover - defensive import-order guard
        raise RuntimeError("Response cache guard was not initialised")
    if scope.get("type") != "http":
        await original(app, scope, receive, send)
        return

    async def guarded_send(message: Message) -> None:
        _apply_cache_policy(scope, message)
        await send(message)

    await original(app, scope, receive, guarded_send)


def install_response_cache_guard() -> None:
    """Install the process-wide cache guard once for FastAPI web responses."""

    global _ORIGINAL_FASTAPI_CALL
    if getattr(FastAPI, "_workout_cache_guard_installed", False):
        return
    _ORIGINAL_FASTAPI_CALL = FastAPI.__call__
    FastAPI.__call__ = _guarded_fastapi_call  # type: ignore[method-assign]
    FastAPI._workout_cache_guard_installed = True  # type: ignore[attr-defined]


__all__ = ["install_response_cache_guard"]
