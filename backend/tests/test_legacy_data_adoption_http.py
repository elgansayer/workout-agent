"""After ``tools/legacy_data_migration.py`` runs, only the target account can read
the adopted legacy records over the real session-authenticated HTTP API.

Fixtures are synthetic; every legacy record carries a unique marker string so a
leak is detected regardless of how an endpoint shapes its response.
"""

from __future__ import annotations

import base64
import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import database
import pytest
from cryptography.fernet import Fernet
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

from tools.backup_restore import Keyring
from tools.legacy_data_migration import migrate

_SECRET = "legacy-adoption-secret-that-is-long-enough"
_MARKER = "legacy-marker-7f3a"
_ENDPOINTS = (
    "/api/history",
    "/api/checkins",
    "/api/progress",
    "/api/chat-history",
    "/api/chat/history",
    "/api/notifications",
    f"/api/xai_reasoning/{_MARKER}",
)


def _cookie(user_id: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user": "athlete@example.test", "user_id": user_id}).encode()
    )
    return TimestampSigner(_SECRET).sign(payload).decode()


def _seed_legacy_records(db_file: Path, legacy_id: str) -> None:
    marker = _MARKER
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "INSERT INTO workout_history (date, hevy_payload, user_id) VALUES (?, ?, ?)",
            ("2030-01-01", json.dumps({"title": marker}), legacy_id),
        )
        conn.execute(
            "INSERT INTO exercise_progress "
            "(date, exercise_name, top_weight_kg, top_reps, sets, user_id) "
            "VALUES ('2030-01-01', ?, 100.0, 5, 3, ?)",
            (marker, legacy_id),
        )
        conn.execute(
            "INSERT INTO daily_log (date, day, focus, carb_tier, plan, lifestyle, user_id) "
            "VALUES ('2030-01-01', 1, ?, 'medium', ?, 'Walk', ?)",
            (marker, marker, legacy_id),
        )
        conn.execute(
            "INSERT INTO check_ins "
            "(number, date, workouts_done, weeks, message, user_id) "
            "VALUES (1, '2030-01-01', 3, 1, ?, ?)",
            (marker, legacy_id),
        )
        conn.execute(
            "INSERT INTO chat_messages (role, content, created_at, user_id) "
            "VALUES ('user', ?, '2030-01-01T00:00:00Z', ?)",
            (marker, legacy_id),
        )
        conn.execute(
            "INSERT INTO reasoning_logs (user_id, context_id, date, exercise_name, reasoning) "
            "VALUES (?, ?, '2030-01-01', 'Squat', ?)",
            (legacy_id, marker, marker),
        )
    database.save_notification(
        legacy_id, title=marker, message=marker, db_path=str(db_file)
    )


def _adopted_database(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A database whose legacy records were adopted by the ``target`` account."""
    db_file = tmp_path / "workout_agent.db"
    database.init_db(str(db_file))
    ids = {"legacy": database.get_legacy_user_id(str(db_file))}
    for name in ("target", "second"):
        ids[name] = database.get_or_create_user(
            f"{name}@example.invalid", name.title(), db_path=str(db_file)
        )["id"]
    _seed_legacy_records(db_file, ids["legacy"])

    migrate(
        db_file,
        target_user_id=ids["target"],
        backup_output=tmp_path / "pre-migration.wab",
        keyring=Keyring(keys={"test": Fernet.generate_key()}, active_key_id="test"),
    )
    # An account created after the migration, as at launch.
    ids["later"] = database.get_or_create_user(
        "later@example.invalid", "Later", db_path=str(db_file)
    )["id"]
    return db_file, ids


def _load_app(
    monkeypatch: pytest.MonkeyPatch, db_file: Path, *, google_sign_in: bool
) -> Any:
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    monkeypatch.setenv("APP_ENV", "test")
    if google_sign_in:
        monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
        monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "adoption-client-id")
        monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "adoption-client-secret")
    else:  # the pre-launch single-user mode that resolves to the legacy tenant
        for name in (
            "WEB_AUTH_SECRET",
            "WEB_GOOGLE_CLIENT_ID",
            "WEB_GOOGLE_CLIENT_SECRET",
        ):
            monkeypatch.delenv(name, raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture()
def adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[str, str]]:
    db_file, ids = _adopted_database(tmp_path)
    return _load_app(monkeypatch, db_file, google_sign_in=True), ids


def _responses(client: TestClient) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for endpoint in _ENDPOINTS:
        response = client.get(endpoint)
        assert response.status_code == 200, endpoint
        bodies[endpoint] = response.text
    return bodies


def test_target_account_reads_the_adopted_records(
    adopted: tuple[Any, dict[str, str]],
) -> None:
    app_module, ids = adopted
    client = TestClient(app_module.app)
    client.cookies.set("session", _cookie(ids["target"]))

    bodies = _responses(client)

    for endpoint in _ENDPOINTS:
        assert _MARKER in bodies[endpoint], endpoint
    assert client.get("/api/stats").json()["total_sessions"] == 1


@pytest.mark.parametrize("account", ["second", "later", "legacy"])
def test_other_accounts_and_the_legacy_tenant_see_none_of_it(
    adopted: tuple[Any, dict[str, str]], account: str
) -> None:
    app_module, ids = adopted
    client = TestClient(app_module.app)
    client.cookies.set("session", _cookie(ids[account]))

    bodies = _responses(client)

    for endpoint, body in bodies.items():
        assert _MARKER not in body, f"{account} can read adopted data at {endpoint}"
    assert client.get("/api/stats").json()["total_sessions"] == 0


def test_anonymous_legacy_fallback_sees_none_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file, _ids = _adopted_database(tmp_path)
    app_module = _load_app(monkeypatch, db_file, google_sign_in=False)
    assert app_module.ALLOW_ANONYMOUS_WEB is True
    client = TestClient(app_module.app)  # no session: resolves to the legacy tenant

    bodies = _responses(client)

    for endpoint, body in bodies.items():
        assert _MARKER not in body, (
            f"anonymous fallback reads adopted data at {endpoint}"
        )
    assert client.get("/api/stats").json()["total_sessions"] == 0


def test_anonymous_requests_are_rejected_once_google_sign_in_is_configured(
    adopted: tuple[Any, dict[str, str]],
) -> None:
    app_module, _ids = adopted
    client = TestClient(app_module.app)

    for endpoint in _ENDPOINTS:
        assert client.get(endpoint).status_code == 401, endpoint
