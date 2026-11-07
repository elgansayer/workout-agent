"""Helpers for tests that need a genuine, registry-backed session cookie.

The production login registers a session identifier in ``user_sessions`` and puts
it inside the signed cookie.  A cookie that is merely *signed* (for example one
forged with a leaked ``WEB_AUTH_SECRET``) is deliberately rejected, so tests that
want to be "logged in" must register a session the same way login does.
"""

from __future__ import annotations

import base64
import json

from itsdangerous import TimestampSigner


def signed_session_cookie(
    secret: str,
    *,
    user: str,
    user_id: str,
    session_id: str | None = None,
) -> str:
    """Return a Starlette-format signed session cookie value.

    ``session_id`` must come from ``SessionStore.create`` for the cookie to be
    honoured; omit it to build the kind of unregistered cookie that must be
    rejected.
    """

    claims: dict[str, str] = {"user": user, "user_id": user_id}
    if session_id is not None:
        claims["sid"] = session_id
    payload = base64.b64encode(json.dumps(claims).encode())
    return TimestampSigner(secret).sign(payload).decode()
