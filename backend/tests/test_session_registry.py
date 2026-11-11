"""Deterministic tests for the tenant-scoped session registry."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from database import get_or_create_user, init_db
from session_registry import (
    SESSION_MAX_AGE_SECONDS,
    approximate_location,
    create_session,
    current_session_row_id,
    describe_device,
    list_user_sessions,
    revoke_current_session,
    revoke_other_sessions,
    revoke_session,
    rotate_session,
    validate_session,
)


def _db(tmp_path: Any) -> str:
    return str(tmp_path / "sessions.db")


def _user(db: str, email: str) -> str:
    return str(get_or_create_user(email, db_path=db)["id"])


def test_create_and_list_sessions_never_returns_credentials(tmp_path: Any) -> None:
    db = _db(tmp_path)
    init_db(db)
    user = _user(db, "synthetic@example.test")

    created = create_session(
        user,
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        client_ip="203.0.113.45",
        country_code="GB",
        db_path=db,
    )

    sessions = list_user_sessions(user, created["sid"], db)

    assert len(sessions) == 1
    session = sessions[0]
    assert session["current"] is True
    assert session["device"] == "Chrome on Linux"
    assert "GB" in session["location"]
    assert "203.0.113.45" not in session["location"]
    assert session["created_at"]
    assert session["last_used_at"]
    assert "sid" not in session
    assert "session_hash" not in session


def test_list_and_revoke_cannot_cross_accounts(tmp_path: Any) -> None:
    db = _db(tmp_path)
    init_db(db)
    owner = _user(db, "owner@example.test")
    other = _user(db, "other@example.test")

    owner_session = create_session(owner, db_path=db)
    other_session = create_session(other, db_path=db)

    owner_ids = {item["id"] for item in list_user_sessions(owner, owner_session["sid"], db)}
    other_ids = {item["id"] for item in list_user_sessions(other, other_session["sid"], db)}
    assert owner_session["id"] in owner_ids
    assert other_session["id"] not in owner_ids
    assert owner_session["id"] not in other_ids

    assert revoke_session(owner, other_session["id"], db) is False
    assert validate_session(other, other_session["sid"], db) is True

    assert revoke_session(owner, owner_session["id"], db) is True
    assert validate_session(owner, owner_session["sid"], db) is False


def test_revoke_other_sessions_preserves_current_and_rotates(tmp_path: Any) -> None:
    db = _db(tmp_path)
    init_db(db)
    user = _user(db, "rotate@example.test")

    current = create_session(user, user_agent="Firefox/120", db_path=db)
    second = create_session(user, user_agent="Safari/17", db_path=db)
    third = create_session(user, user_agent="Edg/120", db_path=db)

    revoked = revoke_other_sessions(user, current["sid"], db)
    assert revoked == 2
    assert validate_session(user, current["sid"], db) is True
    assert validate_session(user, second["sid"], db) is False
    assert validate_session(user, third["sid"], db) is False

    rotated = rotate_session(user, current["sid"], db_path=db)
    assert rotated is not None
    assert rotated["sid"] != current["sid"]
    assert validate_session(user, current["sid"], db) is False
    assert validate_session(user, rotated["sid"], db) is True
    assert current_session_row_id(user, rotated["sid"], db) == rotated["id"]


def test_logout_revokes_only_the_current_session(tmp_path: Any) -> None:
    db = _db(tmp_path)
    init_db(db)
    user = _user(db, "logout@example.test")
    current = create_session(user, db_path=db)
    other = create_session(user, db_path=db)

    assert revoke_current_session(user, current["sid"], db) is True
    assert validate_session(user, current["sid"], db) is False
    assert validate_session(user, other["sid"], db) is True


def test_expired_sessions_are_rejected_and_hidden(tmp_path: Any) -> None:
    db = _db(tmp_path)
    init_db(db)
    user = _user(db, "expired@example.test")
    old = datetime.now(tz=timezone.utc) - timedelta(
        seconds=SESSION_MAX_AGE_SECONDS + 60
    )

    expired = create_session(user, db_path=db, now=old)
    fresh = create_session(user, db_path=db)

    assert validate_session(user, expired["sid"], db) is False
    listed = list_user_sessions(user, fresh["sid"], db)
    assert [item["id"] for item in listed] == [fresh["id"]]


def test_expired_current_session_cannot_be_rotated(tmp_path: Any) -> None:
    db = _db(tmp_path)
    init_db(db)
    user = _user(db, "expired-rotate@example.test")
    old = datetime.now(tz=timezone.utc) - timedelta(
        seconds=SESSION_MAX_AGE_SECONDS + 60
    )
    expired = create_session(user, db_path=db, now=old)

    assert rotate_session(user, expired["sid"], db_path=db) is None


def test_device_and_location_labels_are_bounded_and_coarse() -> None:
    assert describe_device("") == "Unknown device"
    assert describe_device("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1") == (
        "Safari on iOS"
    )
    assert "203.0.113.45" not in approximate_location("203.0.113.45")
    assert "203.0.113.0/24" in approximate_location("203.0.113.45")
    assert approximate_location("127.0.0.1") == "Local network"
    assert approximate_location("not-an-ip") == "Unknown location"
    assert len(describe_device("Chrome " + "x" * 1000)) <= 160


def test_new_table_is_tenant_indexed(tmp_path: Any) -> None:
    db = _db(tmp_path)
    init_db(db)

    with sqlite3.connect(db, timeout=10) as conn:
        foreign_keys = conn.execute(
            'SELECT "table", "from" FROM pragma_foreign_key_list(?)',
            ("user_sessions",),
        ).fetchall()
        assert ("users", "user_id") in foreign_keys

        index_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'user_sessions'",
            ).fetchall()
        }
        assert {"idx_user_sessions_user", "idx_user_sessions_user_active"} <= index_names


def test_legacy_adoption_is_blocked_only_after_own_revocation(tmp_path: Any) -> None:
    from session_registry import (
        create_session,
        legacy_session_adoptable,
        revoke_current_session,
    )

    db_path = str(tmp_path / "legacy.db")
    init_db(db_path)
    first = str(get_or_create_user("a@example.test", db_path=db_path)["id"])
    second = str(get_or_create_user("b@example.test", db_path=db_path)["id"])

    assert legacy_session_adoptable(first, db_path) is True
    record = create_session(first, db_path=db_path)
    assert legacy_session_adoptable(first, db_path) is True
    assert revoke_current_session(first, record["sid"], db_path) is True
    assert legacy_session_adoptable(first, db_path) is False
    assert legacy_session_adoptable(second, db_path) is True
    assert legacy_session_adoptable("", db_path) is False
