"""HTTP contract tests for account and health-data deletion."""

from __future__ import annotations

import base64
import importlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import account_deletion as ad
import pytest
from database import (
    get_or_create_user,
    init_db,
    save_body_metrics,
    save_chat_message,
    save_notification,
    save_user_api_key,
)
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

_SECRET = "account-deletion-test-secret-that-is-long-enough"
EMAIL_A = "athlete.a@example.com"
EMAIL_B = "athlete.b@example.com"


def _cookie(email: str, user_id: str) -> str:
    payload = base64.b64encode(json.dumps({"user": email, "user_id": user_id}).encode())
    return TimestampSigner(_SECRET).sign(payload).decode()


class Env(NamedTuple):
    db: str
    a: str
    b: str
    client_for: Callable[[str, str], TestClient]


def _load_app(monkeypatch: pytest.MonkeyPatch, db: str, *, authenticated: bool) -> Any:
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("ACCOUNT_DELETION_GRACE_DAYS", raising=False)
    if authenticated:
        monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "deletion-client-id")
        monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "deletion-client-secret")
        monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    else:
        monkeypatch.delenv("WEB_GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("WEB_GOOGLE_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    import webapp.app as webapp_app

    return importlib.reload(webapp_app)


def _seed(db: str, user_id: str) -> None:
    save_chat_message("user", "synthetic message", db_path=db, user_id=user_id)
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, user_id=user_id)
    save_notification(user_id, "Title", "Message", db_path=db)
    save_user_api_key(user_id, "hevy", "synthetic-hevy-key", db_path=db)
    save_user_api_key(user_id, "garmin", "synthetic-health-key", db_path=db)


def _count(db: str, table: str, user_id: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    db = str(tmp_path / "api.db")
    init_db(db)
    a = get_or_create_user(EMAIL_A, "Athlete A", db_path=db)["id"]
    b = get_or_create_user(EMAIL_B, "Athlete B", db_path=db)["id"]
    _seed(db, a)
    _seed(db, b)
    webapp_app = _load_app(monkeypatch, db, authenticated=True)

    def client_for(email: str, user_id: str) -> TestClient:
        client = TestClient(webapp_app.app)
        client.cookies.set("session", _cookie(email, user_id))
        return client

    return Env(db, a, b, client_for)


def _confirm(email: str) -> dict[str, str]:
    return {"scope": "account", "confirmation": email}


HEALTH = {"scope": "health_data", "confirmation": ad.HEALTH_CONFIRMATION}


# ---------------------------------------------------------------------------
# Authentication and ownership
# ---------------------------------------------------------------------------


def test_every_deletion_route_requires_a_signed_in_user(env: Env) -> None:
    anonymous = TestClient(importlib.import_module("webapp.app").app)
    assert anonymous.get("/api/account/deletion").status_code == 401
    assert anonymous.post("/api/account/deletion", json=HEALTH).status_code == 401
    assert anonymous.post("/api/account/deletion/cancel").status_code == 401


def test_responses_are_never_cacheable(env: Env) -> None:
    response = env.client_for(EMAIL_A, env.a).get("/api/account/deletion")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]


def test_status_describes_the_exact_confirmation_text(env: Env) -> None:
    body = env.client_for(EMAIL_A, env.a).get("/api/account/deletion").json()

    assert body["eligible"] is True
    scopes = {item["scope"]: item for item in body["scopes"]}
    assert scopes["account"]["required_confirmation"] == EMAIL_A
    assert scopes["account"]["grace_days"] == 7
    assert scopes["health_data"]["required_confirmation"] == ad.HEALTH_CONFIRMATION
    assert body["request"] is None


def test_client_supplied_owner_ids_are_never_used(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    before = _count(env.db, "chat_messages", env.b)

    other = client.post("/api/account/deletion", json={**HEALTH, "user_id": env.b})
    own = client.post("/api/account/deletion", json={**HEALTH, "user_id": env.a})
    header = client.post(
        "/api/account/deletion", json=HEALTH, headers={"X-User-Id": env.b}
    )

    assert other.status_code == 404  # non-enumerating rejection by the mutation guard
    assert own.status_code == 422  # unknown fields are refused outright
    assert header.status_code == 404
    assert _count(env.db, "chat_messages", env.b) == before
    assert _count(env.db, "chat_messages", env.a) == 1
    with sqlite3.connect(env.db) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM account_deletion_requests").fetchone()[0]
            == 0
        )


def test_users_cannot_see_or_cancel_each_others_requests(env: Env) -> None:
    client_a = env.client_for(EMAIL_A, env.a)
    client_b = env.client_for(EMAIL_B, env.b)
    assert (
        client_a.post("/api/account/deletion", json=_confirm(EMAIL_A)).status_code
        == 202
    )

    assert client_b.get("/api/account/deletion").json()["request"] is None
    assert client_b.post("/api/account/deletion/cancel").status_code == 404
    assert client_a.get("/api/account/deletion").json()["request"]["state"] == "pending"


def test_anonymous_mode_cannot_delete_the_shared_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "anonymous.db")
    init_db(db)
    client = TestClient(_load_app(monkeypatch, db, authenticated=False).app)

    status = client.get("/api/account/deletion")
    assert status.status_code == 200
    assert status.json() == {
        "eligible": False,
        "reason": "not_eligible",
        "request": None,
    }
    # Mutations are never anonymous, so nothing can even be asked of the shared tenant.
    assert client.post("/api/account/deletion", json=HEALTH).status_code == 401


def test_signed_in_session_for_the_legacy_tenant_is_refused(env: Env) -> None:
    with sqlite3.connect(env.db) as conn:
        legacy_id = conn.execute(
            "SELECT id FROM users WHERE email = 'legacy@local'"
        ).fetchone()[0]
    client = env.client_for("legacy@local", legacy_id)

    response = client.post("/api/account/deletion", json=_confirm("legacy@local"))

    assert response.status_code == 403
    assert response.json()["detail"] == "not_eligible"


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"scope": "account", "confirmation": "wrong"},
        {"scope": "account", "confirmation": EMAIL_B},
        {"scope": "health_data", "confirmation": EMAIL_A},
        {"scope": "health_data", "confirmation": "delete"},
    ],
)
def test_wrong_confirmation_is_rejected_and_changes_nothing(
    env: Env, payload: dict[str, str]
) -> None:
    response = env.client_for(EMAIL_A, env.a).post(
        "/api/account/deletion", json=payload
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "confirmation_mismatch"
    assert _count(env.db, "chat_messages", env.a) == 1
    assert ad.account_gate(env.db, env.a) is ad.AccountGate.OPEN


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"scope": "account"},
        {"confirmation": EMAIL_A},
        {"scope": "everything", "confirmation": EMAIL_A},
        {"scope": "account", "confirmation": ""},
        {"scope": "account", "confirmation": "x" * 400},
    ],
)
def test_malformed_requests_fail_validation(env: Env, payload: dict[str, Any]) -> None:
    response = env.client_for(EMAIL_A, env.a).post(
        "/api/account/deletion", json=payload
    )
    assert response.status_code == 422
    assert ad.account_gate(env.db, env.a) is ad.AccountGate.OPEN


# ---------------------------------------------------------------------------
# Health-data erasure (immediate)
# ---------------------------------------------------------------------------


def test_health_data_erasure_completes_inline_and_keeps_the_session(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)

    response = client.post("/api/account/deletion", json=HEALTH)

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "completed"
    assert body["progress_percent"] == 100
    assert "clear-site-data" not in response.headers
    assert _count(env.db, "body_metrics", env.a) == 0
    assert _count(env.db, "chat_messages", env.a) == 0
    assert _count(env.db, "notifications", env.a) == 1  # not health data
    with sqlite3.connect(env.db) as conn:
        providers = {
            row[0]
            for row in conn.execute(
                "SELECT provider FROM user_api_keys WHERE user_id = ?", (env.a,)
            )
        }
    assert providers == {"hevy"}
    # Other tenant untouched, and the signed-in session still works.
    assert _count(env.db, "body_metrics", env.b) == 1
    assert client.get("/api/notifications").status_code == 200
    assert client.get("/api/account/deletion").json()["request"]["state"] == "completed"


# ---------------------------------------------------------------------------
# Account closure: confirm, wait, cancel, execute
# ---------------------------------------------------------------------------


def test_account_closure_is_scheduled_and_the_account_stays_usable(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)

    response = client.post("/api/account/deletion", json=_confirm(EMAIL_A))

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "pending"
    assert body["can_cancel"] is True
    assert body["progress_percent"] == 0
    assert datetime.fromisoformat(body["execute_after"]) - datetime.fromisoformat(
        body["requested_at"]
    ) == timedelta(days=7)
    assert [step["step"] for step in body["steps"]][-1] == "account_identity"
    # Nothing is erased yet, the user can keep using the app, and background work is paused.
    assert _count(env.db, "chat_messages", env.a) == 1
    assert client.get("/api/notifications").status_code == 200
    assert ad.is_account_closing(env.db, env.a) is True


def test_a_second_request_while_one_is_open_conflicts(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    assert (
        client.post("/api/account/deletion", json=_confirm(EMAIL_A)).status_code == 202
    )

    again = client.post("/api/account/deletion", json=HEALTH)

    assert again.status_code == 409
    assert again.json()["detail"] == "already_requested"


def test_cancelling_inside_the_window_through_the_api(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    assert (
        client.post("/api/account/deletion", json=_confirm(EMAIL_A)).status_code == 202
    )

    cancelled = client.post("/api/account/deletion/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert ad.account_gate(env.db, env.a) is ad.AccountGate.OPEN
    assert client.post("/api/account/deletion/cancel").status_code == 404
    assert _count(env.db, "chat_messages", env.a) == 1


def test_cancelling_after_the_window_closed_conflicts(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    client.post("/api/account/deletion", json=_confirm(EMAIL_A))
    with sqlite3.connect(env.db) as conn:
        past = ad._ts(datetime.now(tz=timezone.utc) - timedelta(minutes=1))
        conn.execute("UPDATE account_deletion_requests SET execute_after = ?", (past,))

    response = client.post("/api/account/deletion/cancel")

    assert response.status_code == 409
    assert response.json()["detail"] == "not_cancellable"


def test_the_account_is_locked_while_erasure_runs_but_progress_stays_visible(
    env: Env,
) -> None:
    client = env.client_for(EMAIL_A, env.a)
    request = client.post("/api/account/deletion", json=_confirm(EMAIL_A)).json()
    due = datetime.now(tz=timezone.utc) + timedelta(days=8)
    assert ad._claim(env.db, request["request_id"], due) is not None

    locked = client.get("/api/notifications")
    settings = client.get("/api/settings")  # hosts the progress view, so stays readable
    status = client.get("/api/account/deletion")
    retry = client.post("/api/account/deletion", json=HEALTH)
    cancel = client.post("/api/account/deletion/cancel")
    mutate = client.post("/api/chat/clear")

    assert locked.status_code == 423
    assert settings.status_code == 200
    assert mutate.status_code == 423
    assert retry.status_code == 423
    assert status.status_code == 200
    assert status.json()["request"]["state"] == "in_progress"
    assert cancel.status_code == 409
    assert cancel.json()["detail"] == "not_cancellable"
    # The other tenant is unaffected.
    assert env.client_for(EMAIL_B, env.b).get("/api/notifications").status_code == 200


def test_closed_account_sessions_fail_closed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = env.client_for(EMAIL_A, env.a)
    assert (
        client.post("/api/account/deletion", json=_confirm(EMAIL_A)).status_code == 202
    )
    ad.process_due_deletions(
        env.db, now=datetime.now(tz=timezone.utc) + timedelta(days=8)
    )

    # The user row is gone, the tombstone exists, and every old cookie is refused.
    assert ad.account_gate(env.db, env.a) is ad.AccountGate.CLOSED
    stale = env.client_for(EMAIL_A, env.a)
    assert env.client_for(EMAIL_B, env.b).get("/api/me").status_code == 200
    for path in (
        "/api/me",  # the SPA auth guard must not treat a closed account as signed in
        "/api/notifications",
        "/api/account/deletion",
        "/api/settings",
        "/api/chat/history",
    ):
        assert stale.get(path).status_code == 401, path
    assert stale.post("/api/account/deletion/cancel").status_code == 401
    assert (
        stale.post("/api/account/deletion", json=_confirm(EMAIL_A)).status_code == 401
    )
    assert env.client_for(EMAIL_B, env.b).get("/api/notifications").status_code == 200
    # Nothing was resurrected by those rejected calls.
    with sqlite3.connect(env.db) as conn:
        for table in (
            "notifications",
            "chat_messages",
            "user_api_keys",
            "body_metrics",
        ):
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (env.a,)
                ).fetchone()[0]
                == 0
            ), table


def test_immediate_closure_clears_the_session_and_browser_state(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_GRACE_DAYS", "0")
    client = env.client_for(EMAIL_A, env.a)

    response = client.post("/api/account/deletion", json=_confirm(EMAIL_A))

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "completed"
    assert all(
        step["state"] == "done" and step["remaining_after"] == 0
        for step in body["steps"]
    )
    assert response.headers["clear-site-data"] == '"cache", "cookies", "storage"'
    assert "session=" in response.headers.get("set-cookie", "")
    assert ad.account_gate(env.db, env.a) is ad.AccountGate.CLOSED
    assert _count(env.db, "chat_messages", env.a) == 0
    assert _count(env.db, "chat_messages", env.b) == 1
    with sqlite3.connect(env.db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE id = ?", (env.a,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE id = ?", (env.b,)
            ).fetchone()[0]
            == 1
        )


def test_a_failed_inline_run_is_reported_not_hidden(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("synthetic filesystem failure")

    monkeypatch.setattr(ad, "_erase_step", broken)
    client = env.client_for(EMAIL_A, env.a)

    response = client.post("/api/account/deletion", json=HEALTH)

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "failed"
    assert body["error_code"] == "filesystem_error"
    assert "synthetic" not in response.text  # no internal detail leaks
    assert _count(env.db, "chat_messages", env.a) == 1
    assert client.get("/api/notifications").status_code == 423  # stays fenced
