"""A push endpoint is one browser profile, so it belongs to one account at a time.

Two people can use the same browser. If both accounts kept a registration for the
same endpoint, a notification written for the account that left the device would
still be delivered to whoever signs in there next. Registering an endpoint hands
the device to the registering account, and the database refuses a second owner.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from database import (
    delete_push_subscription,
    get_or_create_user,
    get_push_subscriptions,
    init_db,
    save_push_subscription,
)

DEVICE = "https://push.example.test/shared-browser-profile"
PHONE = "https://push.example.test/alice-phone"


@pytest.fixture()
def accounts(tmp_path: Path) -> tuple[str, str, str]:
    db = str(tmp_path / "push.db")
    init_db(db)
    alice = str(get_or_create_user("alice@example.test", "Alice", db_path=db)["id"])
    bob = str(get_or_create_user("bob@example.test", "Bob", db_path=db)["id"])
    return db, alice, bob


def _endpoints(db: str, user_id: str) -> list[str]:
    return sorted(sub["endpoint"] for sub in get_push_subscriptions(user_id, db))


def test_registering_a_shared_device_moves_it_to_the_new_account(
    accounts: tuple[str, str, str],
) -> None:
    db, alice, bob = accounts
    save_push_subscription(alice, DEVICE, "p256dh-alice", "auth-alice", db)

    save_push_subscription(bob, DEVICE, "p256dh-bob", "auth-bob", db)

    assert _endpoints(db, alice) == []
    assert get_push_subscriptions(bob, db) == [
        {"endpoint": DEVICE, "keys": {"p256dh": "p256dh-bob", "auth": "auth-bob"}}
    ]


def test_taking_one_device_leaves_the_other_accounts_other_devices_alone(
    accounts: tuple[str, str, str],
) -> None:
    db, alice, bob = accounts
    save_push_subscription(alice, DEVICE, "p", "a", db)
    save_push_subscription(alice, PHONE, "p", "a", db)

    save_push_subscription(bob, DEVICE, "p", "a", db)

    assert _endpoints(db, alice) == [PHONE]
    assert _endpoints(db, bob) == [DEVICE]


def test_the_device_can_be_handed_back_and_never_ends_up_with_two_owners(
    accounts: tuple[str, str, str],
) -> None:
    db, alice, bob = accounts
    for owner in (alice, bob, alice, alice):
        save_push_subscription(owner, DEVICE, "p", "a", db)

    assert _endpoints(db, alice) == [DEVICE]
    assert _endpoints(db, bob) == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM push_subscriptions").fetchone() == (
            1,
        )


def test_deleting_a_registration_only_removes_the_callers_own_row(
    accounts: tuple[str, str, str],
) -> None:
    db, alice, bob = accounts
    save_push_subscription(alice, PHONE, "p", "a", db)

    delete_push_subscription(bob, PHONE, db)

    assert _endpoints(db, alice) == [PHONE]


def test_the_database_itself_refuses_a_second_owner_for_an_endpoint(
    accounts: tuple[str, str, str],
) -> None:
    db, alice, bob = accounts
    save_push_subscription(alice, DEVICE, "p", "a", db)

    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO push_subscriptions "
            "(user_id, endpoint, p256dh, auth, created_at) VALUES (?, ?, 'p', 'a', 'now')",
            (bob, DEVICE),
        )


def test_upgrade_keeps_only_the_newest_registration_of_a_repeated_endpoint(
    accounts: tuple[str, str, str],
) -> None:
    """Databases written before the rule can repeat an endpoint across accounts."""
    db, alice, bob = accounts
    save_push_subscription(alice, PHONE, "p", "a", db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP INDEX idx_push_subscriptions_endpoint")
        for owner in (alice, bob):  # Alice registered first, Bob most recently
            conn.execute(
                "INSERT INTO push_subscriptions "
                "(user_id, endpoint, p256dh, auth, created_at) "
                "VALUES (?, ?, 'p', 'a', 'now')",
                (owner, DEVICE),
            )

    init_db(db)
    init_db(db)  # idempotent

    assert _endpoints(db, alice) == [PHONE]
    assert _endpoints(db, bob) == [DEVICE]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_push_subscriptions_endpoint'"
        ).fetchone()
