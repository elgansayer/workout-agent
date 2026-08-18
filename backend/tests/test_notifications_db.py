"""Tests for notifications database functions."""

from __future__ import annotations

import pytest
from database import (
    clear_all_notifications,
    delete_notification,
    get_notifications,
    get_or_create_user,
    get_unread_notification_count,
    init_db,
    mark_all_notifications_read,
    mark_notification_read,
    save_notification,
)


@pytest.fixture
def db_with_user(tmp_path):
    db_file = str(tmp_path / "test_notifications.db")
    init_db(db_file)
    u1 = get_or_create_user("user1@example.com", "User One", db_path=db_file)
    u2 = get_or_create_user("user2@example.com", "User Two", db_path=db_file)
    return db_file, u1["id"], u2["id"]


def test_save_and_get_notifications(db_with_user: tuple[str, str, str]) -> None:
    db, u1, u2 = db_with_user
    nid1 = save_notification(
        u1,
        title="✨ Coach Status Update",
        message="Recovery looks great. Push deadlifts today.",
        type="coach",
        link="/dashboard",
        db_path=db,
    )
    nid2 = save_notification(
        u1,
        title="Hevy History Synced",
        message="Successfully rebuilt 25 workouts.",
        type="sync",
        link="/history",
        db_path=db,
    )
    # u2 notification
    save_notification(
        u2,
        title="User 2 Notification",
        message="Should not be visible to user 1",
        type="system",
        db_path=db,
    )

    notifs = get_notifications(u1, db_path=db)
    assert len(notifs) == 2
    assert notifs[0]["id"] == nid2  # ordered newest first
    assert notifs[0]["title"] == "Hevy History Synced"
    assert notifs[0]["type"] == "sync"
    assert notifs[0]["is_read"] is False
    assert notifs[1]["id"] == nid1
    assert notifs[1]["type"] == "coach"

    assert get_unread_notification_count(u1, db_path=db) == 2
    assert get_unread_notification_count(u2, db_path=db) == 1


def test_mark_notification_read(db_with_user: tuple[str, str, str]) -> None:
    db, u1, u2 = db_with_user
    nid = save_notification(u1, "Title", "Message", db_path=db)
    assert get_unread_notification_count(u1, db_path=db) == 1

    # Cross-user should fail
    assert not mark_notification_read(nid, u2, db_path=db)
    assert get_unread_notification_count(u1, db_path=db) == 1

    # Own user marks read
    assert mark_notification_read(nid, u1, db_path=db)
    assert get_unread_notification_count(u1, db_path=db) == 0

    unread = get_notifications(u1, unread_only=True, db_path=db)
    assert len(unread) == 0


def test_mark_all_read_and_delete(db_with_user: tuple[str, str, str]) -> None:
    db, u1, u2 = db_with_user
    nid1 = save_notification(u1, "N1", "M1", db_path=db)
    nid2 = save_notification(u1, "N2", "M2", db_path=db)

    updated = mark_all_notifications_read(u1, db_path=db)
    assert updated == 2
    assert get_unread_notification_count(u1, db_path=db) == 0

    # Delete single
    assert delete_notification(nid1, u1, db_path=db)
    remaining = get_notifications(u1, db_path=db)
    assert len(remaining) == 1
    assert remaining[0]["id"] == nid2

    # Clear all
    cleared = clear_all_notifications(u1, db_path=db)
    assert cleared == 1
    assert len(get_notifications(u1, db_path=db)) == 0
