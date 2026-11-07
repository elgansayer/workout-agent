"""Deterministic regression tests for cookie-authenticated CSRF protection."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest
from database import init_db
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from ownership import InvalidOwnerContextError, UnknownOwnerError, owner_for_user
from tenant_fixtures import synthetic_owner
from webapp.csrf_security import CSRF_HEADER_NAME, CSRFMiddleware, CSRFTokenStore

_SECRET = "s" * 32


def _seed_owner(
    db_path: str,
    user_id: str = "user-1",
    email: str = "athlete@example.test",
) -> str:
    """Store the owner a CSRF nonce may be issued for."""
    init_db(db_path)
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (id, email, created_at) VALUES (?, ?, ?)",
            (user_id, email, "2026-01-01T00:00:00+00:00"),
        )
    return user_id


def _session_cookie(user_id: str = "user-1") -> str:
    payload = base64.b64encode(
        json.dumps({"user": "athlete@example.test", "user_id": user_id}).encode()
    )
    return TimestampSigner(_SECRET).sign(payload).decode()


def _client(tmp_path: Path, *, trusted_origins: tuple[str, ...] = ()) -> TestClient:
    app = FastAPI()

    @app.post("/mutate")
    def mutate() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    def webhook() -> dict[str, str]:
        return {"status": "accepted"}

    db_path = str(tmp_path / "csrf.db")
    _seed_owner(db_path)

    app.add_middleware(
        CSRFMiddleware,
        db_path=db_path,
        session_secret=_SECRET,
        trusted_origins=trusted_origins,
        ttl_seconds=60,
    )
    client = TestClient(app, base_url="https://workout.example")
    client.cookies.set("session", _session_cookie())
    return client


def _token(client: TestClient, **headers: str) -> str:
    response = client.get("/api/csrf-token", headers=headers)
    assert response.status_code == 200
    cache_control = response.headers["cache-control"]
    assert "private" in cache_control
    assert "no-store" in cache_control
    assert "max-age=0" in cache_control
    return response.json()["token"]


def test_token_store_is_user_session_bound_single_use_and_expiring(tmp_path: Path) -> None:
    db_path = str(tmp_path / "tokens.db")
    _seed_owner(db_path)
    _seed_owner(db_path, "user-2", "other@example.test")
    first = owner_for_user(db_path, "user-1")
    second = owner_for_user(db_path, "user-2")
    store = CSRFTokenStore(db_path, ttl_seconds=10)
    token = store.issue(first, "session-a", now=100)

    assert store.consume(second, "session-a", token, now=101) is False
    assert store.consume(first, "session-b", token, now=101) is False
    assert store.consume(first, "session-a", token, now=101) is True
    assert store.consume(first, "session-a", token, now=101) is False

    expired = store.issue(first, "session-a", now=200)
    assert store.consume(first, "session-a", expired, now=210) is False


def test_token_store_rejects_raw_identifiers_and_unknown_owners(tmp_path: Path) -> None:
    db_path = str(tmp_path / "raw.db")
    _seed_owner(db_path)
    store = CSRFTokenStore(db_path, ttl_seconds=10)

    # A bare identifier is not an authenticated owner context, even when it
    # names a stored user, and neither issuing nor spending accepts one.
    for raw in ("user-1", None, object()):
        with pytest.raises(InvalidOwnerContextError):
            store.issue(raw, "session-a", now=100)  # type: ignore[arg-type]
        with pytest.raises(InvalidOwnerContextError):
            store.consume(raw, "session-a", "token", now=101)  # type: ignore[arg-type]

    # A well-formed context for a user that does not exist cannot mint a nonce.
    with pytest.raises(UnknownOwnerError):
        store.issue(synthetic_owner("ghost-user"), "session-a", now=100)


def test_missing_invalid_and_replayed_tokens_fail_closed(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.post("/mutate").status_code == 403
    assert (
        client.post("/mutate", headers={CSRF_HEADER_NAME: "not-a-token"}).status_code
        == 403
    )

    token = _token(client)
    assert client.post("/mutate", headers={CSRF_HEADER_NAME: token}).status_code == 200
    assert client.post("/mutate", headers={CSRF_HEADER_NAME: token}).status_code == 403


def test_cross_origin_request_is_rejected_without_consuming_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    token = _token(client)

    rejected = client.post(
        "/mutate",
        headers={"Origin": "https://evil.example", CSRF_HEADER_NAME: token},
    )
    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "CSRF origin rejected"}

    # The origin check happens before consumption, so the legitimate tab can
    # still use the one-time nonce after a rejected cross-site attempt.
    assert client.post("/mutate", headers={CSRF_HEADER_NAME: token}).status_code == 200


def test_explicit_cross_origin_spa_is_supported(tmp_path: Path) -> None:
    client = _client(tmp_path, trusted_origins=("https://spa.example",))
    token_response = client.get(
        "/api/csrf-token", headers={"Origin": "https://spa.example"}
    )

    assert token_response.status_code == 200
    assert token_response.headers["access-control-allow-origin"] == "https://spa.example"
    assert token_response.headers["access-control-allow-credentials"] == "true"
    token = token_response.json()["token"]
    response = client.post(
        "/mutate",
        headers={"Origin": "https://spa.example", CSRF_HEADER_NAME: token},
    )
    assert response.status_code == 200


def test_oauth_style_gets_and_cookie_free_webhooks_are_not_blanket_bypassed_by_path(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    # OAuth callbacks are safe-method requests and therefore keep their own
    # state/nonce validation in the route instead of receiving a CSRF exception.
    assert client.get("/unmatched-oauth-callback").status_code == 404

    # A connector webhook does not carry the application session cookie. It is
    # allowed through to its own signature validator rather than appearing on a
    # dangerous global CSRF path allowlist.
    client.cookies.clear()
    assert client.post("/webhook").status_code == 200


def test_invalid_signed_session_cannot_mint_or_spend_tokens(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.cookies.set("session", "forged-cookie")

    assert client.get("/api/csrf-token").status_code == 401
    assert (
        client.post("/mutate", headers={CSRF_HEADER_NAME: "anything"}).status_code == 401
    )


def test_production_and_spa_wiring_keep_csrf_boundary_enabled() -> None:
    root = Path(__file__).resolve().parents[2]
    secure_entrypoint = (root / "backend/webapp/secure_app.py").read_text(encoding="utf-8")
    interceptor = (root / "frontend/src/app/interceptors/api.interceptor.ts").read_text(
        encoding="utf-8"
    )

    assert "CSRFMiddleware" in secure_entrypoint
    assert "session_secret=_session_secret" in secure_entrypoint
    assert "trusted_origins=_trusted_browser_origins()" in secure_entrypoint
    assert "app = SecurityHeadersMiddleware(" in secure_entrypoint
    assert "ProxySecurityMiddleware(secured_application" in secure_entrypoint

    assert "UNSAFE_METHODS" in interceptor
    assert "/api/csrf-token" in interceptor
    assert "X-CSRF-Token" in interceptor
    assert "withCredentials: true" in interceptor
