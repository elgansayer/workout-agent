"""End-to-end tests: real routes write attributable, tamper-evident audit events."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

import security_audit as audit
from account_policy import AccessPolicy, AccessPolicyConfig
from database import get_or_create_user, init_db, set_meta
from hevy_connection import HevyCredentialState, HevyCredentialTest
from itsdangerous import TimestampSigner
from secret_vault import SecretVault
from security_audit import (
    SecurityAuditConfigurationError,
    resolve_audit_key,
    verify_chain,
)
from starlette.testclient import TestClient

_SECRET = "audit-routes-test-secret-that-is-long-enough"
_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_HEVY_KEY = "synthetic-hevy-api-key-value-0123456789abcdef"
_AI_KEY = "synthetic-ai-provider-key-value-0123456789abcdef"


def _cookie(user_id: str, email: str, session_id: str | None = None) -> str:
    claims: dict[str, str] = {"user": email, "user_id": user_id}
    if session_id is not None:
        claims["sid"] = session_id
    payload = base64.b64encode(json.dumps(claims).encode())
    return TimestampSigner(_SECRET).sign(payload).decode()


def _connected_hevy() -> HevyCredentialTest:
    return HevyCredentialTest(
        state=HevyCredentialState.CONNECTED,
        tested_at=datetime.now(UTC),
        account_username="lifter42",
        account_id="acct-1",
        workout_count=42,
    )


def _rejected_hevy() -> HevyCredentialTest:
    return HevyCredentialTest(
        state=HevyCredentialState.INVALID,
        tested_at=datetime.now(UTC),
        detail="Hevy rejected the credential.",
    )


def _events(db: str, *event_types: str) -> list[dict[str, Any]]:
    query = "SELECT * FROM security_audit_events"
    if event_types:
        query += f" WHERE event_type IN ({', '.join('?' * len(event_types))})"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query + " ORDER BY seq", event_types)]


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    return json.loads(event["metadata_json"])


class _FakeGoogle:
    def __init__(
        self, token: dict[str, Any] | None = None, error: Exception | None = None
    ):
        self.token, self.error = token, error

    async def authorize_access_token(self, _request: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return self.token or {}


class _FakeOAuth:
    def __init__(self, google: _FakeGoogle) -> None:
        self.google = google


@dataclass
class _Env:
    module: Any
    db: str
    user_a: str
    user_b: str
    client_for: Callable[[str | None], TestClient]


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    db = str(tmp_path / "audit-routes.db")
    init_db(db)
    for name, value in {
        "DATABASE_PATH": db,
        "APP_ENV": "test",
        "WEB_GOOGLE_CLIENT_ID": "audit-client-id",
        "WEB_GOOGLE_CLIENT_SECRET": "audit-client-secret",
        "WEB_AUTH_SECRET": _SECRET,
        "ENCRYPTION_KEY": _ENCRYPTION_KEY,
        "GOOGLE_HEALTH_CLIENT_ID": "gh-client-id",
        "GOOGLE_HEALTH_CLIENT_SECRET": "gh-client-secret",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AUDIT_LOG_HMAC_KEY", raising=False)
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    user_a = get_or_create_user("user-a@example.com", "User A", db_path=db)["id"]
    user_b = get_or_create_user("user-b@example.com", "User B", db_path=db)["id"]
    emails = {user_a: "user-a@example.com", user_b: "user-b@example.com"}

    def client_for(user_id: str | None) -> TestClient:
        client = TestClient(app_module.app)
        if user_id is not None:
            session_id = app_module.SESSION_STORE.create(user_id)
            client.cookies.set(
                "session", _cookie(user_id, emails[user_id], session_id)
            )
        return client

    return _Env(app_module, db, user_a, user_b, client_for)


def _fake_login(
    env: _Env, monkeypatch: pytest.MonkeyPatch, google: _FakeGoogle
) -> None:
    monkeypatch.setattr(env.module, "oauth", _FakeOAuth(google))


# -- login, failed login, session revocation -------------------------------------


def test_successful_login_is_attributed_correlated_and_pseudonymised(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_login(
        env,
        monkeypatch,
        _FakeGoogle(
            {
                "userinfo": {
                    "sub": "synthetic-subject-new-user",
                    "email": "new-user@example.com",
                    "email_verified": True,
                    "name": "New User",
                }
            }
        ),
    )

    response = env.client_for(None).get("/auth", follow_redirects=False)

    assert response.status_code == 307 and response.headers["location"] == "/"
    (event,) = _events(env.db, "auth.login")
    with sqlite3.connect(env.db) as conn:
        (new_user,) = conn.execute(
            "SELECT id FROM users WHERE email = 'new-user@example.com'"
        ).fetchone()
    assert (event["outcome"], event["severity"]) == ("success", "info")
    assert (event["actor_type"], event["actor_id"], event["user_id"]) == (
        "user",
        new_user,
        new_user,
    )
    assert (event["subject_type"], event["subject_id"]) == ("account", new_user)
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert _REQUEST_ID.fullmatch(event["request_id"])
    assert re.fullmatch(r"[0-9a-f]{24}", event["source_ref"])
    assert "testclient" not in repr(event)
    assert _metadata(event) == {"method": "google_oauth"}
    assert verify_chain(env.db, key=resolve_audit_key()).ok


def test_failed_logins_are_recorded_without_exception_text_and_repeats_alert(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_login(
        env,
        monkeypatch,
        _FakeGoogle(
            error=RuntimeError("state mismatch code=synthetic-oauth-code-123456")
        ),
    )
    client = env.client_for(None)

    responses = [client.get("/auth", follow_redirects=False) for _ in range(5)]

    assert {r.headers["location"] for r in responses} == {"/login"}
    failures = _events(env.db, "auth.login")
    assert len(failures) == 5
    for failure, response in zip(failures, responses, strict=True):
        assert (failure["outcome"], failure["actor_type"], failure["user_id"]) == (
            "failure",
            "anonymous",
            None,
        )
        assert failure["request_id"] == response.headers["X-Request-ID"]
        assert _metadata(failure) == {
            "reason": "oauth_exchange_failed",
            "error_type": "RuntimeError",
        }
    assert "synthetic-oauth-code" not in repr(_events(env.db))
    (alert,) = _events(env.db, "alert.repeated_auth_failures")
    assert (alert["severity"], alert["subject_type"]) == ("warning", "source")
    assert alert["subject_id"] == failures[0]["source_ref"]
    assert alert["request_id"] == failures[4]["request_id"]


def test_login_for_an_email_outside_the_allowlist_is_denied_and_only_pseudonymised(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        env.module,
        "ACCESS_POLICY",
        AccessPolicy(
            AccessPolicyConfig(allowed_emails=frozenset({"user-a@example.com"})),
            env.db,
        ),
    )
    _fake_login(
        env,
        monkeypatch,
        _FakeGoogle(
            {
                "userinfo": {
                    "sub": "synthetic-subject-intruder",
                    "email": "Intruder@Example.com",
                    "email_verified": True,
                }
            }
        ),
    )

    response = env.client_for(None).get("/auth", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=access_denied"
    (event,) = _events(env.db, "auth.login")
    assert (event["outcome"], event["severity"], event["actor_type"]) == (
        "denied",
        "warning",
        "anonymous",
    )
    assert event["subject_type"] == "identity"
    assert event["subject_id"] == resolve_audit_key().pseudonym(
        "identity", "intruder@example.com"
    )
    assert "intruder" not in repr(_events(env.db)).lower()
    assert _metadata(event) == {"reason": "access_policy_denied"}
    assert not any(
        row["event_type"] == "auth.login" and row["outcome"] == "success"
        for row in _events(env.db)
    )


def test_login_without_an_identity_is_a_recorded_failure(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_login(env, monkeypatch, _FakeGoogle({}))

    env.client_for(None).get("/auth", follow_redirects=False)

    (event,) = _events(env.db, "auth.login")
    assert (event["outcome"], _metadata(event)) == (
        "failure",
        {"reason": "missing_subject"},
    )


def test_logout_records_session_revocation_for_the_revoked_user(env: _Env) -> None:
    response = env.client_for(env.user_a).get("/logout", follow_redirects=False)

    assert response.headers["location"] == "/login"
    (event,) = _events(env.db, "auth.session_revoked")
    assert (event["actor_type"], event["actor_id"], event["user_id"]) == (
        "user",
        env.user_a,
        env.user_a,
    )
    assert (event["subject_type"], event["outcome"]) == ("session", "success")
    assert event["request_id"] == response.headers["X-Request-ID"]


def test_an_unauthenticated_logout_records_nothing(env: _Env) -> None:
    env.client_for(None).get("/logout", follow_redirects=False)

    assert _events(env.db) == []


# -- key and connector changes ---------------------------------------------------


def test_saving_and_deleting_a_hevy_key_records_credential_and_connector_changes(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        env.module,
        "verify_hevy_credentials",
        lambda _key: _connected_hevy(),
    )
    client = env.client_for(env.user_a)

    saved = client.post(
        "/api/settings/key", json={"provider": "hevy", "api_key": _HEVY_KEY}
    )
    removed = client.post("/api/settings/key/delete", json={"provider": "hevy"})

    assert saved.status_code == removed.status_code == 200
    events = _events(env.db)
    assert [(e["event_type"], e["subject_id"]) for e in events] == [
        ("credential.saved", "hevy"),
        ("connector.connected", "hevy"),
        ("credential.deleted", "hevy"),
        ("connector.disconnected", "hevy"),
    ]
    assert {e["user_id"] for e in events} == {env.user_a}
    assert {e["actor_id"] for e in events} == {env.user_a}
    assert {e["outcome"] for e in events} == {"success"}
    assert (
        events[0]["request_id"]
        == events[1]["request_id"]
        == saved.headers["X-Request-ID"]
    )
    assert (
        events[2]["request_id"]
        == events[3]["request_id"]
        == removed.headers["X-Request-ID"]
    )
    assert events[0]["request_id"] != events[2]["request_id"]
    assert _metadata(events[0]) == {
        "provider": "hevy",
        "credential_kind": "connector",
        "model_configured": False,
    }
    assert _HEVY_KEY not in repr(events)


def test_saving_an_ai_provider_key_is_a_credential_change_only(env: _Env) -> None:
    response = env.client_for(env.user_a).post(
        "/api/settings/key",
        json={"provider": "gemini", "api_key": _AI_KEY, "model": "synthetic-model"},
    )

    assert response.status_code == 200
    (event,) = _events(env.db)
    assert event["event_type"] == "credential.saved"
    assert _metadata(event) == {
        "provider": "gemini",
        "credential_kind": "ai_provider",
        "model_configured": True,
    }
    assert _AI_KEY not in repr(event) and "synthetic-model" not in repr(event)


def test_deleting_an_arbitrary_provider_string_is_never_written_to_the_trail(
    env: _Env,
) -> None:
    pasted_secret = "sk-" "synthetic-pasted-secret-0123456789abcdef"

    response = env.client_for(env.user_a).post(
        "/api/settings/key/delete", json={"provider": pasted_secret}
    )

    assert response.status_code == 400
    (event,) = _events(env.db)
    assert (event["event_type"], event["subject_id"], event["outcome"]) == (
        "credential.deleted",
        "unknown",
        "failure",
    )
    assert pasted_secret not in repr(event)


def test_hevy_connection_tests_are_recorded_for_both_outcomes(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = iter([_connected_hevy(), _rejected_hevy()])
    monkeypatch.setattr(
        env.module,
        "verify_hevy_credentials",
        lambda _key: next(results),
    )
    client = env.client_for(env.user_a)

    ok = client.post("/api/settings/verify-hevy", json={"api_key": _HEVY_KEY})
    bad = client.post("/api/settings/verify-hevy", json={"api_key": _HEVY_KEY})

    assert ok.status_code == 200 and ok.json()["status"] == "ok"
    assert bad.status_code == 400
    first, second = _events(env.db, "connector.test")
    assert (first["outcome"], second["outcome"]) == ("success", "failure")
    assert _metadata(second) == {"provider": "hevy", "reason": "provider_rejected"}
    assert _HEVY_KEY not in repr(_events(env.db))


def test_history_sync_records_counts_on_success_and_only_a_reason_on_failure(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    SecretVault(env.db, environ={"ENCRYPTION_KEY": _ENCRYPTION_KEY}).store(
        env.user_a, "hevy", api_key=_HEVY_KEY
    )
    results = iter(
        [
            {"workouts_found": 5, "processed": 4},
            {
                "error": f"provider said no for {_HEVY_KEY} token=synthetic-provider-token"
            },
        ]
    )
    monkeypatch.setattr(
        "scripts.sync_history.sync_all", lambda *_a, **_k: next(results)
    )
    client = env.client_for(env.user_a)

    assert client.post("/api/settings/sync-history").status_code == 200
    assert client.post("/api/settings/sync-history").status_code == 400

    first, second = _events(env.db, "connector.sync")
    assert (first["outcome"], second["outcome"]) == ("success", "failure")
    assert _metadata(first) == {
        "provider": "hevy",
        "records_found": 5,
        "records_processed": 4,
    }
    assert _metadata(second) == {"provider": "hevy", "reason": "provider_error"}
    assert "synthetic-provider-token" not in repr(_events(env.db))


def test_google_health_connect_denial_mismatch_failure_success_and_disconnect(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchanges = iter([None, {"refresh_token": "synthetic-refresh-token-value"}])
    monkeypatch.setattr(env.module, "exchange_code", lambda *_a, **_k: next(exchanges))
    client = env.client_for(env.user_a)

    def callback(query: str, state: str = "st-1") -> int:
        set_meta("google_health_oauth_state", state, env.db, user_id=env.user_a)
        return client.get(
            f"/google-health/callback?{query}", follow_redirects=False
        ).status_code

    assert callback("error=access_denied") == 303
    assert callback("code=synthetic-code&state=wrong") == 303
    assert callback("code=synthetic-code&state=st-1") == 303  # token exchange fails
    assert callback("code=synthetic-code&state=st-1") == 303  # succeeds
    assert (
        client.post("/google-health/disconnect", follow_redirects=False).status_code
        == 303
    )

    connect = _events(env.db, "connector.connected")
    assert [(e["outcome"], _metadata(e).get("reason")) for e in connect] == [
        ("denied", "consent_denied"),
        ("failure", "state_mismatch"),
        ("failure", "token_exchange_failed"),
        ("success", None),
    ]
    (disconnect,) = _events(env.db, "connector.disconnected")
    assert (
        disconnect["subject_id"] == "google_health"
        and disconnect["user_id"] == env.user_a
    )
    assert "synthetic-refresh-token-value" not in repr(_events(env.db))
    assert "synthetic-code" not in repr(_events(env.db))


# -- deletion, permission change, tenancy ----------------------------------------


def test_clearing_chat_history_and_granting_push_are_recorded_without_secrets(
    env: _Env,
) -> None:
    client = env.client_for(env.user_a)

    assert client.post("/api/chat/clear").status_code == 200
    subscription = {
        "endpoint": "https://push.example.com/synthetic-endpoint-0123456789",
        "keys": {"p256dh": "synthetic-p256dh-value", "auth": "synthetic-auth-value"},
    }
    assert (
        client.post("/api/settings/push-subscribe", json=subscription).status_code
        == 200
    )

    deleted, permission = _events(env.db)
    assert (deleted["event_type"], deleted["subject_type"], deleted["subject_id"]) == (
        "data.deleted",
        "data",
        "chat_history",
    )
    assert (permission["event_type"], permission["subject_id"]) == (
        "permission.changed",
        "push_notifications",
    )
    assert _metadata(permission) == {"change": "granted"}
    stored = repr(_events(env.db))
    for secret in ("synthetic-endpoint", "synthetic-p256dh", "synthetic-auth-value"):
        assert secret not in stored


def test_changing_the_ai_provider_is_a_permission_change_but_resaving_it_is_not(
    env: _Env,
) -> None:
    client = env.client_for(env.user_a)
    body = {"preferred_ai": "claude", "goals": ["synthetic private goal"]}

    assert client.post("/api/settings/preferences", json=body).status_code == 200
    assert client.post("/api/settings/preferences", json=body).status_code == 200
    assert (
        client.post("/api/settings/preferences", json={"goals": ["x"]}).status_code
        == 200
    )
    assert (
        client.post(
            "/api/settings/preferences", json={"preferred_ai": "openai"}
        ).status_code
        == 200
    )

    changes = _events(env.db, "permission.changed")
    assert [(e["subject_id"], _metadata(e)) for e in changes] == [
        ("ai_processor", {"change": "provider_selected", "provider": "claude"}),
        ("ai_processor", {"change": "provider_selected", "provider": "openai"}),
    ]
    assert {e["user_id"] for e in changes} == {env.user_a}
    assert "synthetic private goal" not in repr(_events(env.db))


def test_each_event_is_attributed_to_the_session_user_and_never_to_a_claimed_owner(
    env: _Env,
) -> None:
    client_a, client_b = env.client_for(env.user_a), env.client_for(env.user_b)

    assert client_a.post("/api/chat/clear").status_code == 200
    assert client_b.post("/api/chat/clear").status_code == 200
    spoofed = client_a.post(
        "/api/settings/key",
        json={"provider": "hevy", "api_key": _HEVY_KEY, "user_id": env.user_b},
    )
    spoofed_header = client_a.post("/api/chat/clear", headers={"X-User-Id": env.user_b})

    assert spoofed.status_code == spoofed_header.status_code == 404
    events = _events(env.db)
    assert [(e["user_id"], e["actor_id"]) for e in events] == [
        (env.user_a, env.user_a),
        (env.user_b, env.user_b),
    ]
    assert not _events(env.db, "credential.saved", "connector.connected")


def test_an_audit_storage_outage_never_breaks_the_user_request(
    env: _Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def locked(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(audit, "append_event", locked)

    with caplog.at_level(logging.ERROR, logger="security_audit"):
        response = env.client_for(env.user_a).post("/api/chat/clear")

    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert "security_audit_write_failed event_type=data.deleted" in caplog.text
    assert _events(env.db) == []


# -- production tripwires --------------------------------------------------------


@pytest.fixture()
def anonymous_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, str]:
    db = str(tmp_path / "anonymous.db")
    init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    for name in (
        "WEB_GOOGLE_CLIENT_ID",
        "WEB_GOOGLE_CLIENT_SECRET",
        "WEB_AUTH_SECRET",
        "AUDIT_LOG_HMAC_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    return app_module, db


def test_anonymous_access_outside_production_raises_no_alert(
    anonymous_app: tuple[Any, str],
) -> None:
    module, db = anonymous_app
    client = TestClient(module.app)

    assert client.get("/api/notifications").status_code == 200
    assert client.get("/logout", follow_redirects=False).status_code == 307

    assert _events(db) == []  # an anonymous logout has no session to revoke


def test_anonymous_request_refused_under_production_env_raises_a_critical_alert(
    anonymous_app: tuple[Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, db = anonymous_app
    client = TestClient(module.app)
    assert client.get("/api/notifications").status_code == 200  # builds the app stack
    assert _events(db) == []

    monkeypatch.setenv("APP_ENV", "production")  # the process posture regressed
    response = client.get("/api/notifications")

    # Fail closed: the local fallback tenant is never served once the process
    # claims to be production, but the unsafe posture is still alerted on.
    assert response.status_code == 401
    (alert,) = _events(db, "alert.production_auth_disabled")
    assert (alert["severity"], alert["outcome"], alert["actor_type"]) == (
        "critical",
        "raised",
        "system",
    )
    assert alert["request_id"] == response.headers["X-Request-ID"]
    assert _metadata(alert)["detected_by"] == "anonymous_request"
    assert _metadata(alert)["authentication_enabled"] is False
    client.get("/api/notifications")
    assert (
        len(_events(db, "alert.production_auth_disabled")) == 1
    )  # cooldown de-duplicates


def test_startup_under_a_production_env_without_auth_raises_a_critical_alert(
    anonymous_app: tuple[Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, db = anonymous_app
    monkeypatch.setenv("APP_ENV", "production")

    async def start() -> None:
        async with module._lifespan(module.app):
            pass

    asyncio.run(start())

    (alert,) = _events(db, "alert.production_auth_disabled")
    assert _metadata(alert)["detected_by"] == "startup"
    assert alert["request_id"] is None


def test_startup_rejects_a_malformed_audit_key(
    anonymous_app: tuple[Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _db = anonymous_app
    monkeypatch.setenv("AUDIT_LOG_HMAC_KEY", "too-short")

    async def start() -> None:
        async with module._lifespan(module.app):
            pass

    with pytest.raises(SecurityAuditConfigurationError, match="at least 32"):
        asyncio.run(start())
