"""CSRF protection for cookie-authenticated browser mutations.

The production ASGI entrypoint wraps the application with this middleware. A
browser first obtains an opaque, single-use nonce from ``/api/csrf-token`` and
sends it back in ``X-CSRF-Token`` on POST, PUT, PATCH, and DELETE requests.
Tokens are stored only as hashes, bound to both the authenticated user and the
exact signed session cookie, and consumed atomically so replay fails closed.

OAuth callbacks remain GET requests and keep their purpose-specific OAuth
state/nonce checks. A webhook without the application's session cookie is not a
cookie-authenticated browser request and therefore reaches its route-specific
signature/replay validator; there is deliberately no path-based CSRF bypass.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_TOKEN_PATH = "/api/csrf-token"
CSRF_TOKEN_TTL_SECONDS = 10 * 60
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TOKEN_BYTES = 32


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalise_origin(value: str) -> str | None:
    """Return a comparable HTTP(S) origin or ``None`` for malformed input."""

    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    scheme = parsed.scheme.lower()
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    authority = parsed.hostname.lower()
    if port is not None and not default_port:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _request_origin(request: Request) -> str | None:
    host = request.headers.get("host", "").strip()
    if not host:
        return None
    return _normalise_origin(f"{request.url.scheme}://{host}")


def _origin_is_trusted(request: Request, trusted_origins: frozenset[str]) -> bool:
    """Validate browser origin metadata without requiring it from API clients."""

    origin = request.headers.get("origin")
    if origin:
        normalised = _normalise_origin(origin)
        current = _request_origin(request)
        return normalised is not None and (
            normalised == current or normalised in trusted_origins
        )

    # Modern browsers provide Fetch Metadata even in cases where Origin is
    # omitted. Reject an explicit cross-site signal, while still allowing
    # non-browser clients that provide neither header and possess a valid token.
    return request.headers.get("sec-fetch-site", "").lower() != "cross-site"


class CSRFTokenStore:
    """SQLite-backed, single-use token registry.

    Plaintext CSRF tokens and session cookies are never persisted. A token is
    useful only with the exact signed session that requested it.
    """

    def __init__(
        self,
        db_path: str,
        *,
        ttl_seconds: int = CSRF_TOKEN_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.db_path = db_path
        self.ttl_seconds = ttl_seconds

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS csrf_tokens (
                token_hash TEXT PRIMARY KEY,
                session_hash TEXT NOT NULL,
                user_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_csrf_tokens_user_expiry "
            "ON csrf_tokens (user_id, expires_at)"
        )
        return conn

    def issue(
        self,
        user_id: str,
        session_cookie: str,
        *,
        now: int | None = None,
    ) -> str:
        issued_at = int(time.time()) if now is None else now
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._connect() as conn:
            conn.execute("DELETE FROM csrf_tokens WHERE expires_at <= ?", (issued_at,))
            conn.execute(
                "INSERT INTO csrf_tokens "
                "(token_hash, session_hash, user_id, expires_at) VALUES (?, ?, ?, ?)",
                (
                    _sha256(token),
                    _sha256(session_cookie),
                    user_id,
                    issued_at + self.ttl_seconds,
                ),
            )
        return token

    def consume(
        self,
        user_id: str,
        session_cookie: str,
        token: str,
        *,
        now: int | None = None,
    ) -> bool:
        if not token or len(token) > 512:
            return False
        checked_at = int(time.time()) if now is None else now
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM csrf_tokens WHERE expires_at <= ?", (checked_at,))
            cursor = conn.execute(
                "DELETE FROM csrf_tokens "
                "WHERE token_hash = ? AND session_hash = ? AND user_id = ? "
                "AND expires_at > ?",
                (
                    _sha256(token),
                    _sha256(session_cookie),
                    user_id,
                    checked_at,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class SignedSessionIdentity:
    """Validate the Starlette session cookie without exposing its contents."""

    def __init__(
        self,
        secret_key: str,
        *,
        max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
    ) -> None:
        if not secret_key:
            raise ValueError("A session signing secret is required for CSRF protection")
        self.signer = TimestampSigner(secret_key)
        self.max_age_seconds = max_age_seconds

    def user_id(self, session_cookie: str) -> str | None:
        try:
            signed_payload = self.signer.unsign(
                session_cookie.encode("utf-8"),
                max_age=self.max_age_seconds,
            )
            payload = json.loads(base64.b64decode(signed_payload))
        except (
            BadSignature,
            SignatureExpired,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return None
        if not isinstance(payload, dict):
            return None
        user_id = payload.get("user_id")
        user = payload.get("user")
        if not isinstance(user_id, str) or not user_id or not user:
            return None
        return user_id


class CSRFMiddleware(BaseHTTPMiddleware):
    """Require one-time CSRF tokens for session-authenticated unsafe methods."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        db_path: str,
        session_secret: str,
        trusted_origins: Iterable[str] = (),
        session_cookie_name: str = "session",
        ttl_seconds: int = CSRF_TOKEN_TTL_SECONDS,
    ) -> None:
        super().__init__(app)
        self.store = CSRFTokenStore(db_path, ttl_seconds=ttl_seconds)
        self.session_identity = SignedSessionIdentity(session_secret)
        self.session_cookie_name = session_cookie_name
        self.trusted_origins = frozenset(
            origin
            for value in trusted_origins
            if (origin := _normalise_origin(value)) is not None
        )

    def _validated_session(self, request: Request) -> tuple[str, str] | None:
        session_cookie = request.cookies.get(self.session_cookie_name)
        if not session_cookie:
            return None
        user_id = self.session_identity.user_id(session_cookie)
        if not user_id:
            return None
        return user_id, session_cookie

    def _token_response(self, request: Request) -> Response:
        validated = self._validated_session(request)
        if validated is None:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        if not _origin_is_trusted(request, self.trusted_origins):
            return JSONResponse({"detail": "CSRF origin rejected"}, status_code=403)

        user_id, session_cookie = validated
        token = self.store.issue(user_id, session_cookie)
        response = JSONResponse(
            {"token": token, "expires_in": self.store.ttl_seconds},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

        # The Angular client may be served from a separately configured origin.
        # Mirror only an origin that already passed the explicit trust check.
        origin = request.headers.get("origin")
        normalised = _normalise_origin(origin) if origin else None
        current = _request_origin(request)
        if normalised and normalised != current and normalised in self.trusted_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method == "GET" and request.url.path == CSRF_TOKEN_PATH:
            return self._token_response(request)

        if request.method.upper() not in _UNSAFE_METHODS:
            return await call_next(request)

        session_cookie = request.cookies.get(self.session_cookie_name)
        if not session_cookie:
            # Non-cookie requests (including connector webhooks) must rely on
            # their route-specific authentication/signature checks. There is no
            # path allowlist that could accidentally exempt a browser session.
            return await call_next(request)

        user_id = self.session_identity.user_id(session_cookie)
        if not user_id:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        if not _origin_is_trusted(request, self.trusted_origins):
            return JSONResponse({"detail": "CSRF origin rejected"}, status_code=403)

        token = request.headers.get(CSRF_HEADER_NAME, "")
        if not self.store.consume(user_id, session_cookie, token):
            return JSONResponse(
                {"detail": "Invalid or expired CSRF token"},
                status_code=403,
            )
        return await call_next(request)
