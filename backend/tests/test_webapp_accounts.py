"""Web boundary of the production user model.

Sign-in must bind the provider's stable subject to an ``active`` account, every
authenticated request must re-check that account, and production must have no
anonymous fallback user. The identity provider is stubbed; all data is synthetic.
"""

from __future__ import annotations

import base64
import importlib
import json
import logging
import sqlite3
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NamedTuple

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from accounts import set_account_status
from database import get_all_users, get_legacy_user_id, get_or_create_user, init_db
from fastapi import HTTPException
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

_SECRET = "webapp-accounts-test-secret-that-is-long-enough"
_EMAIL = "person@example.com"
_SUBJECT = "synthetic-google-subject-1"


class Web(NamedTuple):
    client: TestClient
    module: ModuleType
    db: str


@pytest.fixture()
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Web:
    db = str(tmp_path / "web-accounts.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "accounts-test-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "accounts-test-client-secret")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    init_db(db)

    import webapp.app as app_module

    importlib.reload(app_module)
    return Web(TestClient(app_module.app), app_module, db)


def _claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": _SUBJECT,
        "email": _EMAIL,
        "email_verified": True,
        "name": "Synthetic Person",
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


def _stub_google(
    monkeypatch: pytest.MonkeyPatch, web: Web, claims: dict[str, Any]
) -> None:
    async def authorize_access_token(_request: Any) -> dict[str, Any]:
        return {"userinfo": claims}

    monkeypatch.setattr(
        web.module.oauth.google, "authorize_access_token", authorize_access_token
    )


def _sign_in(
    monkeypatch: pytest.MonkeyPatch, web: Web, **overrides: Any
) -> Any:
    _stub_google(monkeypatch, web, _claims(**overrides))
    return web.client.get("/auth", follow_redirects=False)


def _session_cookie(user_id: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user": _EMAIL, "user_id": user_id}).encode()
    )
    return TimestampSigner(_SECRET).sign(payload).decode()


def _decode_session(cookie: str) -> dict[str, Any]:
    payload = TimestampSigner(_SECRET).unsign(cookie.encode())
    return dict(json.loads(base64.b64decode(payload)))


def _identities(db: str) -> list[tuple[str, str, int]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT provider, subject, email_verified FROM user_identities "
            "ORDER BY id"
        ).fetchall()


def _account_id(db: str, email: str = _EMAIL) -> str:
    return next(
        user["id"]
        for user in get_all_users(db, include_inactive=True)
        if user["email"] == email
    )


# ---------------------------------------------------------------------------
# /auth: sign-in binds a verified identity to an active account
# ---------------------------------------------------------------------------


def test_first_verified_sign_in_creates_an_account_bound_to_the_session(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _sign_in(monkeypatch, web, email="Person@Example.com")

    assert response.status_code in {302, 303, 307}
    assert response.headers["location"] == "/"
    users = get_all_users(web.db)
    assert [(user["email"], user["status"]) for user in users] == [
        (_EMAIL, "active")
    ]
    assert _identities(web.db) == [("google", _SUBJECT, 1)]
    # The session names the account by its immutable id, not by an email.
    session = _decode_session(web.client.cookies["session"])
    assert session["user_id"] == users[0]["id"]
    assert web.client.get("/api/me").status_code == 200


def test_a_repeat_sign_in_reuses_the_account(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sign_in(monkeypatch, web)
    first = _account_id(web.db)
    web.client.cookies.clear()

    _sign_in(monkeypatch, web)

    assert _account_id(web.db) == first
    assert len(get_all_users(web.db)) == 1
    assert len(_identities(web.db)) == 1
    assert _decode_session(web.client.cookies["session"])["user_id"] == first


def test_a_pre_identity_account_is_adopted_by_its_first_verified_sign_in(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = get_or_create_user(_EMAIL, db_path=web.db)["id"]

    _sign_in(monkeypatch, web)

    assert _account_id(web.db) == existing
    assert len(get_all_users(web.db)) == 1
    assert _identities(web.db) == [("google", _SUBJECT, 1)]
    assert _decode_session(web.client.cookies["session"])["user_id"] == existing


@pytest.mark.parametrize(
    "verified",
    [False, "false", "no", None],
    ids=["false", "string-false", "string-no", "missing"],
)
def test_an_unverified_email_is_refused_and_creates_nothing(
    web: Web, monkeypatch: pytest.MonkeyPatch, verified: object
) -> None:
    response = _sign_in(monkeypatch, web, email_verified=verified)

    assert response.status_code == 403
    assert get_all_users(web.db, include_inactive=True) == []
    assert _identities(web.db) == []
    assert "session" not in web.client.cookies


def test_a_missing_subject_is_refused_and_creates_nothing(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _sign_in(monkeypatch, web, sub=None)

    assert response.status_code == 403
    assert get_all_users(web.db, include_inactive=True) == []
    assert "session" not in web.client.cookies


@pytest.mark.parametrize("status", ["suspended", "deleted", "retired", "unclaimed"])
def test_an_inactive_account_cannot_sign_in(
    web: Web, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    _sign_in(monkeypatch, web)
    account = _account_id(web.db)
    web.client.cookies.clear()
    with sqlite3.connect(web.db) as conn:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, account))

    response = _sign_in(monkeypatch, web)

    assert response.status_code == 403
    assert "not active" in response.text
    assert "session" not in web.client.cookies


def test_a_reassigned_email_cannot_take_over_a_bound_account(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sign_in(monkeypatch, web)
    web.client.cookies.clear()

    response = _sign_in(monkeypatch, web, sub="a-different-synthetic-subject")

    assert response.status_code == 403
    assert len(get_all_users(web.db)) == 1
    assert len(_identities(web.db)) == 1
    assert "session" not in web.client.cookies


def test_refusals_never_echo_the_email_or_the_subject(
    web: Web, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    refusals = [
        _sign_in(monkeypatch, web, email_verified=False),
        _sign_in(monkeypatch, web, sub=None),
    ]
    _sign_in(monkeypatch, web)
    web.client.cookies.clear()
    refusals.append(_sign_in(monkeypatch, web, sub="another-synthetic-subject"))
    set_account_status(_account_id(web.db), "suspended", web.db)
    refusals.append(_sign_in(monkeypatch, web))

    assert [response.status_code for response in refusals] == [403] * 4
    leaked = " ".join([response.text for response in refusals] + [caplog.text])
    assert _EMAIL not in leaked.lower()
    assert _SUBJECT not in leaked
    assert "another-synthetic-subject" not in leaked


# ---------------------------------------------------------------------------
# Every authenticated request re-checks the account
# ---------------------------------------------------------------------------


def test_a_suspension_takes_effect_on_the_very_next_request(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sign_in(monkeypatch, web)
    account = _account_id(web.db)
    assert web.client.get("/api/notifications").status_code == 200
    assert web.client.get("/api/me").status_code == 200

    set_account_status(account, "suspended", web.db)

    assert web.client.get("/api/notifications").status_code == 401
    assert web.client.get("/api/me").status_code == 401

    set_account_status(account, "active", web.db)

    assert web.client.get("/api/notifications").status_code == 200


def test_a_validly_signed_session_for_an_unknown_account_is_unauthorized(
    web: Web,
) -> None:
    web.client.cookies.set("session", _session_cookie("no-such-account"))

    assert web.client.get("/api/notifications").status_code == 401
    assert web.client.get("/api/me").status_code == 401


def test_a_session_cannot_read_another_accounts_notifications(
    web: Web,
) -> None:
    from database import save_notification

    mine = get_or_create_user(_EMAIL, db_path=web.db)["id"]
    theirs = get_or_create_user("other@example.com", db_path=web.db)["id"]
    save_notification(theirs, "Private", "Not yours", db_path=web.db)
    web.client.cookies.set("session", _session_cookie(mine))

    response = web.client.get("/api/notifications", params={"user_id": theirs})

    assert response.status_code == 200
    assert response.json()["notifications"] == []


# ---------------------------------------------------------------------------
# No anonymous fallback user outside the explicit local mode
# ---------------------------------------------------------------------------


def _anonymous_request() -> SimpleNamespace:
    return SimpleNamespace(session={})


def test_production_has_no_anonymous_fallback_user(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    monkeypatch.setattr(web.module, "ALLOW_ANONYMOUS_WEB", True)

    with pytest.raises(HTTPException) as unauthorised:
        web.module._check_api_auth(_anonymous_request())

    assert unauthorised.value.status_code == 401


def test_the_local_mode_resolves_its_single_tenant_only_when_both_gates_are_set(
    tmp_path: Path, web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_db = str(tmp_path / "local.db")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    init_db(local_db)
    monkeypatch.setattr(web.module, "DB_PATH", local_db)
    monkeypatch.setattr(web.module, "ALLOW_ANONYMOUS_WEB", True)

    assert web.module._check_api_auth(_anonymous_request()) == get_legacy_user_id(
        local_db
    )

    # Either gate closing is enough to fail closed again.
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    with pytest.raises(HTTPException):
        web.module._check_api_auth(_anonymous_request())
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(HTTPException):
        web.module._check_api_auth(_anonymous_request())


@pytest.mark.parametrize(
    "session",
    [{}, {"user": _EMAIL}, {"user_id": ""}, {"user_id": "   "}, {"user_id": 7}],
    ids=["empty", "email-only", "blank-id", "whitespace-id", "non-string-id"],
)
def test_an_incomplete_session_is_never_an_account(
    web: Web, session: dict[str, Any]
) -> None:
    with pytest.raises(HTTPException) as unauthorised:
        web.module._check_api_auth(SimpleNamespace(session=session))

    assert unauthorised.value.status_code == 401
