"""Account sign-in resolution, lifecycle gating and operator status changes."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from accounts import (
    AccountError,
    AccountInactiveError,
    AccountNotFoundError,
    AccountStatus,
    IdentityConflictError,
    IdentityVerificationError,
    has_verified_identity,
    require_active_user,
    resolve_oauth_user,
    set_account_status,
)
from database import get_all_users, get_or_create_user, init_db, transaction
from tenancy import LEGACY_TENANT_EMAIL

_EMAIL = "person@example.com"


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A production-policy database: no placeholder, no implicit tenant."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    path = str(tmp_path / "accounts.db")
    init_db(path)
    return path


def _sign_in(
    db: str,
    *,
    subject: str = "subject-1",
    email: str = _EMAIL,
    verified: object = True,
    provider: str = "google",
    name: str | None = "Person",
) -> dict[str, object]:
    return dict(
        resolve_oauth_user(
            provider=provider,
            subject=subject,
            email=email,
            email_verified=verified,  # type: ignore[arg-type]
            display_name=name,
            db_path=db,
        )
    )


def _count(db: str, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _force_status(db: str, user_id: str, status: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))


# ---------------------------------------------------------------------------
# resolve_oauth_user
# ---------------------------------------------------------------------------


def test_first_verified_sign_in_creates_an_active_account_and_identity(db: str) -> None:
    user = _sign_in(db, email="  Person@Example.COM ")

    assert user["status"] == "active"
    assert user["email"] == _EMAIL
    assert user["display_name"] == "Person"
    assert user["last_login_at"] is not None
    assert user["created_at"] <= user["last_login_at"]  # type: ignore[operator]
    with sqlite3.connect(db) as conn:
        identity = conn.execute(
            "SELECT user_id, provider, subject, email, email_verified "
            "FROM user_identities"
        ).fetchall()
    assert identity == [(user["id"], "google", "subject-1", _EMAIL, 1)]


def test_repeat_sign_in_returns_the_same_account_and_records_the_login(db: str) -> None:
    first = _sign_in(db)
    second = _sign_in(db)

    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["last_login_at"] >= first["last_login_at"]  # type: ignore[operator]
    assert _count(db, "users") == 1
    assert _count(db, "user_identities") == 1


def test_the_stable_subject_selects_the_account_not_the_email(db: str) -> None:
    first = _sign_in(db, email="old@example.com")
    renamed = _sign_in(db, email="new@example.com")

    assert renamed["id"] == first["id"]
    assert renamed["email"] == "old@example.com"  # the account email is not rewritten
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT email FROM user_identities").fetchall() == [
            ("new@example.com",)
        ]
    assert _count(db, "users") == 1


def test_a_verified_email_adopts_a_pre_identity_account_once(db: str) -> None:
    pre_existing = get_or_create_user("Owner@Example.com", "Owner", db)
    with transaction(db) as conn:
        assert not has_verified_identity(conn, pre_existing["id"])

    adopted = _sign_in(db, subject="owner-subject", email="owner@example.com")

    assert adopted["id"] == pre_existing["id"]
    assert _count(db, "users") == 1
    with transaction(db) as conn:
        assert has_verified_identity(conn, pre_existing["id"])
    # The identity is now canonical: the same subject always finds the account.
    assert _sign_in(db, subject="owner-subject", email="other@example.com")["id"] == (
        pre_existing["id"]
    )


def test_provider_names_are_canonicalised(db: str) -> None:
    first = _sign_in(db, provider=" Google ")
    second = _sign_in(db, provider="google")
    assert first["id"] == second["id"]
    assert _count(db, "user_identities") == 1


@pytest.mark.parametrize(
    "claims",
    [
        {"verified": False},
        {"verified": "true"},  # only a real boolean is a verification
        {"verified": 1},
        {"verified": None},
        {"subject": ""},
        {"subject": "   "},
        {"email": ""},
        {"email": "not-an-address"},
        {"email": LEGACY_TENANT_EMAIL},
        {"email": " Legacy@Local "},
    ],
)
def test_unverifiable_claims_are_refused_and_create_nothing(
    db: str, claims: dict[str, object]
) -> None:
    with pytest.raises(IdentityVerificationError):
        _sign_in(db, **claims)  # type: ignore[arg-type]
    assert _count(db, "users") == 0
    assert _count(db, "user_identities") == 0


def test_a_reassigned_email_cannot_take_over_a_bound_account(db: str) -> None:
    owner = _sign_in(db, subject="original-subject")

    with pytest.raises(IdentityConflictError):
        _sign_in(db, subject="different-subject")  # same verified email, new subject

    assert _count(db, "users") == 1
    assert _count(db, "user_identities") == 1
    assert _sign_in(db, subject="original-subject")["id"] == owner["id"]


def test_ambiguous_email_matches_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    path = str(tmp_path / "ambiguous.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, "
            "display_name TEXT, created_at TEXT NOT NULL, "
            "timezone TEXT DEFAULT 'UTC', units TEXT DEFAULT 'metric')"
        )
        for uid, email in (("a", "Dup@Example.com"), ("b", "dup@example.com")):
            conn.execute(
                "INSERT INTO users (id, email, created_at) VALUES (?, ?, 'x')",
                (uid, email),
            )
    init_db(path)  # starts, but without the case-insensitive unique index

    with pytest.raises(IdentityConflictError):
        _sign_in(path, email="DUP@example.com")
    assert _count(path, "user_identities") == 0


@pytest.mark.parametrize("status", ["suspended", "retired", "deleted"])
def test_an_inactive_account_cannot_sign_in(db: str, status: str) -> None:
    user = _sign_in(db)
    _force_status(db, str(user["id"]), status)

    with pytest.raises(AccountInactiveError) as caught:
        _sign_in(db)
    assert caught.value.status == status


@pytest.mark.parametrize("status", ["suspended", "retired", "deleted"])
def test_an_inactive_account_cannot_be_adopted_by_a_new_identity(
    db: str, status: str
) -> None:
    pre_existing = get_or_create_user("dormant@example.com", None, db)
    _force_status(db, pre_existing["id"], status)

    with pytest.raises(AccountInactiveError):
        _sign_in(db, subject="dormant-subject", email="dormant@example.com")

    # The refusal rolled back completely: no identity was bound.
    assert _count(db, "user_identities") == 0
    assert _count(db, "users") == 1


def test_concurrent_first_sign_ins_create_exactly_one_account(db: str) -> None:
    barrier = threading.Barrier(6)
    ids: list[object] = []
    errors: list[BaseException] = []

    def worker() -> None:
        barrier.wait()
        try:
            ids.append(_sign_in(db, subject="raced-subject")["id"])
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(set(ids)) == 1
    assert _count(db, "users") == 1
    assert _count(db, "user_identities") == 1


def test_refusals_never_echo_the_email_or_the_subject(db: str) -> None:
    secret_subject = "subject-that-must-not-leak"
    secret_email = "private.person@example.com"
    messages: list[str] = []

    for kwargs in (
        {"verified": False},
        {"subject": " "},
        {"email": LEGACY_TENANT_EMAIL},
    ):
        with pytest.raises(AccountError) as caught:
            _sign_in(db, **{"subject": secret_subject, "email": secret_email, **kwargs})  # type: ignore[arg-type]
        messages.append(str(caught.value))

    user = _sign_in(db, subject=secret_subject, email=secret_email)
    with pytest.raises(IdentityConflictError) as conflict:
        _sign_in(db, subject="another-subject", email=secret_email)
    messages.append(str(conflict.value))
    _force_status(db, str(user["id"]), "suspended")
    with pytest.raises(AccountInactiveError) as inactive:
        _sign_in(db, subject=secret_subject, email=secret_email)
    messages.append(str(inactive.value))

    for message in messages:
        assert secret_subject not in message
        assert secret_email not in message


# ---------------------------------------------------------------------------
# require_active_user / has_verified_identity
# ---------------------------------------------------------------------------


def test_require_active_user_returns_only_active_accounts(db: str) -> None:
    user = get_or_create_user("active@example.com", None, db)
    assert require_active_user(user["id"], db)["id"] == user["id"]

    _force_status(db, user["id"], "suspended")
    with pytest.raises(AccountInactiveError) as caught:
        require_active_user(user["id"], db)
    assert caught.value.status == "suspended"


@pytest.mark.parametrize("user_id", ["", "   ", "no-such-account"])
def test_require_active_user_refuses_unknown_or_blank_ids(db: str, user_id: str) -> None:
    with pytest.raises(AccountNotFoundError):
        require_active_user(user_id, db)


def test_require_active_user_refuses_non_string_ids(db: str) -> None:
    with pytest.raises(AccountNotFoundError):
        require_active_user(None, db)  # type: ignore[arg-type]
    with pytest.raises(AccountNotFoundError):
        require_active_user(7, db)  # type: ignore[arg-type]


def test_only_a_provider_verified_identity_counts_as_verified(db: str) -> None:
    user = get_or_create_user("claimant@example.com", None, db)
    with transaction(db) as conn:
        assert not has_verified_identity(conn, user["id"])
        conn.execute(
            "INSERT INTO user_identities "
            "(user_id, provider, subject, email, email_verified, created_at) "
            "VALUES (?, 'google', 'unverified', 'claimant@example.com', 0, 'now')",
            (user["id"],),
        )
        assert not has_verified_identity(conn, user["id"])
        conn.execute("UPDATE user_identities SET email_verified = 1")
        assert has_verified_identity(conn, user["id"])


# ---------------------------------------------------------------------------
# set_account_status
# ---------------------------------------------------------------------------


def test_an_operator_can_suspend_and_reactivate_an_account(db: str) -> None:
    user = _sign_in(db)
    user_id = str(user["id"])

    suspended = set_account_status(user_id, "suspended", db)
    assert suspended["status"] == "suspended"
    assert suspended["status_changed_at"] >= user["created_at"]  # type: ignore[operator]
    assert suspended["created_at"] == user["created_at"]
    assert get_all_users(db) == []  # not schedulable while suspended
    with pytest.raises(AccountInactiveError):
        require_active_user(user_id, db)

    active = set_account_status(user_id, AccountStatus.ACTIVE, db)
    assert active["status"] == "active"
    assert [u["id"] for u in get_all_users(db)] == [user_id]


def test_restating_the_current_status_changes_nothing(db: str) -> None:
    user = _sign_in(db)
    again = set_account_status(str(user["id"]), "active", db)
    assert dict(again) == user  # the same row: no new timestamps either


@pytest.mark.parametrize("status", ["unclaimed", "retired", "deleted", "banished", ""])
def test_system_owned_or_unknown_statuses_cannot_be_set_directly(
    db: str, status: str
) -> None:
    user = _sign_in(db)
    with pytest.raises(AccountError):
        set_account_status(str(user["id"]), status, db)
    assert require_active_user(str(user["id"]), db)["status"] == "active"


def test_an_unknown_account_cannot_be_updated(db: str) -> None:
    with pytest.raises(AccountNotFoundError):
        set_account_status("no-such-account", "suspended", db)


def test_a_terminal_account_stays_terminal(db: str) -> None:
    user = _sign_in(db)
    _force_status(db, str(user["id"]), "deleted")
    with pytest.raises(AccountError, match="cannot change status"):
        set_account_status(str(user["id"]), "active", db)


def test_the_legacy_placeholder_cannot_be_managed_by_an_operator(
    tmp_path: Path,
) -> None:
    # Local mode: the placeholder is an ordinary-looking active account.
    path = str(tmp_path / "local.db")
    init_db(path)
    placeholder = next(
        u for u in get_all_users(path, include_inactive=True)
        if u["email"] == LEGACY_TENANT_EMAIL
    )
    with pytest.raises(AccountError, match="ownership"):
        set_account_status(placeholder["id"], "suspended", path)
    assert require_active_user(placeholder["id"], path)["status"] == "active"
