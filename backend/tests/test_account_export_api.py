"""End-to-end tests: account export over HTTP, through the scheduler, to a download."""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import sqlite3
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import account_deletion as ad
import account_export as ae
import pytest
import scheduler
from database import (
    get_or_create_user,
    init_db,
    save_body_metrics,
    save_chat_message,
    save_notification,
    save_user_api_key,
)
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

_SECRET = "account-export-test-secret-that-is-long-enough"
_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()
EMAIL_A = "athlete.a@example.com"
EMAIL_B = "athlete.b@example.com"
ROOT = ae.ARCHIVE_ROOT
EXPORTS = "/api/account/exports"


class Env(NamedTuple):
    db: str
    a: str
    b: str
    client_for: Callable[[str, str], TestClient]
    run_worker: Callable[[], int]


def _load_app(monkeypatch: pytest.MonkeyPatch, db: str, *, authenticated: bool) -> Any:
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    for name in (
        "ACCOUNT_DELETION_GRACE_DAYS",
        "ACCOUNT_EXPORT_RETENTION_HOURS",
        "ACCOUNT_EXPORT_MAX_MB",
        "ACCOUNT_EXPORT_DAILY_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    if authenticated:
        monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "export-client-id")
        monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "export-client-secret")
        monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    else:
        monkeypatch.delenv("WEB_GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("WEB_GOOGLE_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    import webapp.app as webapp_app

    return importlib.reload(webapp_app)


def _seed(db: str, user_id: str, tag: str) -> None:
    save_chat_message("user", f"question-{tag}", db_path=db, user_id=user_id)
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, user_id=user_id)
    save_notification(user_id, f"Title {tag}", "Message", db_path=db)
    save_user_api_key(user_id, "hevy", f"SECRET-KEY-{tag}", db_path=db)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv("ENCRYPTION_KEY", _ENCRYPTION_KEY)
    db = str(tmp_path / "data" / "api.db")
    init_db(db)
    a = get_or_create_user(EMAIL_A, "Athlete A", db_path=db)["id"]
    b = get_or_create_user(EMAIL_B, "Athlete B", db_path=db)["id"]
    _seed(db, a, "A")
    _seed(db, b, "B")
    webapp_app = _load_app(monkeypatch, db, authenticated=True)

    def client_for(email: str, user_id: str) -> TestClient:
        client = TestClient(webapp_app.app)
        client.cookies.set(
            webapp_app.SESSION_SECURITY.cookie.name,
            signed_session_cookie(
                _SECRET,
                user=email,
                user_id=user_id,
                session_id=webapp_app.SESSION_STORE.create(user_id),
            ),
        )
        return client

    def run_worker() -> int:
        # The same entry point the scheduler process calls on every tick.
        return scheduler.run_data_exports(SimpleNamespace(database_path=db))  # type: ignore[arg-type]

    return Env(db, a, b, client_for, run_worker)


def _rows(db: str, sql: str, *params: Any) -> list[sqlite3.Row]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(sql, params))


def _events(db: str, event_type: str, user_id: str) -> list[sqlite3.Row]:
    return _rows(
        db,
        "SELECT * FROM security_audit_events WHERE event_type = ? AND user_id = ? ORDER BY seq",
        event_type,
        user_id,
    )


def _archive(content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(content))


# ---------------------------------------------------------------------------
# Authentication and ownership
# ---------------------------------------------------------------------------


def test_the_export_routes_are_registered_with_their_methods(env: Env) -> None:
    app = importlib.import_module("webapp.app").app
    registered = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", None) or ()
    }
    assert {
        ("GET", "/api/account/exports"),
        ("POST", "/api/account/exports"),
        ("GET", "/api/account/exports/{export_id}"),
        ("GET", "/api/account/exports/{export_id}/download"),
    } <= registered


def test_every_export_route_requires_a_signed_in_user(env: Env) -> None:
    anonymous = TestClient(importlib.import_module("webapp.app").app)
    export_id = "0" * 32
    assert anonymous.get(EXPORTS).status_code == 401
    assert anonymous.post(EXPORTS, json={}).status_code == 401
    assert anonymous.get(f"{EXPORTS}/{export_id}").status_code == 401
    assert anonymous.get(f"{EXPORTS}/{export_id}/download").status_code == 401
    assert _rows(env.db, "SELECT 1 FROM account_export_requests") == []


def test_a_cookie_that_was_never_registered_is_refused(env: Env) -> None:
    forged = TestClient(importlib.import_module("webapp.app").app)
    forged.cookies.set(
        importlib.import_module("webapp.app").SESSION_SECURITY.cookie.name,
        signed_session_cookie(_SECRET, user=EMAIL_A, user_id=env.a),  # no registered session
    )
    assert forged.post(EXPORTS, json={}).status_code == 401


def test_the_description_lists_what_is_and_is_not_exported(env: Env) -> None:
    response = env.client_for(EMAIL_A, env.a).get(EXPORTS)
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    body = response.json()
    assert body["eligible"] is True
    assert (body["retention_hours"], body["daily_limit"], body["remaining_today"]) == (24, 5, 5)
    assert {item["category"] for item in body["included"]} >= {
        "profile",
        "programmes",
        "workouts",
        "metrics",
        "check_ins",
        "connectors",
        "notifications",
        "coaching",
        "audit",
    }
    assert {item["category"] for item in body["excluded"]} >= {"credentials", "sessions", "other_accounts"}
    assert body["exports"] == []


def test_client_supplied_owner_ids_are_never_used(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    for attempt in (
        client.post(EXPORTS, json={"user_id": env.b}),
        client.post(EXPORTS, json={"owner_id": env.b}),
        client.post(EXPORTS, json={}, headers={"X-User-Id": env.b}),
        client.post(f"{EXPORTS}?user_id={env.b}", json={}, headers={"X-Owner-Id": env.b}),
    ):
        assert attempt.status_code in {404, 422}
    assert _rows(env.db, "SELECT 1 FROM account_export_requests") == []

    created = client.post(EXPORTS, json={})
    assert created.status_code == 202
    assert _rows(env.db, "SELECT user_id FROM account_export_requests")[0]["user_id"] == env.a


# ---------------------------------------------------------------------------
# The whole journey: request -> scheduler -> status -> download -> expiry
# ---------------------------------------------------------------------------


def test_export_request_build_download_and_expiry_end_to_end(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)

    # 1. Ask. The request is accepted, not served: building is asynchronous.
    created = client.post(EXPORTS, json={})
    assert created.status_code == 202
    queued = created.json()
    export_id = queued["export_id"]
    assert queued["state"] == "queued"
    assert queued["download_url"] is None and queued["can_download"] is False
    assert created.headers["location"] == f"{EXPORTS}/{export_id}"
    assert "no-store" in created.headers["cache-control"]

    # 2. Asking again while it is open returns the same request, not a duplicate.
    again = client.post(EXPORTS)  # the body is optional
    assert again.status_code == 200 and again.json()["export_id"] == export_id
    assert len(_rows(env.db, "SELECT 1 FROM account_export_requests")) == 1

    # 3. Nothing can be downloaded before the worker has run.
    assert client.get(f"{EXPORTS}/{export_id}").json()["state"] == "queued"
    early = client.get(f"{EXPORTS}/{export_id}/download")
    assert early.status_code == 409 and early.json()["detail"] == "not_ready"

    # 4. The scheduler process builds it.
    assert env.run_worker() == 1

    # 5. The owner sees a ready, expiring export with a download link.
    ready = client.get(f"{EXPORTS}/{export_id}").json()
    assert ready["state"] == "ready" and ready["can_download"] is True
    assert ready["download_url"] == f"{EXPORTS}/{export_id}/download"
    assert ready["size_bytes"] and ready["sha256"] and ready["record_count"]
    assert datetime.fromisoformat(ready["expires_at"]) - datetime.fromisoformat(
        ready["completed_at"]
    ) == timedelta(hours=24)
    listed = client.get(EXPORTS).json()
    assert [item["export_id"] for item in listed["exports"]] == [export_id]
    assert listed["remaining_today"] == 4

    # 6. Download: an attachment that is never cached, byte-for-byte what was recorded.
    download = client.get(ready["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    disposition = download.headers["content-disposition"]
    assert disposition.startswith("attachment;") and ".zip" in disposition
    assert EMAIL_A not in disposition and env.a not in disposition
    assert "no-store" in download.headers["cache-control"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert int(download.headers["content-length"]) == len(download.content) == ready["size_bytes"]
    assert hashlib.sha256(download.content).hexdigest() == ready["sha256"]

    archive = _archive(download.content)
    assert archive.testzip() is None
    manifest = json.loads(archive.read(f"{ROOT}/manifest.json"))
    assert manifest["account_id"] == env.a and manifest["export_id"] == export_id
    text = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    assert "question-A" in text and "Title A" in text and EMAIL_A in text
    for foreign in ("question-B", "Title B", EMAIL_B, env.b, "SECRET-KEY-A", "SECRET-KEY-B"):
        assert foreign not in text, foreign

    # 7. The delivery is audited against the owner, with the export as its subject.
    delivered = _events(env.db, "data.exported", env.a)
    assert len(delivered) == 1
    assert (delivered[0]["outcome"], delivered[0]["subject_id"]) == ("success", export_id)
    assert delivered[0]["actor_id"] == env.a and delivered[0]["request_id"]
    assert json.loads(delivered[0]["metadata_json"])["size_bytes"] == ready["size_bytes"]

    # 8. A resumed range of the same download is not a second disclosure event.
    resumed = client.get(ready["download_url"], headers={"Range": "bytes=100-199"})
    assert resumed.status_code == 206 and resumed.content == download.content[100:200]
    assert len(_events(env.db, "data.exported", env.a)) == 1

    # 9. Expiry: the next worker tick after the deadline removes the file...
    path = ad.export_directory(env.db, env.a) / f"{export_id}.zip"  # type: ignore[operator]
    assert path.exists()
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "UPDATE account_export_requests SET expires_at = ? WHERE id = ?",
            ((datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat(timespec="microseconds"), export_id),
        )
    # ...but the download is already refused before any sweep has run.
    gone = client.get(ready["download_url"])
    assert gone.status_code == 410 and gone.json()["detail"] == "expired"
    assert path.exists()
    env.run_worker()
    assert not path.exists()
    expired = client.get(f"{EXPORTS}/{export_id}").json()
    assert expired["state"] == "expired" and expired["download_url"] is None
    assert client.get(f"{EXPORTS}/{export_id}/download").status_code == 410

    # 10. The owner can simply ask again.
    assert client.post(EXPORTS, json={}).status_code == 202


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_another_account_cannot_see_or_download_an_export(env: Env) -> None:
    owner = env.client_for(EMAIL_A, env.a)
    intruder = env.client_for(EMAIL_B, env.b)
    export_id = owner.post(EXPORTS, json={}).json()["export_id"]
    env.run_worker()
    assert owner.get(f"{EXPORTS}/{export_id}/download").status_code == 200

    assert intruder.get(EXPORTS).json()["exports"] == []
    status = intruder.get(f"{EXPORTS}/{export_id}")
    missing = intruder.get(f"{EXPORTS}/{'0' * 32}")
    assert status.status_code == missing.status_code == 404  # indistinguishable
    assert status.json() == missing.json()
    stolen = intruder.get(f"{EXPORTS}/{export_id}/download")
    assert stolen.status_code == 404 and stolen.json() == {"detail": "not_found"}
    for hostile in ("..%2F..%2Fworkout.db", "ABCDEF", export_id.upper(), "x" * 200):
        assert intruder.get(f"{EXPORTS}/{hostile}/download").status_code == 404

    # The attempt is audited as denied and raises the operator alert.
    denied = [e for e in _events(env.db, "data.exported", env.b) if e["outcome"] == "denied"]
    assert denied and json.loads(denied[0]["metadata_json"]) == {"reason": "not_found"}
    assert denied[0]["actor_id"] == env.b
    assert denied[0]["subject_id"] in (export_id, None)
    alerts = _events(env.db, "alert.unusual_export_activity", env.b)
    assert alerts and alerts[0]["severity"] == "critical"
    assert _events(env.db, "alert.unusual_export_activity", env.a) == []
    # And the owner's archive was never disclosed to B.
    assert [e["outcome"] for e in _events(env.db, "data.exported", env.a)] == ["success"]


def test_each_account_gets_only_its_own_archive(env: Env) -> None:
    a_client = env.client_for(EMAIL_A, env.a)
    b_client = env.client_for(EMAIL_B, env.b)
    a_id = a_client.post(EXPORTS, json={}).json()["export_id"]
    b_id = b_client.post(EXPORTS, json={}).json()["export_id"]
    assert env.run_worker() == 2

    a_text = a_client.get(f"{EXPORTS}/{a_id}/download").content
    b_text = b_client.get(f"{EXPORTS}/{b_id}/download").content
    a_names = "\n".join(_archive(a_text).read(n).decode() for n in _archive(a_text).namelist())
    b_names = "\n".join(_archive(b_text).read(n).decode() for n in _archive(b_text).namelist())
    assert "question-A" in a_names and "question-B" not in a_names
    assert "question-B" in b_names and "question-A" not in b_names
    assert a_client.get(f"{EXPORTS}/{b_id}/download").status_code == 404
    assert b_client.get(f"{EXPORTS}/{a_id}/download").status_code == 404


def test_a_ready_export_whose_file_vanished_is_gone_not_a_server_error(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    export_id = client.post(EXPORTS, json={}).json()["export_id"]
    env.run_worker()
    (ad.export_directory(env.db, env.a) / f"{export_id}.zip").unlink()  # type: ignore[operator]
    gone = client.get(f"{EXPORTS}/{export_id}/download")
    assert gone.status_code == 410 and gone.json()["detail"] == "expired"


def test_failed_exports_report_a_code_and_offer_no_download(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    export_id = client.post(EXPORTS, json={}).json()["export_id"]
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "UPDATE account_export_requests SET state = 'failed', error_code = 'too_large' WHERE id = ?",
            (export_id,),
        )
    body = client.get(f"{EXPORTS}/{export_id}").json()
    assert (body["state"], body["error_code"], body["download_url"]) == ("failed", "too_large", None)
    assert client.get(f"{EXPORTS}/{export_id}/download").status_code == 409


# ---------------------------------------------------------------------------
# Limits and account state
# ---------------------------------------------------------------------------


def test_requests_are_limited_per_day(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCOUNT_EXPORT_DAILY_LIMIT", "1")
    client = env.client_for(EMAIL_A, env.a)
    assert client.post(EXPORTS, json={}).status_code == 202
    env.run_worker()
    limited = client.post(EXPORTS, json={})
    assert limited.status_code == 429 and limited.json()["detail"] == "rate_limited"
    assert env.client_for(EMAIL_B, env.b).post(EXPORTS, json={}).status_code == 202  # per account
    assert client.get(EXPORTS).json()["remaining_today"] == 0


def test_the_request_rate_limit_applies_per_minute(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    codes = [client.post(EXPORTS, json={}).status_code for _ in range(7)]
    assert codes[:5] == [202, 200, 200, 200, 200]
    assert 429 in codes[5:]


def test_an_account_can_export_while_its_closure_is_pending(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    assert client.post("/api/account/deletion", json={"scope": "account", "confirmation": EMAIL_A}).status_code == 202
    export_id = client.post(EXPORTS, json={}).json()["export_id"]
    env.run_worker()
    assert client.get(f"{EXPORTS}/{export_id}/download").status_code == 200


def test_a_locked_account_is_refused_and_a_closed_one_fails_closed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = env.client_for(EMAIL_A, env.a)
    export_id = client.post(EXPORTS, json={}).json()["export_id"]
    env.run_worker()
    monkeypatch.setenv("ACCOUNT_DELETION_GRACE_DAYS", "0")
    request = ad.request_deletion(env.db, env.a, "account", EMAIL_A)
    with sqlite3.connect(env.db) as conn:  # erasure has started
        conn.execute("UPDATE account_deletion_requests SET state = 'in_progress' WHERE id = ?", (request["request_id"],))
    for response in (
        client.get(EXPORTS),
        client.post(EXPORTS, json={}),
        client.get(f"{EXPORTS}/{export_id}"),
        client.get(f"{EXPORTS}/{export_id}/download"),
    ):
        assert response.status_code == 423

    with sqlite3.connect(env.db) as conn:
        conn.execute("UPDATE account_deletion_requests SET state = 'pending' WHERE id = ?", (request["request_id"],))
    ad.execute_deletion(env.db, request["request_id"])
    assert client.get(EXPORTS).status_code == 401
    assert client.get(f"{EXPORTS}/{export_id}/download").status_code == 401
    assert _rows(env.db, "SELECT 1 FROM account_export_requests WHERE user_id = ?", env.a) == []
    other = env.client_for(EMAIL_B, env.b)
    assert other.get(EXPORTS).status_code == 200  # nobody else is affected


def test_a_suspended_account_cannot_export(env: Env) -> None:
    client = env.client_for(EMAIL_A, env.a)
    with sqlite3.connect(env.db) as conn:
        conn.execute("UPDATE users SET status = 'suspended' WHERE id = ?", (env.a,))
    assert client.post(EXPORTS, json={}).status_code == 401
    assert _rows(env.db, "SELECT 1 FROM account_export_requests") == []


def test_the_shared_local_tenant_cannot_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anonymous local mode is read-only and has no owner to hand an archive to."""
    monkeypatch.setenv("ENCRYPTION_KEY", _ENCRYPTION_KEY)
    db = str(tmp_path / "local" / "local.db")
    init_db(db)
    webapp_app = _load_app(monkeypatch, db, authenticated=False)
    client = TestClient(webapp_app.app)
    described = client.get(EXPORTS)
    assert described.status_code == 200 and described.json()["eligible"] is False
    assert client.post(EXPORTS, json={}).status_code == 401  # no anonymous writes
    legacy = _rows(db, "SELECT id FROM users WHERE email = 'legacy@local'")[0]["id"]
    with pytest.raises(ae.ExportError) as caught:
        ae.request_export(db, legacy)
    assert caught.value.code == "not_eligible"
    assert _rows(db, "SELECT 1 FROM account_export_requests") == []
