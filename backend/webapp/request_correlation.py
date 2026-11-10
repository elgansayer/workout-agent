"""Per-request correlation identifiers shared by audit events and responses.

Every HTTP request receives a fresh, server-generated identifier. It is exposed
as ``request.state.request_id`` so all audit events written while serving the
request share it, and returned in the ``X-Request-ID`` response header so a user
or operator can quote it when investigating an incident.

An inbound ``X-Request-ID`` is deliberately ignored: a client-chosen value could
be replayed to make unrelated events look correlated in an audit trail.
"""

from __future__ import annotations

import uuid

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_STATE_KEY = "request_id"


class RequestCorrelationMiddleware:
    """Assign a correlation identifier to every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})[_STATE_KEY] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def get_request_id(request: Request) -> str:
    """Return the request's correlation id, minting one if no middleware ran."""

    request_id = getattr(request.state, _STATE_KEY, None)
    if not isinstance(request_id, str) or not request_id:
        request_id = uuid.uuid4().hex
        setattr(request.state, _STATE_KEY, request_id)
    return request_id
