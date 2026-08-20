#!/usr/bin/env python3
"""Validate the deployed HTTP security-header baseline.

Uses only the Python standard library so the check can run from CI or an
operator shell without installing project dependencies.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping

DEFAULT_URL = "https://workout.elgansayer.com/"
_NONCE_RE = re.compile(r"'nonce-[A-Za-z0-9_-]+'")
_REQUIRED_EXACT = {
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-site",
    "Cross-Origin-Embedder-Policy": "credentialless",
    "X-Permitted-Cross-Domain-Policies": "none",
}
_REQUIRED_CSP_DIRECTIVES = (
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "script-src-attr 'none'",
    "style-src-attr 'unsafe-inline'",
    "upgrade-insecure-requests",
)
_REQUIRED_PERMISSION_DENIES = (
    "camera=()",
    "geolocation=()",
    "microphone=()",
    "payment=()",
)


def validate_headers(headers: Mapping[str, str]) -> list[str]:
    """Return human-readable policy failures for a response header mapping."""

    failures: list[str] = []
    lower = {key.lower(): value for key, value in headers.items()}

    for name, expected in _REQUIRED_EXACT.items():
        actual = lower.get(name.lower())
        if actual != expected:
            failures.append(f"{name}: expected {expected!r}, got {actual!r}")

    hsts = lower.get("strict-transport-security", "")
    if "max-age=" not in hsts or "includesubdomains" not in hsts.lower():
        failures.append("Strict-Transport-Security must set max-age and includeSubDomains")

    permissions = lower.get("permissions-policy", "")
    for directive in _REQUIRED_PERMISSION_DENIES:
        if directive not in permissions:
            failures.append(f"Permissions-Policy is missing {directive}")

    csp = lower.get("content-security-policy", "")
    if not csp:
        failures.append("Content-Security-Policy is missing")
        return failures

    for directive in _REQUIRED_CSP_DIRECTIVES:
        if directive not in csp:
            failures.append(f"Content-Security-Policy is missing {directive!r}")

    script_directive = next(
        (part.strip() for part in csp.split(";") if part.strip().startswith("script-src ")),
        "",
    )
    if not _NONCE_RE.search(script_directive):
        failures.append("script-src must contain a per-response nonce")
    if "'unsafe-inline'" in script_directive:
        failures.append("script-src must not allow unsafe-inline")

    style_directive = next(
        (part.strip() for part in csp.split(";") if part.strip().startswith("style-src ")),
        "",
    )
    if not _NONCE_RE.search(style_directive):
        failures.append("style-src must contain a per-response nonce")
    if "'unsafe-inline'" in style_directive:
        failures.append("style-src must not broadly allow unsafe-inline")

    return failures


def fetch_headers(url: str, timeout: float) -> tuple[int, Mapping[str, str]]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "workout-agent-security-header-check/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items())


def check(url: str, timeout: float) -> list[str]:
    status, headers = fetch_headers(url, timeout)
    failures = validate_headers(headers)
    if status >= 500:
        failures.insert(0, f"deployment returned HTTP {status}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--delay", type=float, default=5.0)
    args = parser.parse_args(argv)

    if args.attempts < 1:
        parser.error("--attempts must be at least 1")

    last_failures: list[str] = []
    for attempt in range(1, args.attempts + 1):
        try:
            last_failures = check(args.url, args.timeout)
        except (OSError, urllib.error.URLError) as exc:
            last_failures = [f"request failed: {exc}"]

        if not last_failures:
            print(f"security header check passed: {args.url}")
            return 0

        if attempt < args.attempts:
            print(
                f"attempt {attempt}/{args.attempts} failed; retrying in {args.delay:g}s",
                file=sys.stderr,
            )
            time.sleep(args.delay)

    print(f"security header check failed: {args.url}", file=sys.stderr)
    for failure in last_failures:
        print(f"- {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
