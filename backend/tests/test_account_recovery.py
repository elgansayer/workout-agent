"""Single-use recovery and verified e-mail change (issue #781).

All addresses are synthetic examples. Delivery is captured by a fake sender so
the tests exercise the real token lifecycle, persistence and rate limits without
depending on a mail server or wall-clock timing.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import account_recovery
import pytest
from accounts import resolve_oauth_user, set_account_status
from database import get_or_create_user, init_db

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
EMAIL = "person@example.com"
NEW_EMAIL = "new-address@example.com"


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    path = str(tmp_path / "recovery.db")
    init_db(path)
    init_db(path)  # schema is idempotent
    return path


def _user(
    db: str,
    *,
    email: str = EMAIL,
    subject: str = "subject-1",
    status: str | None = None,
) -> str:
    row = resolve_oauth_user(
        provider="google",
        subject=subject,
        email=email,
        email_verified=True,
        display_name="Person",
        db_path=db,
    )
    if status is not None:
        set_account_status(row["id"], status, db_path=db)
    return str(row["id"])


class CapturedMail:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def __call__(self, to: str, subject: str, body: str) -> None:
        self.messages.append((to, subject, body))

    def token(self, index: int = 0) -> str:
        body = self.messages[index][2]
        match = re.search(r"https?://[^\s]+", body)
        assert match is not None
        values = parse_qs(urlsplit(match.group(0)).query)
        return values["token"][0]

    def clear(self) -> None:
        self.messages.clear()


def _issue_recovery(db: str, mail: CapturedMail, now: datetime = NOW) -> str:
    assert account_recovery.request_recovery(
        email=EMAIL,
        source="source-a",
        db_path=db,
        now=now,
        sender=mail,
        public_url="https://workout.example.test",
    )
    return mail.token()


def _token_rows(db: str) -> list[sqlite3.Row]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM account_action_tokens ORDER BY created_at, rowid"
        ).fetchall()


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def test_recovery_token_is_hashed_single_use_and_returns_the_active_account(
    db: str,
) -> None:
    user_id = _user(db)
    mail = CapturedMail()
    token = _issue_recovery(db, mail)

    rows = _token_rows(db)
    assert len(rows) == 1
    assert rows[0]["user_id"] == user_id
    assert rows[0]["purpose"] == "recovery"
    assert rows[0]["token_hash"] != token
    assert token not in {row["token_hash"] for row in rows}
    assert mail.messages[0][0] == EMAIL

    recovered = account_recovery.complete_recovery(token=token, db_path=db, now=NOW)
    assert recovered["id"] == user_id
    assert recovered["email"] == EMAIL

    with pytest.raises(account_recovery.InvalidTokenError):
        account_recovery.complete_recovery(token=token, db_path=db, now=NOW)


def test_recovery_token_expires_and_cannot_be_replayed(db: str) -> None:
    _user(db)
    mail = CapturedMail()
    token = _issue_recovery(db, mail)

    with pytest.raises(account_recovery.InvalidTokenError):
        account_recovery.complete_recovery(
            token=token,
            db_path=db,
            now=NOW + account_recovery.RECOVERY_TOKEN_TTL + timedelta(seconds=1),
        )
    assert _token_rows(db)[0]["used_at"] is None


def test_a_new_request_invalidates_the_previous_recovery_token(db: str) -> None:
    _user(db)
    mail = CapturedMail()
    first = _issue_recovery(db, mail)
    second = account_recovery.request_recovery(
        email=EMAIL,
        source="source-a",
        db_path=db,
        now=NOW + timedelta(minutes=1),
        sender=mail,
        public_url="https://workout.example.test",
    )
    assert second
    with pytest.raises(account_recovery.InvalidTokenError):
        account_recovery.complete_recovery(token=first, db_path=db, now=NOW)


def test_unknown_addresses_are_indistinguishable_and_issue_nothing(db: str) -> None:
    mail = CapturedMail()
    assert not account_recovery.request_recovery(
        email="missing@example.com",
        source="source-a",
        db_path=db,
        now=NOW,
        sender=mail,
    )
    assert mail.messages == []
    assert _token_rows(db) == []


@pytest.mark.parametrize("status", ["suspended", "deleted"])
def test_inactive_accounts_never_receive_or_consume_a_recovery_token(
    db: str, status: str
) -> None:
    user_id = _user(db, status="active")
    mail = CapturedMail()
    # Issue before suspension, then suspend before the link is used.
    token = _issue_recovery(db, mail)
    if status == "deleted":
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE users SET status = 'deleted' WHERE id = ?", (user_id,))
    else:
        set_account_status(user_id, status, db_path=db)

    with pytest.raises(account_recovery.InvalidTokenError):
        account_recovery.complete_recovery(token=token, db_path=db, now=NOW)
    assert _token_rows(db)[0]["used_at"] is not None
    mail.clear()
    assert not account_recovery.request_recovery(
        email=EMAIL,
        source="source-b",
        db_path=db,
        now=NOW,
        sender=mail,
    )
    assert mail.messages == []
    if status == "suspended":
        set_account_status(user_id, "active", db_path=db)
        with pytest.raises(account_recovery.InvalidTokenError):
            account_recovery.complete_recovery(token=token, db_path=db, now=NOW)


def test_recovery_request_is_rate_limited_per_address(db: str) -> None:
    _user(db)
    mail = CapturedMail()
    for index in range(account_recovery._MAX_REQUESTS_PER_SUBJECT):
        assert account_recovery.request_recovery(
            email=EMAIL,
            source=f"source-{index}",
            db_path=db,
            now=NOW + timedelta(seconds=index),
            sender=mail,
            public_url="https://workout.example.test",
        )
    with pytest.raises(account_recovery.AccountActionRateLimited):
        account_recovery.request_recovery(
            email=EMAIL,
            source="source-other",
            db_path=db,
            now=NOW + timedelta(seconds=10),
            sender=mail,
        )


def test_recovery_request_is_rate_limited_per_source(db: str) -> None:
    mail = CapturedMail()
    for index in range(account_recovery._MAX_REQUESTS_PER_SOURCE):
        account_recovery.request_recovery(
            email=f"unknown-{index}@example.com",
            source="one-source",
            db_path=db,
            now=NOW + timedelta(seconds=index),
            sender=mail,
        )
    with pytest.raises(account_recovery.AccountActionRateLimited):
        account_recovery.request_recovery(
            email="another@example.com",
            source="one-source",
            db_path=db,
            now=NOW + timedelta(seconds=20),
            sender=mail,
        )


def test_a_token_for_a_disallowed_address_is_never_sent(db: str) -> None:
    _user(db)
    mail = CapturedMail()
    assert not account_recovery.request_recovery(
        email=EMAIL,
        source="source-a",
        db_path=db,
        now=NOW,
        sender=mail,
        allowed=False,
    )
    assert mail.messages == []
    assert _token_rows(db) == []


def test_delivery_failure_discards_the_token(db: str) -> None:
    _user(db)

    def failing_sender(_to: str, _subject: str, _body: str) -> None:
        raise OSError("synthetic delivery failure")

    with pytest.raises(account_recovery.EmailDeliveryFailed):
        account_recovery.request_recovery(
            email=EMAIL,
            source="source-a",
            db_path=db,
            now=NOW,
            sender=failing_sender,
        )
    assert _token_rows(db) == []


# ---------------------------------------------------------------------------
# E-mail change
# ---------------------------------------------------------------------------


def _request_change(
    db: str,
    user_id: str,
    mail: CapturedMail,
    *,
    new_email: str = NEW_EMAIL,
    authenticated_at: datetime | None = None,
    now: datetime = NOW,
    allowed: bool = True,
) -> None:
    account_recovery.request_email_change(
        user_id=user_id,
        new_email=new_email,
        authenticated_at=authenticated_at or now - timedelta(minutes=1),
        db_path=db,
        now=now,
        sender=mail,
        public_url="https://workout.example.test",
        new_email_allowed=allowed,
    )


def _email(db: str, user_id: str) -> str:
    with sqlite3.connect(db) as conn:
        return str(
            conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        )


def _session_epoch(db: str, user_id: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(
            conn.execute(
                "SELECT session_epoch FROM users WHERE id = ?", (user_id,)
            ).fetchone()[0]
        )


def test_email_change_requires_recent_authentication(db: str) -> None:
    user_id = _user(db)
    mail = CapturedMail()
    with pytest.raises(account_recovery.RecentAuthenticationRequired):
        _request_change(
            db,
            user_id,
            mail,
            authenticated_at=NOW
            - account_recovery.RECENT_AUTHENTICATION_WINDOW
            - timedelta(seconds=1),
        )
    assert mail.messages == []


def test_email_change_verifies_the_new_address_and_alerts_both_addresses(
    db: str,
) -> None:
    user_id = _user(db)
    mail = CapturedMail()
    _request_change(db, user_id, mail)

    assert [(to, subject) for to, subject, _ in mail.messages] == [
        (NEW_EMAIL, "Confirm your new Workout Agent e-mail address"),
        (EMAIL, "Workout Agent e-mail change requested"),
    ]
    assert _email(db, user_id) == EMAIL  # not changed before verification
    token = mail.token(0)

    result = account_recovery.confirm_email_change(
        token=token,
        db_path=db,
        now=NOW + timedelta(minutes=1),
        sender=mail,
    )
    assert result.previous_email == EMAIL
    assert result.user["email"] == NEW_EMAIL
    assert _email(db, user_id) == NEW_EMAIL
    assert _session_epoch(db, user_id) == 1
    assert mail.messages[-1][0] == EMAIL
    assert "was changed" in mail.messages[-1][1]

    with pytest.raises(account_recovery.InvalidTokenError):
        account_recovery.confirm_email_change(
            token=token,
            db_path=db,
            now=NOW + timedelta(minutes=2),
            sender=mail,
        )


def test_email_change_token_expires(db: str) -> None:
    user_id = _user(db)
    mail = CapturedMail()
    _request_change(db, user_id, mail)
    with pytest.raises(account_recovery.InvalidTokenError):
        account_recovery.confirm_email_change(
            token=mail.token(),
            db_path=db,
            now=NOW + account_recovery.EMAIL_CHANGE_TOKEN_TTL + timedelta(seconds=1),
            sender=mail,
        )
    assert _email(db, user_id) == EMAIL


def test_email_change_rejects_the_current_address(db: str) -> None:
    user_id = _user(db)
    mail = CapturedMail()
    with pytest.raises(account_recovery.EmailChangeRejected):
        _request_change(db, user_id, mail, new_email=EMAIL)
    assert mail.messages == []


def test_email_change_rejects_an_address_owned_by_another_account(db: str) -> None:
    user_id = _user(db)
    get_or_create_user(NEW_EMAIL, "Other", db)
    mail = CapturedMail()
    with pytest.raises(account_recovery.EmailChangeRejected):
        _request_change(db, user_id, mail)
    assert mail.messages == []


def test_email_change_rejects_a_disallowed_address(db: str) -> None:
    user_id = _user(db)
    mail = CapturedMail()
    with pytest.raises(account_recovery.EmailChangeRejected):
        _request_change(db, user_id, mail, allowed=False)
    assert mail.messages == []


def test_email_change_does_nothing_for_a_suspended_account(db: str) -> None:
    user_id = _user(db, status="suspended")
    mail = CapturedMail()
    with pytest.raises(account_recovery.EmailChangeRejected):
        _request_change(db, user_id, mail)
    assert mail.messages == []


def test_email_change_token_is_refused_after_the_account_is_suspended(db: str) -> None:
    user_id = _user(db)
    mail = CapturedMail()
    _request_change(db, user_id, mail)
    token = mail.token()
    set_account_status(user_id, "suspended", db_path=db)

    with pytest.raises(account_recovery.InvalidTokenError):
        account_recovery.confirm_email_change(
            token=token, db_path=db, now=NOW + timedelta(minutes=1), sender=mail
        )
    assert _email(db, user_id) == EMAIL
    set_account_status(user_id, "active", db_path=db)
    with pytest.raises(account_recovery.InvalidTokenError):
        account_recovery.confirm_email_change(
            token=token, db_path=db, now=NOW + timedelta(minutes=2), sender=mail
        )
    assert _email(db, user_id) == EMAIL


def test_email_change_requests_are_rate_limited(db: str) -> None:
    user_id = _user(db)
    mail = CapturedMail()
    for index in range(account_recovery._MAX_REQUESTS_PER_SUBJECT):
        with pytest.raises(account_recovery.EmailChangeRejected):
            # Reusing the current address still records the request before the
            # address check, which is what the rate limit is measuring.
            _request_change(
                db,
                user_id,
                mail,
                new_email=EMAIL,
                now=NOW + timedelta(seconds=index),
            )
    with pytest.raises(account_recovery.AccountActionRateLimited):
        _request_change(db, user_id, mail, now=NOW + timedelta(seconds=10))
