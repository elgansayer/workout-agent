"""HTTP security headers and CSP nonce injection for the public web app.

The middleware is deliberately ASGI-only so the policy can wrap every response,
including responses produced by mounted static applications. HTML responses are
buffered just long enough to attach the per-request nonce to Angular's root and
any script/style elements before the response is sent.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[dict[str, Any], Receive, Send], Awaitable[None]]

_CSP_NONCE_ATTRIBUTE = re.compile(r"\bnonce\s*=", re.IGNORECASE)
_APP_ROOT = re.compile(r"<app-root\b([^>]*)>", re.IGNORECASE)
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b([^>]*)>", re.IGNORECASE)

# Angular templates currently contain a small number of style bindings. CSP
# nonces cover Angular-created <style> elements via ngCspNonce, while this
# narrowly-scoped CSP3 exception keeps style="..." / [style.*] bindings working.
# It intentionally does NOT permit inline script execution.
STYLE_ATTRIBUTE_EXCEPTION = "'unsafe-inline'"


def generate_csp_nonce() -> str:
    """Return a URL-safe, cryptographically-random nonce for one HTTP response."""

    return secrets.token_urlsafe(24)


def build_content_security_policy(nonce: str) -> str:
    """Build the production CSP for a single response nonce."""

    if not nonce or any(ch.isspace() for ch in nonce):
        raise ValueError("CSP nonce must be a non-empty token without whitespace")

    directives = (
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        "script-src-attr 'none'",
        f"style-src 'self' 'nonce-{nonce}'",
        f"style-src-elem 'self' 'nonce-{nonce}'",
        f"style-src-attr {STYLE_ATTRIBUTE_EXCEPTION}",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "manifest-src 'self'",
        "worker-src 'self' blob:",
        "frame-src 'none'",
        "upgrade-insecure-requests",
    )
    return "; ".join(directives)


def security_headers(nonce: str) -> dict[str, str]:
    """Return the baseline defence-in-depth headers for one response."""

    return {
        "Content-Security-Policy": build_content_security_policy(nonce),
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()"
        ),
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-site",
        "Cross-Origin-Embedder-Policy": "credentialless",
        "X-Permitted-Cross-Domain-Policies": "none",
    }


def inject_csp_nonce(html: str, nonce: str) -> str:
    """Attach the CSP nonce to Angular and any inline script/style elements."""

    if not nonce:
        raise ValueError("nonce is required")

    def app_root(match: re.Match[str]) -> str:
        attrs = match.group(1)
        if re.search(r"\bngcspnonce\s*=", attrs, re.IGNORECASE):
            return match.group(0)
        return f'<app-root ngCspNonce="{nonce}"{attrs}>'

    def executable_tag(match: re.Match[str]) -> str:
        tag = match.group(1)
        attrs = match.group(2)
        if _CSP_NONCE_ATTRIBUTE.search(attrs):
            return match.group(0)
        return f'<{tag} nonce="{nonce}"{attrs}>'

    html = _APP_ROOT.sub(app_root, html, count=1)
    return _SCRIPT_OR_STYLE.sub(executable_tag, html)


def _header_value(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    target = name.lower()
    for key, value in headers:
        if key.lower() == target:
            return value
    return None


def _replace_headers(
    headers: Iterable[tuple[bytes, bytes]], replacements: dict[str, str]
) -> list[tuple[bytes, bytes]]:
    replacement_names = {name.lower().encode("ascii") for name in replacements}
    result = [
        (key, value) for key, value in headers if key.lower() not in replacement_names
    ]
    result.extend(
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in replacements.items()
    )
    return result


def _replace_content_length(
    headers: Iterable[tuple[bytes, bytes]], length: int
) -> list[tuple[bytes, bytes]]:
    result = [(key, value) for key, value in headers if key.lower() != b"content-length"]
    result.append((b"content-length", str(length).encode("ascii")))
    return result


def _transform_html(raw_body: bytes, nonce: str) -> bytes:
    try:
        return inject_csp_nonce(raw_body.decode("utf-8"), nonce).encode("utf-8")
    except UnicodeDecodeError:
        return raw_body


class SecurityHeadersMiddleware:
    """Apply security headers and a request-specific CSP nonce to HTTP responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        nonce = generate_csp_nonce()
        scope.setdefault("state", {})["csp_nonce"] = nonce
        pending_start: dict[str, Any] | None = None
        buffer_html = False
        body_chunks: list[bytes] = []

        async def send_transformed_html(raw_body: bytes, body_message: dict[str, Any]) -> None:
            nonlocal pending_start
            transformed = _transform_html(raw_body, nonce)
            assert pending_start is not None
            pending_start["headers"] = _replace_content_length(
                pending_start.get("headers", []), len(transformed)
            )
            await send(pending_start)
            pending_start = None
            await send(
                {
                    "type": "http.response.body",
                    "body": transformed,
                    "more_body": False,
                }
            )

        async def secure_send(message: dict[str, Any]) -> None:
            nonlocal pending_start, buffer_html

            if message["type"] == "http.response.start":
                headers = _replace_headers(
                    message.get("headers", []), security_headers(nonce)
                )
                pending_start = {**message, "headers": headers}
                content_type = (_header_value(headers, b"content-type") or b"").lower()
                content_encoding = _header_value(headers, b"content-encoding")
                buffer_html = (
                    scope.get("method", "GET").upper() != "HEAD"
                    and content_type.startswith(b"text/html")
                    and content_encoding is None
                )
                if not buffer_html:
                    await send(pending_start)
                    pending_start = None
                return

            if buffer_html and message["type"] == "http.response.pathsend":
                # Starlette can use the ASGI pathsend extension for FileResponse.
                # Read only HTML here so the nonce can be injected before bytes
                # leave the process; non-HTML FileResponses are never buffered.
                raw_body = Path(message["path"]).read_bytes()
                await send_transformed_html(raw_body, message)
                return

            if message["type"] != "http.response.body" or not buffer_html:
                await send(message)
                return

            body_chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return

            await send_transformed_html(b"".join(body_chunks), message)

        await self.app(scope, receive, secure_send)
