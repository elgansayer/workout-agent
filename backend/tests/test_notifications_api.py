"""Tests for notifications API endpoints."""

from __future__ import annotations

import importlib
import pytest
from database import get_or_create_user, init_db, save_notification
from starlette.testclient import TestClient


@pytest.fixture
def client_with_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_api_notifications.db")
    init_db(db_file)
    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("WEB_AUTH_SECRET", "")

    import webapp.app as webapp_app
    importlib.reload(webapp_app)

    u1 = get_or_create_user("user1@example.com", "User One", db_path=db_file)
    u2 = get_or_create_user("user2@example.com", "User Two", db_path=db_file)

    monkeypatch.setattr(webapp_app, "_check_api_auth", lambda request: u1["id"])

    client = TestClient(webapp_app.app)
    return client, db_file, u1["id"], u2["id"]


def test_get_notifications_empty(client_with_db) -> None:
    client, db_file, u1, u2 = client_with_db
    response = client.get("/api/notifications")
    assert response.status_code == 200
    data = response.json()
    assert data["notifications"] == []
    assert data["unread_count"] == 0


def test_get_notifications_with_items(client_with_db) -> None:
    client, db_file, u1, u2 = client_with_db
    save_notification(u1, "Title 1", "Message 1", type="coach", link="/dashboard", db_path=db_file)
    save_notification(u1, "Title 2", "Message 2", type="sync", link="/history", db_path=db_file)
    save_notification(u2, "Title U2", "Message U2", type="system", db_path=db_file)

    response = client.get("/api/notifications")
    assert response.status_code == 200
    data = response.json()
    assert len(data["notifications"]) == 2
    assert data["unread_count"] == 2
    assert data["notifications"][0]["title"] == "Title 2"
    assert data["notifications"][1]["title"] == "Title 1"


def test_mark_notification_read_api(client_with_db) -> None:
    client, db_file, u1, u2 = client_with_db
    nid = save_notification(u1, "T1", "M1", db_path=db_file)

    response = client.post(f"/api/notifications/{nid}/read")
    assert response.status_code == 200
    assert response.json()["unread_count"] == 0

    # Non-existent or other user notification returns 404
    other_nid = save_notification(u2, "T2", "M2", db_path=db_file)
    bad_resp = client.post(f"/api/notifications/{other_nid}/read")
    assert bad_resp.status_code == 404


def test_mark_all_read_and_clear_api(client_with_db) -> None:
    client, db_file, u1, u2 = client_with_db
    save_notification(u1, "T1", "M1", db_path=db_file)
    save_notification(u1, "T2", "M2", db_path=db_file)

    # Mark all read
    resp1 = client.post("/api/notifications/read-all")
    assert resp1.status_code == 200
    assert resp1.json()["updated"] == 2
    assert resp1.json()["unread_count"] == 0

    # Clear all
    resp2 = client.post("/api/notifications/clear")
    assert resp2.status_code == 200
    assert resp2.json()["cleared"] == 2

    # Verify empty
    get_resp = client.get("/api/notifications")
    assert len(get_resp.json()["notifications"]) == 0


def test_test_coach_notification_endpoint(client_with_db) -> None:
    client, db_file, u1, u2 = client_with_db
    resp = client.post("/api/notifications/test-coach")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["unread_count"] == 1
