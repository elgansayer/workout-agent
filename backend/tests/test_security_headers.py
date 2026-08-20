from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ALLOW_ANONYMOUS_WEB", "1")

from scripts.check_security_headers import validate_headers
from webapp.security_headers import (
    SecurityHeadersMiddleware,
    build_content_security_policy,
    inject_csp_nonce,
    security_headers,
)


class SecurityHeaderPolicyTests(unittest.TestCase):
    def test_csp_is_nonce_based_and_inline_script_is_forbidden(self) -> None:
        csp = build_content_security_policy("fixed-test-nonce")

        self.assertIn("script-src 'self' 'nonce-fixed-test-nonce'", csp)
        self.assertIn("style-src 'self' 'nonce-fixed-test-nonce'", csp)
        self.assertIn("script-src-attr 'none'", csp)
        self.assertIn("style-src-attr 'unsafe-inline'", csp)
        script_src = next(
            directive.strip()
            for directive in csp.split(";")
            if directive.strip().startswith("script-src ")
        )
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_nonce_is_injected_into_angular_root_script_and_style(self) -> None:
        html = (
            '<html><body><app-root class="shell"></app-root>'
            '<script src="main.js"></script><style>.ready{display:block}</style>'
            "</body></html>"
        )

        rendered = inject_csp_nonce(html, "abc123")

        self.assertIn('<app-root ngCspNonce="abc123" class="shell">', rendered)
        self.assertIn('<script nonce="abc123" src="main.js">', rendered)
        self.assertIn('<style nonce="abc123">', rendered)

    def test_existing_nonce_attributes_are_not_replaced(self) -> None:
        html = '<app-root ngCspNonce="existing"></app-root><script nonce="existing"></script>'
        rendered = inject_csp_nonce(html, "new")
        self.assertEqual(rendered, html)

    def test_smoke_validator_accepts_generated_baseline(self) -> None:
        headers = security_headers("fixed-test-nonce")
        self.assertEqual(validate_headers(headers), [])

    def test_smoke_validator_rejects_unsafe_inline_script(self) -> None:
        headers = security_headers("fixed-test-nonce")
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "script-src 'self'", "script-src 'self' 'unsafe-inline'"
        )
        failures = validate_headers(headers)
        self.assertTrue(
            any("script-src must not allow unsafe-inline" in item for item in failures)
        )


class SecurityHeadersMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def test_html_response_gets_headers_and_matching_body_nonce(self) -> None:
        async def app(scope, receive, send):
            body = (
                b'<html><body><app-root></app-root>'
                b'<script src="/main.js"></script></body></html>'
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/html; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send(
                {"type": "http.response.body", "body": body[:30], "more_body": True}
            )
            await send(
                {"type": "http.response.body", "body": body[30:], "more_body": False}
            )

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        middleware = SecurityHeadersMiddleware(app)
        scope = {"type": "http", "method": "GET", "path": "/", "state": {}}
        await middleware(scope, self._receive, send)

        self.assertEqual(
            [message["type"] for message in messages],
            ["http.response.start", "http.response.body"],
        )
        start, body_message = messages
        headers = {
            key.decode("ascii"): value.decode("ascii") for key, value in start["headers"]
        }
        csp = headers["content-security-policy"]
        match = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
        self.assertIsNotNone(match)
        nonce = match.group(1)
        body = body_message["body"].decode("utf-8")
        self.assertIn(f'ngCspNonce="{nonce}"', body)
        self.assertIn(f'<script nonce="{nonce}"', body)
        self.assertEqual(int(headers["content-length"]), len(body_message["body"]))
        self.assertEqual(
            headers["strict-transport-security"],
            "max-age=31536000; includeSubDomains",
        )
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")

    async def test_file_response_pathsend_is_converted_to_nonce_html(self) -> None:
        source = (
            b'<html><body><app-root></app-root>'
            b'<script src="main.js"></script></body></html>'
        )
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.html"
            index.write_bytes(source)

            async def app(scope, receive, send):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"text/html; charset=utf-8"),
                            (b"content-length", str(len(source)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.pathsend", "path": str(index)})

            messages: list[dict] = []

            async def send(message):
                messages.append(message)

            await SecurityHeadersMiddleware(app)(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/",
                    "state": {},
                    "extensions": {"http.response.pathsend": {}},
                },
                self._receive,
                send,
            )

        self.assertEqual(
            [message["type"] for message in messages],
            ["http.response.start", "http.response.body"],
        )
        headers = {
            key.decode("ascii"): value.decode("ascii")
            for key, value in messages[0]["headers"]
        }
        nonce = re.search(
            r"'nonce-([A-Za-z0-9_-]+)'", headers["content-security-policy"]
        ).group(1)
        body = messages[1]["body"].decode("utf-8")
        self.assertIn(f'ngCspNonce="{nonce}"', body)
        self.assertIn(f'<script nonce="{nonce}"', body)

    async def test_head_html_keeps_file_content_length_and_has_headers(self) -> None:
        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/html; charset=utf-8"),
                        (b"content-length", b"123"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        await SecurityHeadersMiddleware(app)(
            {"type": "http", "method": "HEAD", "path": "/", "state": {}},
            self._receive,
            send,
        )
        headers = {
            key.decode("ascii"): value.decode("ascii")
            for key, value in messages[0]["headers"]
        }
        self.assertEqual(headers["content-length"], "123")
        self.assertIn("content-security-policy", headers)

    async def test_non_html_body_is_not_buffered_or_changed(self) -> None:
        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"detail":"no"}'})

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        await SecurityHeadersMiddleware(app)(
            {"type": "http", "method": "GET", "path": "/api/private", "state": {}},
            self._receive,
            send,
        )

        self.assertEqual(messages[1]["body"], b'{"detail":"no"}')
        headers = {
            key.decode("ascii"): value.decode("ascii")
            for key, value in messages[0]["headers"]
        }
        self.assertIn("content-security-policy", headers)
        self.assertEqual(headers["referrer-policy"], "strict-origin-when-cross-origin")


if __name__ == "__main__":
    unittest.main()
