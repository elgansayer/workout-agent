"""Invitation lifecycle and invite-only beta regression coverage."""

from __future__ import annotations

import base64
import importlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from database import get_or_create_user, get_user_by_email, init_db
from invitations import (
    InvalidInvitation,
    InvitationConflict,
    InvitationRateLimited,
    accept_invitation,
    consume_rate_limit,
    create_invitation,
    enforce_rate_limit,
    get_invitation,
    list_invitations,
    revoke_invitation,
)
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

_SECRET = "invitations-test-secret-that-is-long-enough"
_ADMIN_EMAIL = "admin@example.test"


def _db(tmp_path: Path) -> str:
    path = str(tmp_path / "invitations.db")
    init_db(path)
    return path


def _session_cookie(session: dict[str, Any]) -> str:
    payload = base64.b64encode(json.dumps(session).encode())
    return TimestampSigner(_SECRET).sign(payload).decode()


def _app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, str]:
    db_path = _db(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "invitations-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "invitations-client-secret")
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", frozenset({_ADMIN_EMAIL}))
    admin = get_or_create_user(_ADMIN_EMAIL, "Admin", db_path=db_path)
    client = TestClient(app_module.app)
    client.cookies.set(
        "session",
        _session_cookie({"user": _ADMIN_EMAIL, "user_id": admin["id"]}),
    )
    return client, db_path


def test_invitation_is_email_bound_and_creates_one_account(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    invitation, token = create_invitation(
        "invitee@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )

    stored = get_invitation(invitation.id, db_path)
    assert stored is not None
    assert stored.status == "pending"
    assert stored.intended_email == "invitee@example.test"

    with pytest.raises(InvalidInvitation):
        accept_invitation(token, email="other@example.test", db_path=db_path)
    assert get_user_by_email("other@example.test", db_path) is None

    first = accept_invitation(
        token,
        email="invitee@example.test",
        display_name="Invitee",
        db_path=db_path,
    )
    second = accept_invitation(
        token,
        email="invitee@example.test",
        display_name="Invitee",
        db_path=db_path,
    )

    assert first["id"] == second["id"]
    assert get_user_by_email("invitee@example.test", db_path)["id"] == first["id"]
    accepted = get_invitation(invitation.id, db_path)
    assert accepted is not None
    assert accepted.status == "accepted"


def test_invitation_only_stores_hashed_token(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _, token = create_invitation(
        "hash@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT token_hash FROM invitations").fetchall()

    assert rows
    assert all(token not in (row[0] or "") for row in rows)


def test_expired_and_revoked_invitations_cannot_be_accepted(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    expired, expired_token = create_invitation(
        "expired@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )
    past = (datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE invitations SET expires_at = ? WHERE id = ?",
            (past, expired.id),
        )

    assert get_invitation(expired.id, db_path).status == "expired"
    with pytest.raises(InvalidInvitation):
        accept_invitation(
            expired_token,
            email="expired@example.test",
            db_path=db_path,
        )

    revoked, revoked_token = create_invitation(
        "revoked@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )
    updated = revoke_invitation(
        revoked.id,
        actor_user_id="admin-user",
        reason="no longer needed",
        db_path=db_path,
    )
    assert updated.status == "revoked"
    assert updated.revoke_reason == "no longer needed"
    with pytest.raises(InvalidInvitation):
        accept_invitation(
            revoked_token,
            email="revoked@example.test",
            db_path=db_path,
        )


def test_accepted_invitation_cannot_be_revoked(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    invitation, token = create_invitation(
        "accepted@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )
    accept_invitation(token, email="accepted@example.test", db_path=db_path)

    with pytest.raises(InvitationConflict):
        revoke_invitation(
            invitation.id,
            actor_user_id="admin-user",
            db_path=db_path,
        )


def test_listing_filters_lifecycle_statuses_without_account_signals(
    tmp_path: Path,
) -> None:
    db_path = _db(tmp_path)
    pending, _ = create_invitation(
        "pending@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )
    expired, _ = create_invitation(
        "expired-list@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )
    revoked, _ = create_invitation(
        "revoked-list@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )
    accepted, accepted_token = create_invitation(
        "accepted-list@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )
    accept_invitation(
        accepted_token,
        email="accepted-list@example.test",
        db_path=db_path,
    )
    past = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE invitations SET expires_at = ? WHERE id = ?",
            (past, expired.id),
        )
    revoke_invitation(revoked.id, actor_user_id="admin-user", db_path=db_path)

    assert [item.id for item in list_invitations(status="pending", db_path=db_path)] == [
        pending.id
    ]
    assert [item.id for item in list_invitations(status="expired", db_path=db_path)] == [
        expired.id
    ]
    assert [item.id for item in list_invitations(status="revoked", db_path=db_path)] == [
        revoked.id
    ]
    assert [
        item.id for item in list_invitations(status="accepted", db_path=db_path)
    ] == [accepted.id]

    payload = list_invitations(status="accepted", db_path=db_path)[0].as_dict()
    assert "accepted_user_id" not in payload
    assert "account_exists" not in payload
    assert payload["intended_email"] == "accepted-list@example.test"


def test_rate_limit_rejects_excess_actions(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    assert consume_rate_limit(
        "create:admin", limit=2, window_seconds=60, db_path=db_path
    )
    assert consume_rate_limit(
        "create:admin", limit=2, window_seconds=60, db_path=db_path
    )
    assert not consume_rate_limit(
        "create:admin", limit=2, window_seconds=60, db_path=db_path
    )
    with pytest.raises(InvitationRateLimited):
        enforce_rate_limit("create:admin", limit=2, window_seconds=60, db_path=db_path)


def test_admin_can_create_list_and_revoke_invitations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db_path = _app_client(tmp_path, monkeypatch)

    created = client.post(
        "/api/admin/invitations",
        json={"email": "invitee@example.test", "ttl_hours": 24},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["invitation"]["status"] == "pending"
    assert body["invite_url"] == f"/invite/{body['token']}"
    assert body["token"] not in json.dumps(
        list_invitations(status="all", db_path=db_path)[0].as_dict()
    )

    listed = client.get("/api/admin/invitations?status=pending")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["invitations"]] == [
        body["invitation"]["id"]
    ]

    revoked = client.post(
        f"/api/admin/invitations/{body['invitation']['id']}/revoke",
        json={"reason": "duplicate"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["invitation"]["status"] == "revoked"
    assert client.get("/api/admin/invitations?status=pending").json()[
        "invitations"
    ] == []


def test_non_admin_cannot_view_or_create_invitations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db_path = _app_client(tmp_path, monkeypatch)
    member = get_or_create_user("member@example.test", "Member", db_path=db_path)
    client.cookies.set(
        "session",
        _session_cookie({"user": "member@example.test", "user_id": member["id"]}),
    )

    assert client.get("/api/admin/invitations").status_code == 403
    assert (
        client.post(
            "/api/admin/invitations",
            json={"email": "invitee@example.test"},
        ).status_code
        == 403
    )


def test_invite_link_redirects_to_google_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _app_client(tmp_path, monkeypatch)

    response = client.get("/invite/synthetic-token", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login/google"


def test_oauth_callback_accepts_invitation_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db_path = _app_client(tmp_path, monkeypatch)
    invitation, token = create_invitation(
        "oauth-invitee@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )

    import webapp.app as app_module

    class _FakeGoogle:
        async def authorize_access_token(self, request: Any) -> dict[str, Any]:
            return {
                "userinfo": {
                    "email": "oauth-invitee@example.test",
                    "email_verified": True,
                    "name": "OAuth Invitee",
                }
            }

    class _FakeOAuth:
        google = _FakeGoogle()

    monkeypatch.setattr(app_module, "oauth", _FakeOAuth())

    for _ in range(2):
        client.cookies.set(
            "session",
            _session_cookie({"invitation_token": token}),
        )
        response = client.get("/auth", follow_redirects=False)
        assert response.status_code in {302, 303, 307}
        assert response.headers["location"] == "/"

    assert get_user_by_email("oauth-invitee@example.test", db_path) is not None
    assert get_invitation(invitation.id, db_path).status == "accepted"
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE email = ?",
            ("oauth-invitee@example.test",),
        ).fetchone()[0]
    assert count == 1


def test_oauth_callback_rejects_invitation_for_unverified_email(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db_path = _app_client(tmp_path, monkeypatch)
    invitation, token = create_invitation(
        "unverified-invitee@example.test",
        created_by_user_id="admin-user",
        db_path=db_path,
    )

    import webapp.app as app_module

    for claim in ({"email_verified": False}, {}, {"email_verified": "true"}):

        class _FakeGoogle:
            async def authorize_access_token(
                self, request: Any, _claim: dict[str, Any] = claim
            ) -> dict[str, Any]:
                return {
                    "userinfo": {
                        "email": "unverified-invitee@example.test",
                        "name": "Unverified",
                        **_claim,
                    }
                }

        class _FakeOAuth:
            google = _FakeGoogle()

        monkeypatch.setattr(app_module, "oauth", _FakeOAuth())
        client.cookies.set(
            "session",
            _session_cookie({"invitation_token": token}),
        )
        response = client.get("/auth", follow_redirects=False)
        assert response.status_code == 403

    assert get_user_by_email("unverified-invitee@example.test", db_path) is None
    assert get_invitation(invitation.id, db_path).status == "pending"


def test_beta_invite_only_blocks_uninvited_new_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db_path = _app_client(tmp_path, monkeypatch)

    import webapp.app as app_module

    class _FakeGoogle:
        async def authorize_access_token(self, request: Any) -> dict[str, Any]:
            return {
                "userinfo": {
                    "email": "blocked@example.test",
                    "name": "Blocked",
                }
            }

    class _FakeOAuth:
        google = _FakeGoogle()

    monkeypatch.setattr(app_module, "oauth", _FakeOAuth())
    monkeypatch.setattr(app_module, "BETA_INVITE_ONLY", True)

    response = client.get("/auth", follow_redirects=False)
    assert response.status_code == 403
    assert get_user_by_email("blocked@example.test", db_path) is None

    existing = get_or_create_user("existing@example.test", "Existing", db_path=db_path)

    class _ExistingGoogle:
        async def authorize_access_token(self, request: Any) -> dict[str, Any]:
            return {
                "userinfo": {
                    "email": existing["email"],
                    "name": existing["display_name"],
                }
            }

    class _ExistingOAuth:
        google = _ExistingGoogle()

    monkeypatch.setattr(app_module, "oauth", _ExistingOAuth())
    client.cookies.set(
        "session",
        _session_cookie({"user": existing["email"], "user_id": existing["id"]}),
    )
    accepted = client.get("/auth", follow_redirects=False)
    assert accepted.status_code in {302, 303, 307}
    assert accepted.headers["location"] == "/"
