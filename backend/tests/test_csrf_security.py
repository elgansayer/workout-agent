"""Deterministic regression tests for cookie-authenticated CSRF protection."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from webapp.csrf_security import CSRF_HEADER_NAME, CSRFMiddleware, CSRFTokenStore

_SECRET = "s" * 32


def _session_cookie(user_id: str = "user-1") -> str:
    payload = base64.b64encode(
        json.dumps({"user": "athlete@example.test", "user_id": user_id}).encode()
    )
    return TimestampSigner(_SECRET).sign(payload).decode()


def _client(
    tmp_path: Path,
    *,
    trusted_origins: tuple[str, ...] = (),
    cookie_name: str = "session",
    **middleware_options: Any,
) -> TestClient:
    app = FastAPI()

    @app.post("/mutate")
    def mutate() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    def webhook() -> dict[str, str]:
        return {"status": "accepted"}

    app.add_middleware(
        CSRFMiddleware,
        db_path=str(tmp_path / "csrf.db"),
        session_secret=_SECRET,
        trusted_origins=trusted_origins,
        ttl_seconds=60,
        session_cookie_name=cookie_name,
        **middleware_options,
    )
    client = TestClient(app, base_url="https://workout.example")
    client.cookies.set(cookie_name, _session_cookie())
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
    store = CSRFTokenStore(str(tmp_path / "tokens.db"), ttl_seconds=10)
    token = store.issue("user-1", "session-a", now=100)

    assert store.consume("user-2", "session-a", token, now=101) is False
    assert store.consume("user-1", "session-b", token, now=101) is False
    assert store.consume("user-1", "session-a", token, now=101) is True
    assert store.consume("user-1", "session-a", token, now=101) is False

    expired = store.issue("user-1", "session-a", now=200)
    assert store.consume("user-1", "session-a", expired, now=210) is False


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


def test_session_validator_can_revoke_a_correctly_signed_cookie(tmp_path: Path) -> None:
    honoured = {"value": True}
    seen: list[dict[str, Any]] = []

    def validator(payload: Any) -> bool:
        seen.append(dict(payload))
        return honoured["value"]

    client = _client(tmp_path, session_validator=validator)
    token = _token(client)
    assert seen and seen[0]["user_id"] == "user-1"  # given the decoded payload

    honoured["value"] = False  # the session registry no longer honours the cookie

    assert client.get("/api/csrf-token").status_code == 401
    assert client.post("/mutate", headers={CSRF_HEADER_NAME: token}).status_code == 401


def test_hardened_cookie_name_is_the_one_that_authenticates(tmp_path: Path) -> None:
    client = _client(tmp_path, cookie_name="__Host-session")

    assert client.get("/api/csrf-token").status_code == 200
    client.cookies.clear()
    client.cookies.set("session", _session_cookie())  # the legacy name is ignored
    assert client.get("/api/csrf-token").status_code == 401


def test_configured_session_max_age_bounds_the_cookie_signature(tmp_path: Path) -> None:
    class _AgedSigner(TimestampSigner):
        def get_timestamp(self) -> int:
            return int(time.time()) - 120  # signed two minutes ago

    payload = base64.b64encode(
        json.dumps({"user": "athlete@example.test", "user_id": "user-1"}).encode()
    )
    aged = _AgedSigner(_SECRET).sign(payload).decode()

    lenient = _client(tmp_path / "lenient", session_max_age_seconds=3600)
    lenient.cookies.set("session", aged)
    assert lenient.get("/api/csrf-token").status_code == 200

    strict = _client(tmp_path / "strict", session_max_age_seconds=60)
    strict.cookies.set("session", aged)
    assert strict.get("/api/csrf-token").status_code == 401


def test_production_and_spa_wiring_keep_csrf_boundary_enabled() -> None:
    root = Path(__file__).resolve().parents[2]
    secure_entrypoint = (root / "backend/webapp/secure_app.py").read_text(encoding="utf-8")
    interceptor = (root / "frontend/src/app/interceptors/api.interceptor.ts").read_text(
        encoding="utf-8"
    )

    assert "CSRFMiddleware" in secure_entrypoint
    assert "session_secret=_session_secret" in secure_entrypoint
    assert "trusted_origins=_trusted_browser_origins()" in secure_entrypoint
    # The CSRF layer must read the hardened cookie and defer to the session
    # registry, otherwise a revoked cookie could still mint CSRF tokens.
    assert "session_cookie_name=SESSION_SECURITY.cookie.name" in secure_entrypoint
    assert "session_max_age_seconds=SESSION_SECURITY.cookie.max_age" in secure_entrypoint
    assert "session_validator=SESSION_STORE.is_active_payload" in secure_entrypoint
    assert "app = SecurityHeadersMiddleware(" in secure_entrypoint
    assert "ProxySecurityMiddleware(secured_application" in secure_entrypoint

    assert "UNSAFE_METHODS" in interceptor
    assert "/api/csrf-token" in interceptor
    assert "X-CSRF-Token" in interceptor
    assert "withCredentials: true" in interceptor


def test_closed_accounts_can_neither_obtain_nor_use_tokens(tmp_path: Path) -> None:
    from database import init_db

    client = _client(tmp_path)
    db = str(tmp_path / "csrf.db")
    init_db(db)  # adds the deletion ledger next to the token table
    live_token = _token(client)

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO account_tombstones "
            "(user_id, request_id, deleted_at, retain_until, policy_version) "
            "VALUES ('user-1', 'receipt', '2026-11-10T00:00:00+00:00', "
            "'2027-11-10T00:00:00+00:00', '1.0.0')"
        )

    # A token minted before closure is useless, and no new one is issued.
    assert client.get("/api/csrf-token").status_code == 401
    assert (
        client.post("/mutate", headers={CSRF_HEADER_NAME: live_token}).status_code
        == 401
    )
    # Other accounts are unaffected.
    other = TestClient(client.app, base_url="https://workout.example")
    other.cookies.set("session", _session_cookie("user-2"))
    assert _token(other)


def _public_recovery_client(tmp_path: Path) -> TestClient:
    from webapp.mutation_security import PUBLIC_MUTATION_PATHS

    app = FastAPI()

    def complete() -> dict[str, str]:
        return {"status": "ok"}

    for path in sorted(PUBLIC_MUTATION_PATHS):
        app.add_api_route(path, complete, methods=["POST"])

    app.add_middleware(
        CSRFMiddleware,
        db_path=str(tmp_path / "public-recovery.db"),
        session_secret=_SECRET,
        trusted_origins=(),
        ttl_seconds=60,
    )
    return TestClient(app, base_url="https://workout.example")


def test_public_recovery_mutations_reject_explicit_cross_site_browsers(
    tmp_path: Path,
) -> None:
    client = _public_recovery_client(tmp_path)
    path = "/api/auth/recovery/complete"

    cross_origin = client.post(path, headers={"Origin": "https://evil.example"})
    assert cross_origin.status_code == 403
    assert cross_origin.json() == {"detail": "CSRF origin rejected"}

    cross_site_metadata = client.post(path, headers={"Sec-Fetch-Site": "cross-site"})
    assert cross_site_metadata.status_code == 403

    assert client.post(path).status_code == 200
    same_origin = client.post(path, headers={"Origin": "https://workout.example"})
    assert same_origin.status_code == 200


def test_public_recovery_mutations_keep_origin_check_with_a_stale_cookie(
    tmp_path: Path,
) -> None:
    client = _public_recovery_client(tmp_path)
    path = "/api/auth/recovery/complete"
    client.cookies.set("session", "forged-cookie")

    rejected = client.post(path, headers={"Origin": "https://evil.example"})
    assert rejected.status_code == 403

    allowed = client.post(path, headers={"Origin": "https://workout.example"})
    assert allowed.status_code == 200
