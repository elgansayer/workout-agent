"""Opaque error responses with the detail kept in protected diagnostics.

An unhandled exception, a ``5xx`` response and a validation failure are three
ways a stack trace, an exception message or an echoed request value can reach a
browser, a proxy log or a cache. This module closes all three:

* **Unhandled exceptions** become ``500`` responses carrying only a generic
  message and a random ``error_id``. The traceback is written once, redacted by
  :mod:`log_redaction`, to the ``workout_agent.error_diagnostics`` logger together
  with that identifier and the request correlation id. That log stream is the
  protected diagnostics channel: only an operator with access to the server logs
  can read it, and nothing in it is reachable over HTTP.
* **``5xx`` ``HTTPException``\\ s** keep their curated ``detail`` (scrubbed of any
  secret-shaped text) and gain the same ``error_id``; the exception, including
  the ``raise ... from`` cause that used to be dropped silently, is recorded in
  the diagnostics channel.
* **Validation errors** return the field locations, error types and messages but
  never the submitted ``input`` or the validator ``ctx``, which would otherwise
  echo a submitted credential, prompt or health value back in the response.

The behaviour does not depend on the environment: a development server returns the
same opaque body, so a misconfigured ``APP_ENV`` can never turn on detailed errors.
"""

from __future__ import annotations

import html
import logging
import secrets
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from log_redaction import redact_text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from webapp.request_correlation import REQUEST_ID_HEADER, get_request_id

DIAGNOSTICS_LOGGER_NAME = "workout_agent.error_diagnostics"
INTERNAL_ERROR_DETAIL = "An internal error occurred."
ERROR_ID_PREFIX = "err_"

_diagnostics = logging.getLogger(DIAGNOSTICS_LOGGER_NAME)
_BODYLESS_STATUSES = frozenset({204, 304})
_MAX_DETAIL_DEPTH = 8
_MAX_LOGGED_PATH = 200

_HTML_ERROR_PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8">'
    "<title>Something went wrong</title></head>"
    "<body><main><h1>Something went wrong</h1>"
    "<p>The request could not be completed. If you contact the operator, quote "
    "this reference:</p><p><code>{error_id}</code></p></main></body></html>\n"
)


def _public_text(value: object) -> str:
    """Scrub text that will be shown to a client.

    The exact-value secret registry is deliberately skipped: it is shared by all
    tenants, so masking a registered value in a response would let a caller probe
    whether a guessed string is another account's live credential.
    """

    return redact_text(value, known_secrets=False)


def new_error_id() -> str:
    """Return a random identifier that encodes nothing about the failure."""

    return f"{ERROR_ID_PREFIX}{secrets.token_hex(8)}"


def _log_safe(value: object, limit: int = _MAX_LOGGED_PATH) -> str:
    """Neutralise a client-influenced value (a path) before it is logged."""

    text = redact_text(str(value))[:limit]
    return text.encode("unicode_escape").decode("ascii")


def record_server_error(
    scope: Scope,
    error_id: str,
    exc: BaseException,
    *,
    status: int = 500,
    trace: bool = True,
) -> None:
    """Write the protected diagnostic record for one server error.

    The traceback is redacted by the process-wide log redaction before any handler
    sees it. ``trace=False`` is used when the server itself will already log the
    traceback (a response that had started streaming).
    """

    _diagnostics.error(
        "server_error error_id=%s request_id=%s status=%d method=%s path=%s exception=%s",
        error_id,
        get_request_id(Request(scope)),
        status,
        _log_safe(scope.get("method", "-"), 16),
        _log_safe(scope.get("path", "-")),
        type(exc).__name__,
        exc_info=exc if trace else None,
    )


def _accepts_html(scope: Scope) -> bool:
    path = str(scope.get("path", ""))
    if path.startswith("/api/"):
        return False
    for name, value in scope.get("headers", ()):
        if name.lower() == b"accept" and b"text/html" in value.lower():
            return True
    return False


def _opaque_response(scope: Scope, error_id: str) -> Response:
    headers = {
        "Cache-Control": "no-store",
        REQUEST_ID_HEADER: get_request_id(Request(scope)),
    }
    if _accepts_html(scope):
        return HTMLResponse(
            _HTML_ERROR_PAGE.format(error_id=html.escape(error_id)),
            status_code=500,
            headers=headers,
        )
    return JSONResponse(
        {"detail": INTERNAL_ERROR_DETAIL, "error_id": error_id},
        status_code=500,
        headers=headers,
    )


class ErrorBoundaryMiddleware:
    """Turn any escaping exception into an opaque ``500`` plus a diagnostic record."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except ClientDisconnect:
            # The caller went away; there is nobody to answer and nothing failed.
            raise
        except Exception as exc:
            error_id = new_error_id()
            if response_started:
                # The status line is already on the wire, so the response cannot
                # be replaced. Let the server abort the connection (it logs the
                # redacted traceback) and keep only the correlating record.
                record_server_error(scope, error_id, exc, trace=False)
                raise
            record_server_error(scope, error_id, exc)
            await _opaque_response(scope, error_id)(scope, receive, send)


def _scrub_detail(value: Any, depth: int = 0) -> Any:
    """Redact secret-shaped text in a response detail, keeping its structure.

    Keys are untouched on purpose: structured details (programme violations,
    conflict descriptions) are consumed by the browser and use field names that
    the log classification would otherwise treat as workout data.
    """

    if isinstance(value, str):
        return _public_text(value)
    if depth >= _MAX_DETAIL_DEPTH:
        return "[REDACTED:DEPTH]"
    if isinstance(value, Mapping):
        return {key: _scrub_detail(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_detail(item, depth + 1) for item in value]
    return value


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """Render an ``HTTPException`` with a scrubbed detail; ``5xx`` also get an id."""

    headers = getattr(exc, "headers", None)
    if exc.status_code in _BODYLESS_STATUSES:
        return Response(status_code=exc.status_code, headers=headers)
    body: dict[str, Any] = {"detail": _scrub_detail(exc.detail)}
    if exc.status_code >= 500:
        error_id = new_error_id()
        record_server_error(request.scope, error_id, exc, status=exc.status_code)
        body["error_id"] = error_id
    return JSONResponse(body, status_code=exc.status_code, headers=headers)


def _clean_location(part: object) -> int | str:
    return part if isinstance(part, int) else _public_text(part)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Report which fields are invalid without echoing what was submitted."""

    errors = [
        {
            "type": str(error.get("type", "value_error")),
            "loc": [_clean_location(part) for part in error.get("loc", ())],
            "msg": _public_text(error.get("msg", "Invalid value.")),
        }
        for error in exc.errors()
    ]
    return JSONResponse({"detail": errors}, status_code=422)


def install_error_handling(app: FastAPI) -> None:
    """Install the exception handlers and the outermost error boundary.

    Call this *before* adding the request-correlation middleware so the boundary
    sits inside it and its responses carry the ``X-Request-ID`` header.
    """

    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_middleware(ErrorBoundaryMiddleware)
