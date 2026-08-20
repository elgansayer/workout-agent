"""Fail-closed authentication and ownership guard for state-changing requests.

The dashboard supports a deliberately read-only anonymous mode in trusted test and
local environments, but mutations are never anonymous. This module installs a
process-wide FastAPI middleware boundary immediately inside ``SessionMiddleware``
so every current and future POST/PUT/PATCH/DELETE request must carry a complete
signed session identity before route code runs.

Routes must derive ownership from the authenticated session. Explicit client
owner claims are treated only as a defence-in-depth consistency assertion: a
claim for another account is rejected with a non-enumerating 404 and is never
used to choose the record owner.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_OWNER_FIELDS = frozenset(
    {
        "user_id",
        "owner_id",
        "account_id",
        "subject_user_id",
        "target_user_id",
    }
)
_OWNER_HEADERS = {
    "x-user-id": "user_id",
    "x-owner-id": "owner_id",
    "x-account-id": "account_id",
}
_MAX_INSPECTABLE_BODY_BYTES = 128 * 1024
_MULTIPART_BOUNDARY_RE = re.compile(r"(?:^|;)\s*boundary=(?:\"([^\"]+)\"|([^;]+))", re.I)
_MULTIPART_NAME_RE = re.compile(r'(?:^|;)\s*name="([^"]+)"', re.I)


def _json_response(status: int, detail: str) -> tuple[Message, Message]:
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
    return (
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"private, no-store, max-age=0"),
            ],
        },
        {"type": "http.response.body", "body": body},
    )


async def _send_rejection(send: Send, *, status: int, detail: str) -> None:
    start, body = _json_response(status, detail)
    await send(start)
    await send(body)


def _session_identity(scope: Scope) -> str | None:
    session = scope.get("session")
    if not isinstance(session, Mapping):
        return None
    user = session.get("user")
    user_id = session.get("user_id")
    if not user or not isinstance(user_id, str) or not user_id.strip():
        return None
    return user_id.strip()


def _claim_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        values: list[str] = []
        for item in value:
            values.extend(_claim_values(item))
        return values
    if value is None:
        return [""]
    return [str(value).strip()]


def _collect_json_owner_claims(value: Any) -> list[str]:
    claims: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalised_key = str(key).strip().lower()
            if normalised_key in _OWNER_FIELDS:
                claims.extend(_claim_values(item))
            claims.extend(_collect_json_owner_claims(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            claims.extend(_collect_json_owner_claims(item))
    return claims


def _query_owner_claims(headers: Headers, scope: Scope) -> list[str]:
    claims: list[str] = []
    raw_query = scope.get("query_string", b"")
    if isinstance(raw_query, bytes) and raw_query:
        try:
            query = parse_qs(raw_query.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            query = {}
        for key, values in query.items():
            if key.strip().lower() in _OWNER_FIELDS:
                claims.extend(_claim_values(values))

    for header, _field in _OWNER_HEADERS.items():
        if header in headers:
            claims.extend(_claim_values(headers.get(header)))
    return claims


def _multipart_owner_claims(body: bytes, content_type: str) -> list[str]:
    match = _MULTIPART_BOUNDARY_RE.search(content_type)
    boundary_text = (match.group(1) or match.group(2)).strip() if match else ""
    if not boundary_text:
        return []
    try:
        boundary = ("--" + boundary_text).encode("ascii")
    except UnicodeEncodeError:
        return []

    claims: list[str] = []
    for part in body.split(boundary):
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, raw_value = part.split(b"\r\n\r\n", 1)
        try:
            header_text = raw_headers.decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes all bytes
            continue
        disposition = next(
            (
                line.split(":", 1)[1].strip()
                for line in header_text.split("\r\n")
                if line.lower().startswith("content-disposition:") and ":" in line
            ),
            "",
        )
        name_match = _MULTIPART_NAME_RE.search(disposition)
        if not name_match or name_match.group(1).strip().lower() not in _OWNER_FIELDS:
            continue
        value = raw_value.rstrip(b"\r\n-")
        claims.append(value.decode("utf-8", errors="replace").strip())
    return claims


def _body_owner_claims(body: bytes, content_type: str) -> list[str]:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/json" or media_type.endswith("+json"):
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
        return _collect_json_owner_claims(payload)

    if media_type == "application/x-www-form-urlencoded":
        try:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return []
        claims: list[str] = []
        for key, value in values.items():
            if key.strip().lower() in _OWNER_FIELDS:
                claims.extend(_claim_values(value))
        return claims

    if media_type == "multipart/form-data":
        return _multipart_owner_claims(body, content_type)
    return []


def _content_length(headers: Headers) -> int | None:
    raw = headers.get("content-length")
    if not raw:
        return None
    try:
        length = int(raw)
    except ValueError:
        return None
    return length if 0 <= length <= _MAX_INSPECTABLE_BODY_BYTES else None


async def _buffer_body(receive: Receive, expected_length: int) -> tuple[bytes, Receive]:
    if expected_length == 0:
        return b"", receive

    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            # A disconnect before a complete request body is best left to the
            # downstream application rather than turning it into an auth error.
            async def replay_disconnect(message: Message = message) -> Message:
                return message

            return b"", replay_disconnect
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break

    body = b"".join(chunks)
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return body, replay


def _claims_match_authenticated_user(claims: list[str], user_id: str) -> bool:
    return all(claim == user_id for claim in claims)


class MutationSecurityMiddleware:
    """Require a signed identity and reject contradictory owner claims."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http" or str(scope.get("method", "")).upper() not in _MUTATION_METHODS:
            await self.app(scope, receive, send)
            return

        user_id = _session_identity(scope)
        if user_id is None:
            await _send_rejection(send, status=401, detail="Not authenticated")
            return

        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["mutation_user_id"] = user_id

        headers = Headers(scope=scope)
        claims = _query_owner_claims(headers, scope)
        downstream_receive = receive

        length = _content_length(headers)
        content_type = headers.get("content-type", "")
        if length is not None and length > 0 and content_type:
            body, downstream_receive = await _buffer_body(receive, length)
            claims.extend(_body_owner_claims(body, content_type))

        if not _claims_match_authenticated_user(claims, user_id):
            await _send_rejection(send, status=404, detail="Not found")
            return

        await self.app(scope, downstream_receive, send)


def _guarded_add_middleware(
    app: FastAPI,
    middleware_class: type[Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    original = getattr(FastAPI, "_workout_mutation_guard_original_add_middleware", None)
    if original is None:  # pragma: no cover - defensive import-order guard
        raise RuntimeError("Mutation security guard was not initialised")

    if middleware_class is SessionMiddleware and not any(
        middleware.cls is MutationSecurityMiddleware for middleware in app.user_middleware
    ):
        # Starlette applies middleware in reverse registration order. Register
        # this guard first so SessionMiddleware, registered immediately after,
        # wraps it and populates scope['session'] before mutation validation.
        original(app, MutationSecurityMiddleware)

    original(app, middleware_class, *args, **kwargs)


def install_mutation_security_guard() -> None:
    """Install the FastAPI mutation boundary once for this Python process."""

    if getattr(FastAPI, "_workout_mutation_guard_installed", False):
        return
    FastAPI._workout_mutation_guard_original_add_middleware = FastAPI.add_middleware  # type: ignore[attr-defined]
    FastAPI.add_middleware = _guarded_add_middleware  # type: ignore[method-assign]
    FastAPI._workout_mutation_guard_installed = True  # type: ignore[attr-defined]


__all__ = ["MutationSecurityMiddleware", "install_mutation_security_guard"]
