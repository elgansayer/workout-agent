"""End-to-end tests for the account data export through the real web app.

Each test drives the FastAPI application with signed session cookies, a real
SQLite database, the real background worker and real ZIP artifacts on disk.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import re
import sqlite3
import threading
import time
import zipfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

import account_export as ax
import scheduler
from account_export_support import SESSION_SECRET, secret_values, seed_tenant
from database import init_db
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from webapp.csrf_security import CSRF_HEADER_NAME, CSRFMiddleware

NOT_FOUND = {"detail": "Export not found"}


def _cookie(payload: dict[str, str]) -> str:
    raw = base64.b64encode(json.dumps(payload).encode())
    return TimestampSigner(SESSION_SECRET).sign(raw).decode()


def _session(email: str, user_id: str) -> str:
    return _cookie({"user": email, "user_id": user_id})


@dataclass
class Api:
    module: Any
    db_path: str
    export_dir: Path
    alpha: str
    bravo: str
    alpha_client: TestClient
    bravo_client: TestClient
    anonymous: TestClient


def _wait(client: TestClient, export_id: str, *, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/account/exports/{export_id}")
        assert response.status_code == 200, response.text
        export = response.json()["export"]
        if export["status"] in {"ready", "failed", "expired"}:
            return export
        time.sleep(0.02)
    raise AssertionError("export did not finish before the timeout")


def _request_and_wait(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/account/exports")
    assert response.status_code == 202, response.text
    return _wait(client, response.json()["export"]["id"])


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Api, None, None]:
    db_path = str(tmp_path / "api.db")
    export_dir = tmp_path / "exports"
    init_db(db_path)
    alpha = seed_tenant(
        db_path, email="alpha@example.com", display_name="Alpha Example", tag="alpha"
    )
    bravo = seed_tenant(
        db_path, email="bravo@example.com", display_name="Bravo Example", tag="bravo"
    )
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "export-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "export-client-secret")
    monkeypatch.setenv("WEB_AUTH_SECRET", SESSION_SECRET)
    monkeypatch.setenv("ACCOUNT_EXPORT_DIR", str(export_dir))
    monkeypatch.delenv("ACCOUNT_EXPORT_ENABLED", raising=False)
    monkeypatch.delenv("ACCOUNT_EXPORT_TTL_HOURS", raising=False)
    ax.shutdown_runner()

    import webapp.app as webapp_app

    importlib.reload(webapp_app)
    application = webapp_app.app

    def client_for(cookie: str | None) -> TestClient:
        client = TestClient(application)
        if cookie is not None:
            client.cookies.set("session", cookie)
        return client

    yield Api(
        module=webapp_app,
        db_path=db_path,
        export_dir=export_dir,
        alpha=alpha,
        bravo=bravo,
        alpha_client=client_for(_session("alpha@example.com", alpha)),
        bravo_client=client_for(_session("bravo@example.com", bravo)),
        anonymous=client_for(None),
    )
    ax.shutdown_runner()


# ---------------------------------------------------------------------------
# The full asynchronous flow
# ---------------------------------------------------------------------------


def test_export_is_requested_built_asynchronously_and_downloaded(api: Api) -> None:
    started = api.alpha_client.post("/api/account/exports")
    assert started.status_code == 202
    body = started.json()
    assert body["created"] is True
    queued = body["export"]
    assert queued["status"] == "queued"
    assert queued["download_url"] is None
    assert re.fullmatch(r"[0-9a-f]{32}", queued["id"])
    assert "user_id" not in json.dumps(body)

    finished = _wait(api.alpha_client, queued["id"])
    assert finished["status"] == "ready"
    assert finished["download_url"] == f"/api/account/exports/{queued['id']}/download"
    assert finished["schema_version"] == 1
    assert finished["record_counts"]["profile.account"] == 1
    assert finished["size_bytes"] > 0

    listing = api.alpha_client.get("/api/account/exports").json()
    assert [item["id"] for item in listing["exports"]] == [queued["id"]]
    assert listing["retention_hours"] == 24
    assert listing["enabled"] is True

    download = api.alpha_client.get(finished["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert re.fullmatch(
        r'attachment; filename="workout-agent-export-\d{4}-\d{2}-\d{2}\.zip"',
        download.headers["content-disposition"],
    )
    assert int(download.headers["content-length"]) == finished["size_bytes"]
    assert hashlib.sha256(download.content).hexdigest() == finished["checksum_sha256"]

    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        text = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    assert manifest["export_id"] == queued["id"]
    assert [s["section"] for s in manifest["sections"]] == [
        "profile",
        "programmes",
        "workouts",
        "metrics",
        "check_ins",
        "connectors",
        "notifications",
        "coaching",
        "audit",
    ]
    assert "alpha@example.com" in text and "alpha-chat-question" in text
    assert "bravo" not in text
    for owner in ("alpha", "bravo"):
        for secret in secret_values(owner):
            assert secret not in text


def test_requests_while_an_export_is_building_share_one_job(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build = ax.build_archive
    started, release = threading.Event(), threading.Event()

    def slow_build(*args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(20)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(ax, "build_archive", slow_build)
    try:
        first = api.alpha_client.post("/api/account/exports").json()
        assert started.wait(10)
        second = api.alpha_client.post("/api/account/exports").json()
        assert second["created"] is False
        assert second["export"]["id"] == first["export"]["id"]

        status = api.alpha_client.get(f"/api/account/exports/{first['export']['id']}").json()
        assert status["export"]["status"] == "running"
        assert status["export"]["download_url"] is None
        not_ready = api.alpha_client.get(
            f"/api/account/exports/{first['export']['id']}/download"
        )
        assert not_ready.status_code == 409
        assert not_ready.json() == {"detail": "The export is not ready yet"}

        # Another account is not blocked by this account's job.
        other = api.bravo_client.post("/api/account/exports").json()
        assert other["created"] is True
        assert other["export"]["id"] != first["export"]["id"]
    finally:
        release.set()
    assert _wait(api.alpha_client, first["export"]["id"])["status"] == "ready"
    assert _wait(api.bravo_client, other["export"]["id"])["status"] == "ready"


def test_audit_trail_of_exports_appears_in_the_next_export(api: Api) -> None:
    first = _request_and_wait(api.alpha_client)
    assert api.alpha_client.get(first["download_url"]).status_code == 200
    second = _request_and_wait(api.alpha_client)
    body = api.alpha_client.get(second["download_url"]).content
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        events = json.loads(archive.read("audit/events.json"))["records"]
    assert [event["event_type"] for event in events] == [
        "account_export.requested",
        "account_export.completed",
        "account_export.downloaded",
        "account_export.requested",
    ]
    assert {event["resource_id"] for event in events[:3]} == {first["id"]}
    assert all(event["outcome"] == "success" for event in events)
    assert set(events[0]) == {
        "event_id",
        "event_type",
        "outcome",
        "resource_type",
        "resource_id",
        "occurred_at",
    }


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method_and_path",
    [
        ("POST", "/api/account/exports"),
        ("GET", "/api/account/exports"),
        ("GET", "/api/account/exports/" + "a" * 32),
        ("GET", "/api/account/exports/" + "a" * 32 + "/download"),
        ("DELETE", "/api/account/exports/" + "a" * 32),
    ],
)
def test_every_export_route_requires_a_signed_in_account(
    api: Api, method_and_path: tuple[str, str]
) -> None:
    method, path = method_and_path
    assert api.anonymous.request(method, path).status_code == 401
    forged = TestClient(api.module.app)
    forged.cookies.set("session", "not-a-valid-signature")
    assert forged.request(method, path).status_code == 401
    for incomplete in ({"user": "alpha@example.com"}, {"user_id": api.alpha}):
        partial = TestClient(api.module.app)
        partial.cookies.set("session", _cookie(incomplete))
        assert partial.request(method, path).status_code == 401, incomplete


def test_anonymous_clients_cannot_download_a_known_export_id(api: Api) -> None:
    export = _request_and_wait(api.alpha_client)
    response = api.anonymous.get(export["download_url"])
    assert response.status_code == 401
    assert b"PK" not in response.content[:2]


def test_other_accounts_cannot_see_download_or_delete_an_export(api: Api) -> None:
    export = _request_and_wait(api.alpha_client)
    export_id = export["id"]
    missing = "0" * 32

    for path in (
        f"/api/account/exports/{export_id}",
        f"/api/account/exports/{export_id}/download",
    ):
        foreign = api.bravo_client.get(path)
        unknown = api.bravo_client.get(path.replace(export_id, missing))
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json() == unknown.json() == NOT_FOUND
    foreign_delete = api.bravo_client.delete(f"/api/account/exports/{export_id}")
    assert foreign_delete.status_code == 404
    assert foreign_delete.json() == NOT_FOUND

    assert api.bravo_client.get("/api/account/exports").json()["exports"] == []
    # The owner is unaffected and no extra audit trail was written for either side.
    assert api.alpha_client.get(export["download_url"]).status_code == 200
    with sqlite3.connect(api.db_path) as conn:
        bravo_events = conn.execute(
            "SELECT COUNT(*) FROM account_audit_events WHERE user_id = ?", (api.bravo,)
        ).fetchone()
    assert bravo_events == (0,)


@pytest.mark.parametrize(
    "export_id",
    ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "A" * 32, "a" * 31, "a" * 33, "'; DROP TABLE users;--", "%00"],
)
def test_malformed_export_ids_are_plain_not_found(api: Api, export_id: str) -> None:
    for method, suffix in (("GET", ""), ("GET", "/download"), ("DELETE", "")):
        response = api.alpha_client.request(method, f"/api/account/exports/{export_id}{suffix}")
        assert response.status_code in {404, 405}, (method, export_id, response.status_code)
    with sqlite3.connect(api.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 2


def test_client_supplied_owner_claims_cannot_redirect_an_export(api: Api) -> None:
    attempts = [
        {"json": {"user_id": api.bravo}},
        {"params": {"user_id": api.bravo}},
        {"headers": {"X-User-Id": api.bravo}},
        {"json": {"account_id": api.bravo}},
    ]
    for attempt in attempts:
        response = api.alpha_client.post("/api/account/exports", **attempt)
        assert response.status_code == 404, attempt
    with sqlite3.connect(api.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM account_exports").fetchone() == (0,)


def test_each_account_downloads_only_its_own_archive(api: Api) -> None:
    alpha = _request_and_wait(api.alpha_client)
    bravo = _request_and_wait(api.bravo_client)
    alpha_zip = api.alpha_client.get(alpha["download_url"]).content
    bravo_zip = api.bravo_client.get(bravo["download_url"]).content
    for blob, owner, other in ((alpha_zip, "alpha", "bravo"), (bravo_zip, "bravo", "alpha")):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            text = "\n".join(archive.read(n).decode("utf-8") for n in archive.namelist())
        assert owner in text and other not in text
    # Bravo presenting Alpha's id with its own session still gets nothing.
    assert api.bravo_client.get(alpha["download_url"]).status_code == 404


def test_anonymous_mode_never_exposes_the_legacy_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = str(tmp_path / "anonymous.db")
    init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    for name in ("WEB_GOOGLE_CLIENT_ID", "WEB_GOOGLE_CLIENT_SECRET", "WEB_AUTH_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACCOUNT_EXPORT_DIR", str(tmp_path / "exports"))

    import webapp.app as webapp_app

    importlib.reload(webapp_app)
    with TestClient(webapp_app.app) as client:
        # The dashboard's anonymous mode serves the legacy tenant elsewhere...
        assert client.get("/api/notifications").status_code == 200
        # ...but a whole-account export always needs a verified session.
        assert client.get("/api/account/exports").status_code == 401
        assert client.post("/api/account/exports").status_code == 401
        assert client.get("/api/account/exports/" + "a" * 32).status_code == 401
    assert not (tmp_path / "exports").exists()


def test_csrf_protection_applies_to_export_mutations(
    api: Api, tmp_path: Path
) -> None:
    wrapped = CSRFMiddleware(
        api.module.app,
        db_path=str(tmp_path / "csrf.db"),
        session_secret=SESSION_SECRET,
        trusted_origins=(),
    )
    client = TestClient(wrapped, base_url="https://workout.example")
    client.cookies.set("session", _session("alpha@example.com", api.alpha))

    assert client.post("/api/account/exports").status_code == 403
    token = client.get("/api/csrf-token").json()["token"]
    created = client.post("/api/account/exports", headers={CSRF_HEADER_NAME: token})
    assert created.status_code == 202
    export_id = created.json()["export"]["id"]
    assert _wait(client, export_id)["status"] == "ready"
    assert client.delete(f"/api/account/exports/{export_id}").status_code == 403
    token = client.get("/api/csrf-token").json()["token"]
    deleted = client.delete(
        f"/api/account/exports/{export_id}", headers={CSRF_HEADER_NAME: token}
    )
    assert deleted.status_code == 200


def test_export_responses_are_never_cacheable(api: Api) -> None:
    export = _request_and_wait(api.alpha_client)
    responses = [
        api.alpha_client.post("/api/account/exports"),
        api.alpha_client.get("/api/account/exports"),
        api.alpha_client.get(f"/api/account/exports/{export['id']}"),
        api.alpha_client.get(export["download_url"]),
        api.anonymous.get(export["download_url"]),
    ]
    for response in responses:
        cache_control = response.headers["cache-control"]
        assert "no-store" in cache_control and "private" in cache_control


def test_export_routes_are_registered_on_the_live_app(api: Api) -> None:
    registered = {
        (method, route.path)
        for route in api.module.app.routes
        for method in getattr(route, "methods", ())
    }
    assert {
        ("POST", "/api/account/exports"),
        ("GET", "/api/account/exports"),
        ("GET", "/api/account/exports/{export_id}"),
        ("GET", "/api/account/exports/{export_id}/download"),
        ("DELETE", "/api/account/exports/{export_id}"),
    } <= registered


# ---------------------------------------------------------------------------
# Expiry and deletion
# ---------------------------------------------------------------------------


def _expire_now(api: Api, export_id: str) -> None:
    with sqlite3.connect(api.db_path) as conn:
        conn.execute(
            "UPDATE account_exports SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (export_id,),
        )


def test_expired_exports_are_not_served_and_their_files_are_removed(api: Api) -> None:
    export = _request_and_wait(api.alpha_client)
    artifact = api.export_dir / f"{export['id']}.zip"
    assert artifact.exists()

    _expire_now(api, export["id"])
    gone = api.alpha_client.get(export["download_url"])
    assert gone.status_code == 410
    assert gone.json() == {"detail": "The export is no longer available. Request a new one."}
    assert not artifact.exists()

    listed = api.alpha_client.get("/api/account/exports").json()["exports"]
    assert [(item["id"], item["status"], item["download_url"]) for item in listed] == [
        (export["id"], "expired", None)
    ]
    # An expired export frees the account to request a fresh one.
    fresh = api.alpha_client.post("/api/account/exports").json()
    assert fresh["created"] is True and fresh["export"]["id"] != export["id"]


def test_scheduled_sweep_removes_expired_exports_of_accounts_that_never_return(
    api: Api,
) -> None:
    export = _request_and_wait(api.alpha_client)
    artifact = api.export_dir / f"{export['id']}.zip"
    _expire_now(api, export["id"])
    scheduler.run_export_maintenance(MagicMock(database_path=api.db_path))
    assert not artifact.exists()
    with sqlite3.connect(api.db_path) as conn:
        status = conn.execute(
            "SELECT status FROM account_exports WHERE id = ?", (export["id"],)
        ).fetchone()
    assert status == ("expired",)


def test_web_start_up_sweeps_expired_exports(api: Api) -> None:
    export = _request_and_wait(api.alpha_client)
    artifact = api.export_dir / f"{export['id']}.zip"
    _expire_now(api, export["id"])
    assert artifact.exists()
    with TestClient(api.module.app):  # entering the context runs the app lifespan
        pass
    assert not artifact.exists()


def test_a_failing_start_up_sweep_never_blocks_the_web_app(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(**_kwargs: Any) -> None:
        raise OSError("export directory unavailable")

    monkeypatch.setattr(ax, "maintain_exports", explode)
    with TestClient(api.module.app) as client:  # entering runs the lifespan
        client.cookies.set("session", _session("alpha@example.com", api.alpha))
        assert client.get("/api/account/exports").status_code == 200


def test_owner_can_delete_an_export_immediately(api: Api) -> None:
    export = _request_and_wait(api.alpha_client)
    artifact = api.export_dir / f"{export['id']}.zip"
    deleted = api.alpha_client.delete(f"/api/account/exports/{export['id']}")
    assert deleted.status_code == 200 and deleted.json() == {"status": "ok"}
    assert not artifact.exists()
    assert api.alpha_client.get(export["download_url"]).status_code == 404
    assert api.alpha_client.get("/api/account/exports").json()["exports"] == []
    assert api.alpha_client.delete(f"/api/account/exports/{export['id']}").status_code == 404


def test_deleting_an_export_that_is_still_building_cancels_it(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build = ax.build_archive
    started, release = threading.Event(), threading.Event()

    def slow_build(*args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(20)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(ax, "build_archive", slow_build)
    created = api.alpha_client.post("/api/account/exports").json()["export"]
    assert started.wait(10)
    assert api.alpha_client.delete(f"/api/account/exports/{created['id']}").status_code == 200
    release.set()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and any(api.export_dir.glob("*")):
        time.sleep(0.02)
    assert not list(api.export_dir.glob("*"))
    assert api.alpha_client.get(f"/api/account/exports/{created['id']}").status_code == 404


# ---------------------------------------------------------------------------
# Failure paths and operator controls
# ---------------------------------------------------------------------------


def test_failed_build_is_reported_without_internal_details(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom /private/path alpha-secret-hevy-key")

    monkeypatch.setattr(ax, "build_archive", explode)
    failed = _request_and_wait(api.alpha_client)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "build_failed"
    assert failed["download_url"] is None
    assert "boom" not in json.dumps(failed) and "private" not in json.dumps(failed)
    assert api.alpha_client.get(f"/api/account/exports/{failed['id']}/download").status_code == 410

    monkeypatch.undo()
    retry = _request_and_wait(api.alpha_client)
    assert retry["status"] == "ready" and retry["id"] != failed["id"]


def test_request_limit_returns_429_with_a_retry_hint(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ax, "MAX_REQUESTS_PER_WINDOW", 1)
    _request_and_wait(api.alpha_client)
    limited = api.alpha_client.post("/api/account/exports")
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    assert limited.json() == {"detail": "Too many export requests. Try again later."}
    assert api.bravo_client.post("/api/account/exports").status_code == 202


def test_operator_kill_switch_stops_creating_and_serving_exports(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = _request_and_wait(api.alpha_client)
    monkeypatch.setenv("ACCOUNT_EXPORT_ENABLED", "0")
    blocked = api.alpha_client.post("/api/account/exports")
    assert blocked.status_code == 503
    assert api.alpha_client.get(export["download_url"]).status_code == 503
    listing = api.alpha_client.get("/api/account/exports").json()
    assert listing["enabled"] is False
    assert api.alpha_client.delete(f"/api/account/exports/{export['id']}").status_code == 200
    monkeypatch.setenv("ACCOUNT_EXPORT_ENABLED", "1")
    assert api.alpha_client.post("/api/account/exports").status_code == 202


def test_retention_period_is_configurable(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCOUNT_EXPORT_TTL_HOURS", "6")
    assert api.alpha_client.get("/api/account/exports").json()["retention_hours"] == 6
    export = _request_and_wait(api.alpha_client)
    from datetime import datetime

    completed = datetime.fromisoformat(export["completed_at"])
    expires = datetime.fromisoformat(export["expires_at"])
    assert 5.9 * 3600 <= (expires - completed).total_seconds() <= 6.1 * 3600
