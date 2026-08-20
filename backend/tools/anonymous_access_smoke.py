#!/usr/bin/env python3
"""Probe the deployed app's route inventory without authenticating.

The script imports the local FastAPI app only to discover the canonical route
inventory.  It then probes every personalised route on the target hostname with
GET when available, otherwise HEAD.  Using HEAD for mutation-only routes verifies
that the authentication middleware fails closed without risking a state change.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute

PUBLIC_EXACT_PATHS = {
    "/login",
    "/login/google",
    "/logout",
    "/auth",
    "/google-health/callback",
    "/favicon.ico",
    "/sw.js",
}
PUBLIC_PREFIXES = ("/static/", "/assets/")
PUBLIC_SUFFIXES = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".map",
    ".json",
    ".webmanifest",
)
SENSITIVE_MARKERS = (
    "api_key",
    "refresh_token",
    "access_token",
    "workout_history",
    "body_fat_pct",
    "personal_records",
    "chat_messages",
)
DENIAL_STATUSES = {301, 302, 303, 307, 308, 401, 403}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _is_public(path: str) -> bool:
    return (
        path in PUBLIC_EXACT_PATHS
        or path.startswith(PUBLIC_PREFIXES)
        or path.endswith(PUBLIC_SUFFIXES)
    )


def _representative_path(path: str) -> str:
    def replace(match: re.Match[str]) -> str:
        spec = match.group(1)
        _name, _, converter = spec.partition(":")
        if converter in {"int", "float"}:
            return "1"
        if converter == "path":
            return "sample"
        return "test"

    return re.sub(r"\{([^{}]+)\}", replace, path)


def _load_routes() -> list[APIRoute]:
    repo_root = Path(__file__).resolve().parents[2]
    (repo_root / "frontend" / "dist" / "frontend" / "browser").mkdir(
        parents=True,
        exist_ok=True,
    )
    os.environ.setdefault("APP_ENV", "production")
    os.environ.setdefault("ALLOW_ANONYMOUS_WEB", "0")
    os.environ.setdefault("WEB_AUTH_SECRET", "anonymous-smoke-local-route-discovery-secret")
    os.environ.setdefault("WEB_GOOGLE_CLIENT_ID", "anonymous-smoke-route-discovery-client")
    os.environ.setdefault("WEB_GOOGLE_CLIENT_SECRET", "anonymous-smoke-route-discovery-secret")

    import webapp.app as app_module

    importlib.reload(app_module)
    return [route for route in app_module.app.routes if isinstance(route, APIRoute)]


def _probe(base_url: str, timeout: float) -> list[str]:
    opener = urllib.request.build_opener(_NoRedirect())
    failures: list[str] = []
    checked = 0

    for route in _load_routes():
        if _is_public(route.path):
            continue

        path = _representative_path(route.path)
        methods = route.methods or {"GET"}
        method = "GET" if "GET" in methods else "HEAD"
        url = f"{base_url.rstrip('/')}{path}"
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.1",
                "User-Agent": "workout-agent-anonymous-route-smoke/1",
            },
        )
        try:
            response = opener.open(request, timeout=timeout)
            status = response.status
            body = response.read(65536).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read(65536).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - report the exact network failure
            failures.append(f"{method} {route.path}: request failed: {exc}")
            continue

        checked += 1
        if status not in DENIAL_STATUSES:
            failures.append(
                f"{method} {route.path}: returned {status}, expected an anonymous denial"
            )
            continue

        lowered = body.lower()
        leaked = [marker for marker in SENSITIVE_MARKERS if marker in lowered]
        if leaked:
            failures.append(
                f"{method} {route.path}: denial body contained sensitive markers: {', '.join(leaked)}"
            )

    print(f"Checked {checked} personalised route paths against {base_url}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    failures = _probe(args.base_url, args.timeout)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("Anonymous access smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
