"""Account recovery, token safety and verified email changes.

All addresses and identities below are synthetic and use reserved example
domains. The tests exercise persistence, service semantics and the real FastAPI
route graph; SMTP is replaced by an in-memory delivery list.
"""

from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest
from account_recovery import (
    RECOVERY_TOKEN_TTL_SECONDS,
    EmailAlreadyInUse,
    EmailUnchanged,
    IdentityConflictError,
    InactiveAccountError,
    InvalidEmailAddress,
    RecoveryTokenAlreadyUsed,
    RecoveryTokenExpired,
    canonical_email,
    consume_action_rate_limit,
    consume_email_change_request,
    consume_recovery_token,
    create_email_change_request,
    is_recently_authenticated,
    request_recovery,
    resolve_oauth_account,
)
from database import get_or_create_user, get_user_by_email, get_user_by_id, init_db

_EMAIL = "person@example.com"
_NEW_EMAIL = "new-person@example.com"
_OTHER_EMAIL = "other@example.com"


def _db(tmp_path: Path) -> str:
    path = str(tmp_path / "recovery.db")
    init_db(path)
    return path


def _account(db: str, email: str = _EMAIL) -> str:
    row = get_user_by_email(email, db)
    if row is None:
        row = get_or_create_user(email, "Synthetic Person", db, email_verified=True)
    return row["id"]


def _token_from_body(body: str) -> str:
    marker = "token="
    start = body.index(marker) + len(marker)
    return body[start:].splitlines()[0].strip()


# ---------------------------------------------------------------------------
# Recovery tokens
# ---------------------------------------------------------------------------


def test_recovery_token_is_hashed_at_rest(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _account(db)

    token = request_recovery(_EMAIL, db)

    assert token
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT token_hash FROM account_recovery_tokens"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] != token
    assert len(rows[0][0]) == 64


def test_recovery_token_is_single_use_and_replay_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _account(db)
    token = request_recovery(_EMAIL, db)
    assert token is not None

    first = consume_recovery_token(token, db)
    with pytest.raises(RecoveryTokenAlreadyUsed):
        consume_recovery_token(token, db)

    assert first.email == _EMAIL


def test_recovery_token_expires(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _account(db)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = request_recovery(_EMAIL, db, now=now)
    assert token is not None

    with pytest.raises(RecoveryTokenExpired):
        consume_recovery_token(
            token,
            db,
            now=now + timedelta(seconds=RECOVERY_TOKEN_TTL_SECONDS + 1),
        )


def test_unknown_expired_or_invalid_addresses_do_not_recover(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert request_recovery("missing@example.com", db) is None
    assert request_recovery("not-an-email", db) is None
    assert request_recovery("legacy@local", db) is None


def test_suspended_account_cannot_request_or_consume_recovery(tmp_path: Path) -> None:
    db = _db(tmp_path)
    user_id = _account(db)
    token = request_recovery(_EMAIL, db)
    assert token is not None
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE users SET status = 'suspended' WHERE id = ?", (user_id,))

    assert request_recovery(_EMAIL, db) is None
    with pytest.raises(InactiveAccountError):
        consume_recovery_token(token, db)


def test_recovery_requests_are_rate_limited(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert consume_action_rate_limit(
        "recovery", _EMAIL, db, limit=2, window_seconds=60, now=now
    )
    assert consume_action_rate_limit(
        "recovery", _EMAIL, db, limit=2, window_seconds=60, now=now
    )
    assert not consume_action_rate_limit(
        "recovery", _EMAIL, db, limit=2, window_seconds=60, now=now
    )
    assert consume_action_rate_limit(
        "recovery",
        _EMAIL,
        db,
        limit=2,
        window_seconds=60,
        now=now + timedelta(seconds=61),
    )


def test_recent_authentication_window(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session = {"authenticated_at": int(now.timestamp())}
    assert is_recently_authenticated(session, now=now)
    assert not is_recently_authenticated(
        session, now=now + timedelta(minutes=11)
    )
    assert not is_recently_authenticated({})
    assert not is_recently_authenticated({"authenticated_at": "never"})


# ---------------------------------------------------------------------------
# Verified email changes
# ---------------------------------------------------------------------------


def test_email_change_verifies_new_address_once(tmp_path: Path) -> None:
    db = _db(tmp_path)
    user_id = _account(db)

    change = create_email_change_request(user_id, _NEW_EMAIL, db)
    result = consume_email_change_request(change.token, db)

    assert result.old_email == _EMAIL
    assert result.new_email == _NEW_EMAIL
    assert get_user_by_id(user_id, db)["email"] == _NEW_EMAIL  # type: ignore[index]
    with pytest.raises(RecoveryTokenAlreadyUsed):
        consume_email_change_request(change.token, db)


def test_email_change_rejects_invalid_same_and_taken_address(tmp_path: Path) -> None:
    db = _db(tmp_path)
    user_id = _account(db)
    _account(db, _OTHER_EMAIL)

    with pytest.raises(InvalidEmailAddress):
        create_email_change_request(user_id, "nope", db)
    with pytest.raises(EmailUnchanged):
        create_email_change_request(user_id, "PERSON@example.com", db)
    with pytest.raises(EmailAlreadyInUse):
        create_email_change_request(user_id, _OTHER_EMAIL, db)


def test_email_change_expires_and_cannot_complete_for_suspended_account(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    user_id = _account(db)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expired = create_email_change_request(user_id, _NEW_EMAIL, db, now=now)
    with pytest.raises(RecoveryTokenExpired):
        consume_email_change_request(
            expired.token, db, now=now + timedelta(minutes=31)
        )

    pending = create_email_change_request(user_id, _NEW_EMAIL, db, now=now)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE users SET status = 'suspended' WHERE id = ?", (user_id,))
    with pytest.raises(InactiveAccountError):
        consume_email_change_request(pending.token, db, now=now)


def test_oauth_identity_survives_an_address_change_and_blocks_takeover(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    first = resolve_oauth_account(
        provider="google",
        subject="synthetic-subject-1",
        email=_EMAIL,
        email_verified=True,
        db_path=db,
    )
    assert first.created is True

    change = create_email_change_request(first.user_id, _NEW_EMAIL, db)
    consume_email_change_request(change.token, db)

    repeats = resolve_oauth_account(
        provider="google",
        subject="synthetic-subject-1",
        email=_EMAIL,
        email_verified=True,
        db_path=db,
    )
    assert repeats.user_id == first.user_id
    assert repeats.email == _NEW_EMAIL

    with pytest.raises(IdentityConflictError):
        resolve_oauth_account(
            provider="google",
            subject="synthetic-subject-2",
            email=_NEW_EMAIL,
            email_verified=True,
            db_path=db,
        )


def test_oauth_adopts_a_pre_identity_account_and_marks_it_verified(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    existing = get_or_create_user(_EMAIL, "Synthetic Person", db)
    assert existing["email_verified"] is False

    admitted = resolve_oauth_account(
        provider="google",
        subject="synthetic-subject",
        email=_EMAIL,
        email_verified=True,
        db_path=db,
    )

    assert admitted.user_id == existing["id"]
    assert admitted.created is False
    assert get_user_by_id(existing["id"], db)["email_verified"] is True  # type: ignore[index]


def test_oauth_requires_a_verified_email(tmp_path: Path) -> None:
    db = _db(tmp_path)
    from account_recovery import IdentityVerificationError

    with pytest.raises(IdentityVerificationError):
        resolve_oauth_account(
            provider="google",
            subject="synthetic-subject",
            email=_EMAIL,
            email_verified=False,
            db_path=db,
        )
    assert get_user_by_email(_EMAIL, db) is None


def test_canonical_email_normalises_case_only(tmp_path: Path) -> None:
    assert canonical_email(" Person@Example.COM ") == _EMAIL
    assert canonical_email("person+tag@example.com") == "person+tag@example.com"
    assert canonical_email("person@localhost") is None
    assert canonical_email(None) is None


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------


class Web(NamedTuple):
    client: Any
    module: ModuleType
    db: str
    sent: list[Any]


@pytest.fixture()
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Web:
    repo_root = Path(__file__).resolve().parents[2]
    (repo_root / "frontend" / "dist" / "frontend" / "browser").mkdir(
        parents=True,
        exist_ok=True,
    )
    db = str(tmp_path / "recovery-web.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    monkeypatch.setenv("WEB_AUTH_SECRET", "account-recovery-test-secret-long-enough")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "recovery-test-client")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "recovery-test-secret")
    monkeypatch.setenv("WEB_PUBLIC_URL", "https://workout.example.com")
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    init_db(db)

    import webapp.app as app_module

    importlib.reload(app_module)
    sent: list[Any] = []
    monkeypatch.setattr(app_module, "_send_account_email", sent.append)
    from fastapi.testclient import TestClient

    return Web(
        TestClient(app_module.app, base_url="https://workout.example.com"),
        app_module,
        db,
        sent,
    )


def test_recovery_route_is_enumeration_safe_and_completes(web: Web) -> None:
    _account(web.db)

    unknown = web.client.post(
        "/api/account/recovery/request",
        json={"email": "missing@example.com"},
    )
    known = web.client.post(
        "/api/account/recovery/request",
        json={"email": _EMAIL},
    )

    assert unknown.status_code == 202
    assert known.status_code == 202
    assert unknown.json() == known.json() == {"status": "accepted"}
    assert len(web.sent) == 1
    token = _token_from_body(web.sent[0].body)

    replay = web.client.post(
        "/api/account/recovery/complete", json={"token": token}
    )
    second = web.client.post(
        "/api/account/recovery/complete", json={"token": token}
    )

    assert replay.status_code == 200
    assert replay.json()["user"]["email"] == _EMAIL
    assert second.status_code == 400
    assert web.client.get("/api/me").status_code == 200


def test_email_change_route_requires_recent_auth_and_alerts_old_address(
    web: Web,
) -> None:
    user_id = _account(web.db)
    token = request_recovery(_EMAIL, web.db)
    assert token is not None
    assert web.client.post(
        "/api/account/recovery/complete", json={"token": token}
    ).status_code == 200

    response = web.client.post(
        "/api/account/email/request", json={"email": _NEW_EMAIL}
    )

    assert response.status_code == 202
    assert [message.recipient for message in web.sent] == [_NEW_EMAIL, _EMAIL]
    verification = _token_from_body(
        next(message for message in web.sent if message.recipient == _NEW_EMAIL).body
    )

    confirmed = web.client.post(
        "/api/account/email/confirm", json={"token": verification}
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["email"] == _NEW_EMAIL
    assert get_user_by_id(user_id, web.db)["email"] == _NEW_EMAIL  # type: ignore[index]
    assert web.sent[-1].recipient == _EMAIL


def test_email_change_route_refuses_stale_authentication(web: Web) -> None:
    user_id = _account(web.db)
    token = request_recovery(_EMAIL, web.db)
    assert token is not None
    assert web.client.post(
        "/api/account/recovery/complete", json={"token": token}
    ).status_code == 200
    cookie_name = web.module.SESSION_SECURITY.cookie.name
    with web.client as client:
        cookie = client.cookies.get(cookie_name)
    assert cookie
    # Replace the session with the same identity but no recent-auth claim.
    web.client.cookies.clear()
    web.client.cookies.set(
        cookie_name, _signed_session(web.module, _EMAIL, user_id)
    )

    response = web.client.post(
        "/api/account/email/request", json={"email": _NEW_EMAIL}
    )
    assert response.status_code == 403
    assert web.sent == []


def test_suspended_route_cases_do_not_issue_a_session(web: Web) -> None:
    user_id = _account(web.db)
    token = request_recovery(_EMAIL, web.db)
    assert token is not None
    with sqlite3.connect(web.db) as conn:
        conn.execute("UPDATE users SET status = 'suspended' WHERE id = ?", (user_id,))

    requested = web.client.post(
        "/api/account/recovery/request", json={"email": _EMAIL}
    )
    completed = web.client.post(
        "/api/account/recovery/complete", json={"token": token}
    )

    assert requested.status_code == 202
    assert completed.status_code == 403
    assert web.client.get("/api/me").status_code == 401


def test_recovery_request_does_not_email_addresses_outside_the_allowlist(
    web: Web,
) -> None:
    _account(web.db)
    web.module.ALLOWED_EMAILS = [_OTHER_EMAIL]

    response = web.client.post(
        "/api/account/recovery/request", json={"email": _EMAIL}
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert web.sent == []


def test_recovery_completion_rechecks_the_allowlist(web: Web) -> None:
    _account(web.db)
    token = request_recovery(_EMAIL, web.db)
    assert token is not None
    web.module.ALLOWED_EMAILS = [_OTHER_EMAIL]

    response = web.client.post(
        "/api/account/recovery/complete", json={"token": token}
    )

    assert response.status_code == 400
    assert web.client.get("/api/me").status_code == 401


def test_recovery_still_works_for_an_allowlisted_address(web: Web) -> None:
    _account(web.db)
    web.module.ALLOWED_EMAILS = [_EMAIL]

    requested = web.client.post(
        "/api/account/recovery/request", json={"email": _EMAIL}
    )
    assert requested.status_code == 202
    assert len(web.sent) == 1
    token = _token_from_body(web.sent[0].body)

    completed = web.client.post(
        "/api/account/recovery/complete", json={"token": token}
    )
    assert completed.status_code == 200
    assert web.client.get("/api/me").status_code == 200


def _signed_session(module: ModuleType, email: str, user_id: str) -> str:
    import base64
    import json

    from itsdangerous import TimestampSigner

    session: dict[str, Any] = {}
    module.SESSION_LIFECYCLE.establish(session, user_id=user_id, email=email)
    payload = base64.b64encode(json.dumps(session).encode())
    return TimestampSigner(module.WEB_AUTH_SECRET).sign(payload).decode()
