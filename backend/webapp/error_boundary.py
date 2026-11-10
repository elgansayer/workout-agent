"""Opaque error responses for the web application.

Unhandled exceptions never reach the client: the response carries only a
generic message and an opaque reference, the redacted traceback goes to the
protected diagnostics sink (``diagnostics.py``), and the general log gets a
single line with the same reference. Deliberate ``HTTPException`` details and
request-validation errors are scrubbed so they cannot echo secrets or the
submitted values back.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from diagnostics import new_error_id, record_exception
from log_redaction import redact_text, redact_value

logger = logging.getLogger(__name__)

PUBLIC_ERROR_MESSAGE = "An unexpected error occurred."
_NO_STORE = {"Cache-Control": "no-store"}


def _wants_html(scope: Scope) -> bool:
    """Browser page navigations get a page; everything else gets JSON."""
    if str(scope.get("path") or "").startswith("/api/"):
        return False
    for name, value in scope.get("headers") or []:
        if name == b"accept":
            return "text/html" in value.decode("latin-1").lower()
    return False


def _error_response(scope: Scope, error_id: str) -> Response:
    if _wants_html(scope):
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            "<title>Something went wrong</title></head><body>"
            "<h1>Something went wrong</h1>"
            f"<p>{html.escape(PUBLIC_ERROR_MESSAGE)} If you ask for help, "
            f"quote this reference: <code>{html.escape(error_id)}</code></p>"
            "</body></html>"
        )
        return HTMLResponse(body, status_code=500, headers=_NO_STORE)
    return JSONResponse(
        {
            "detail": f"{PUBLIC_ERROR_MESSAGE} Reference: {error_id}",
            "error_id": error_id,
        },
        status_code=500,
        headers=_NO_STORE,
    )


def _report(scope: Scope, exc: Exception) -> str:
    """Record *exc* in protected diagnostics and return its opaque reference."""
    error_id = new_error_id()
    method = str(scope.get("method") or "")
    route = str(getattr(scope.get("route"), "path", "") or "(unmatched)")
    record_exception(error_id, exc, method=method, route=route)
    logger.error(
        "Unhandled server error. error_id=%s method=%s route=%s exception=%s",
        error_id,
        method,
        route,
        type(exc).__name__,
    )
    return error_id


class OpaqueErrorMiddleware:
    """Contain every unhandled exception behind an opaque 500 response.

    The exception is deliberately not re-raised, so neither the ASGI server
    nor a test client prints a second, unprotected copy of the traceback.
    """

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
        except Exception as exc:  # noqa: BLE001 - this is the containment boundary
            error_id = _report(scope, exc)
            if response_started:
                # The status line is already on the wire; the server ends the
                # connection instead of appending an error body to a stream.
                return
            await _error_response(scope, error_id)(scope, receive, send)


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    scrubbed = StarletteHTTPException(
        status_code=exc.status_code,
        detail=redact_value(exc.detail),
        headers=exc.headers,
    )
    return await http_exception_handler(request, scrubbed)


async def _validation_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    # FastAPI's default body echoes each rejected ``input``; a rejected API key
    # or health value must not be reflected, so keep only where and why.
    errors: list[dict[str, Any]] = [
        {
            "type": error.get("type"),
            "loc": list(error.get("loc", ())),
            "msg": redact_text(str(error.get("msg", ""))),
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def install_error_boundary(app: FastAPI) -> None:
    """Install the opaque-error middleware and scrubbing handlers once."""
    if any(item.cls is OpaqueErrorMiddleware for item in app.user_middleware):
        return
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_middleware(OpaqueErrorMiddleware)


__all__ = ["PUBLIC_ERROR_MESSAGE", "OpaqueErrorMiddleware", "install_error_boundary"]
