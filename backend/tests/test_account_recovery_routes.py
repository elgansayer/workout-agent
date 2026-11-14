"""HTTP coverage for anonymous recovery and authenticated e-mail changes."""

from __future__ import annotations

import importlib
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from accounts import resolve_oauth_user, set_account_status
from database import init_db
from fastapi.testclient import TestClient

EMAIL = "person@example.com"
NEW_EMAIL = "new-address@example.com"
SECRET = "account-recovery-route-test-secret-long-enough"


class Captured:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def __call__(self, to: str, subject: str, body: str) -> None:
        self.messages.append((to, subject, body))

    def token(self, index: int = 0) -> str:
        match = re.search(r"https?://[^\s]+", self.messages[index][2])
        assert match is not None
        return parse_qs(urlsplit(match.group(0)).query)["token"][0]


@pytest.fixture()
def recovery_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo_root = Path(__file__).resolve().parents[2]
    (repo_root / "frontend" / "dist" / "frontend" / "browser").mkdir(
        parents=True, exist_ok=True
    )
    db = str(tmp_path / "recovery-routes.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    monkeypatch.setenv("WEB_AUTH_SECRET", SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "recovery-route-client")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "recovery-route-client-secret")
    monkeypatch.setenv("WEB_PUBLIC_URL", "https://workout.example.test")
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    init_db(db)
    mail = Captured()
    monkeypatch.setattr(app_module.email_delivery, "send_email", mail)
    return app_module, db, mail


def _account(db: str, *, status: str | None = None) -> str:
    row = resolve_oauth_user(
        provider="google",
        subject="route-subject",
        email=EMAIL,
        email_verified=True,
        display_name="Person",
        db_path=db,
    )
    if status is not None:
        set_account_status(row["id"], status, db_path=db)
    return str(row["id"])


def _user_email(db: str, user_id: str) -> str:
    with sqlite3.connect(db) as conn:
        return str(
            conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        )


def test_recovery_request_is_non_enumerating_for_known_and_unknown_addresses(
    recovery_app,
) -> None:
    app_module, db, mail = recovery_app
    _account(db)

    with TestClient(app_module.app, base_url="https://testserver") as client:
        unknown = client.post(
            "/api/auth/recovery/request", json={"email": "missing@example.com"}
        )
        known = client.post("/api/auth/recovery/request", json={"email": EMAIL})

    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert len(mail.messages) == 1


def test_a_recovery_token_creates_a_session_and_cannot_be_replayed(
    recovery_app,
) -> None:
    app_module, db, mail = recovery_app
    _account(db)

    with TestClient(app_module.app, base_url="https://testserver") as client:
        assert (
            client.post("/api/auth/recovery/request", json={"email": EMAIL}).status_code
            == 202
        )
        token = mail.token()
        completed = client.post("/api/auth/recovery/complete", json={"token": token})
        assert completed.status_code == 200
        me = client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["user"] == EMAIL

        replay = client.post("/api/auth/recovery/complete", json={"token": token})
        assert replay.status_code == 400


def test_a_suspended_account_never_receives_a_recovery_message(recovery_app) -> None:
    app_module, db, mail = recovery_app
    _account(db, status="suspended")

    with TestClient(app_module.app, base_url="https://testserver") as client:
        response = client.post("/api/auth/recovery/request", json={"email": EMAIL})

    assert response.status_code == 202
    assert mail.messages == []


def test_verified_email_change_alerts_the_previous_address_and_ends_sessions(
    recovery_app,
) -> None:
    app_module, db, mail = recovery_app
    user_id = _account(db)

    with TestClient(app_module.app, base_url="https://testserver") as client:
        client.post("/api/auth/recovery/request", json={"email": EMAIL})
        recovery_token = mail.token()
        assert (
            client.post(
                "/api/auth/recovery/complete", json={"token": recovery_token}
            ).status_code
            == 200
        )

        requested = client.post(
            "/api/account/email/change-request", json={"new_email": NEW_EMAIL}
        )
        assert requested.status_code == 202
        assert [to for to, _subject, _body in mail.messages[-2:]] == [
            NEW_EMAIL,
            EMAIL,
        ]
        change_token = mail.token(len(mail.messages) - 2)

        confirmed = client.post(
            "/api/account/email/confirm", json={"token": change_token}
        )
        assert confirmed.status_code == 200
        assert _user_email(db, user_id) == NEW_EMAIL
        assert mail.messages[-1][0] == EMAIL
        assert "was changed" in mail.messages[-1][1]

        # The user is signed out after the change and cannot replay the token.
        assert client.get("/api/me").status_code == 401
        assert (
            client.post(
                "/api/account/email/confirm", json={"token": change_token}
            ).status_code
            == 400
        )


def test_email_change_requires_recent_authentication(recovery_app) -> None:
    app_module, db, mail = recovery_app
    user_id = _account(db)

    with TestClient(app_module.app, base_url="https://testserver") as client:
        client.post("/api/auth/recovery/request", json={"email": EMAIL})
        client.post(
            "/api/auth/recovery/complete", json={"token": mail.token()}
        )
        # Simulate an old session without touching the token under test.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE user_sessions SET created_at = created_at - 3600 "
                "WHERE user_id = ?",
                (user_id,),
            )
        response = client.post(
            "/api/account/email/change-request", json={"new_email": NEW_EMAIL}
        )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Sign in again before changing the account e-mail address."
    )


def test_public_recovery_routes_reject_invalid_tokens(recovery_app) -> None:
    app_module, _db, _mail = recovery_app
    with TestClient(app_module.app, base_url="https://testserver") as client:
        assert (
            client.post(
                "/api/auth/recovery/complete", json={"token": "not-a-real-token"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/account/email/confirm", json={"token": "not-a-real-token"}
            ).status_code
            == 400
        )
