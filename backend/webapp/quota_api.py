"""HTTP adapter for per-account quotas.

Turns quota and administrator-action failures into actionable responses
(``Retry-After`` plus a structured body), identifies the acting account from the
signed session only, and defines the strict request models for the
administrator routes in :mod:`webapp.app`.
"""

from __future__ import annotations

import html

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictInt
from quota_admin import (
    AccountNotFoundError,
    AdminAuthorizationError,
    AdminValidationError,
)
from quotas import (
    CODE_DISABLED,
    QuotaDenial,
    QuotaExceededError,
    QuotaUnavailableError,
    describe_denial,
)

from webapp.auth_boundary import has_verified_identity

_UNAVAILABLE_RETRY_SECONDS = 5


class EntitlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entitlement: str
    reason: str


class OverrideRequest(BaseModel):
    """Set an override, or clear it when ``limit_percent`` is null."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    limit_percent: StrictInt | None = None
    expires_in_seconds: StrictInt | None = None
    reason: str


class AbuseReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    reason: str


def client_address(request: Request) -> str:
    """Return the proxy-validated client address, never a raw forwarding header."""

    validated = getattr(request.state, "validated_client_ip", None)
    if isinstance(validated, str) and validated:
        return validated
    return request.client.host if request.client else "unknown"


def session_actor(request: Request) -> str:
    """Return the signed-session account id or raise 401.

    Administrator routes use this instead of the dashboard's anonymous-mode
    fallback: an anonymous deployment has no administrators.
    """

    session = request.session
    if not has_verified_identity(session):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return str(session["user_id"]).strip()


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith("/api/") and "text/html" in request.headers.get(
        "accept", ""
    )


def _html_page(title: str, message: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title></head>"
        f"<body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></main>"
        "</body></html>"
    )


def _respond(
    request: Request,
    status_code: int,
    message: str,
    *,
    error: str,
    retry_after: int | None = None,
    quota: dict[str, object] | None = None,
) -> Response:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    if _wants_html(request):
        return HTMLResponse(
            _html_page("Please slow down", message), status_code=status_code, headers=headers
        )
    body: dict[str, object] = {"detail": message, "error": error}
    if quota is not None:
        body["quota"] = quota
    return JSONResponse(body, status_code=status_code, headers=headers)


def quota_denial_response(request: Request, denial: QuotaDenial) -> Response:
    """Build the 429 (or 403 for an indefinitely disabled quota) response."""

    indefinite = denial.code == CODE_DISABLED and denial.retry_after_seconds is None
    return _respond(
        request,
        403 if indefinite else 429,
        describe_denial(denial),
        error=denial.code,
        retry_after=denial.retry_after_seconds,
        quota={
            "kind": denial.kind.value,
            "limit": denial.limit,
            "window_seconds": denial.window_seconds,
            "retry_after_seconds": denial.retry_after_seconds,
        },
    )


def install_quota_error_handlers(app: FastAPI) -> None:
    """Map quota and administrator-action errors to HTTP responses."""

    @app.exception_handler(QuotaExceededError)
    async def _quota_exceeded(request: Request, exc: QuotaExceededError) -> Response:
        return quota_denial_response(request, exc.denial)

    @app.exception_handler(QuotaUnavailableError)
    async def _quota_unavailable(request: Request, exc: QuotaUnavailableError) -> Response:
        return _respond(
            request,
            503,
            "Usage limits are temporarily unavailable. Try again shortly.",
            error="quota_unavailable",
            retry_after=_UNAVAILABLE_RETRY_SECONDS,
        )

    @app.exception_handler(AdminAuthorizationError)
    async def _not_admin(request: Request, exc: AdminAuthorizationError) -> Response:
        return JSONResponse({"detail": "Administrator access required."}, status_code=403)

    @app.exception_handler(AccountNotFoundError)
    async def _no_account(request: Request, exc: AccountNotFoundError) -> Response:
        return JSONResponse({"detail": "No such account."}, status_code=404)

    @app.exception_handler(AdminValidationError)
    async def _invalid(request: Request, exc: AdminValidationError) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=422)
