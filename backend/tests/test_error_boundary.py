"""Opaque production error responses, with detail kept in protected diagnostics.

Secrets and personal data are synthetic. Values are assembled from fragments so
that no complete provider-shaped token literal sits in the repository.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import re
import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from synthetic_secrets import EMAIL, GOOGLE_CODE, OPENAI_KEY

import diagnostics
from database import get_or_create_user, init_db
from webapp.error_boundary import (
    PUBLIC_ERROR_MESSAGE,
    OpaqueErrorMiddleware,
    install_error_boundary,
)

SECRET_MESSAGE = f"upstream rejected api_key={OPENAI_KEY} for {EMAIL}"
ERROR_ID = re.compile(r"err_[0-9a-f]{16}")
SESSION_SECRET = "error-boundary-test-secret-that-is-long-enough"


@pytest.fixture()
def diagnostics_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "diagnostics"
    monkeypatch.setenv("DIAGNOSTICS_DIR", str(directory))
    return directory


def _entries(directory: Path) -> list[dict[str, Any]]:
    path = directory / diagnostics.DIAGNOSTICS_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _build_app() -> FastAPI:
    app = FastAPI()
    install_error_boundary(app)

    @app.get("/boom/{item}")
    def boom(item: str) -> dict[str, str]:
        raise RuntimeError(SECRET_MESSAGE)

    @app.get("/api/boom")
    def api_boom() -> dict[str, str]:
        raise ValueError(SECRET_MESSAGE)

    @app.get("/denied")
    def denied() -> dict[str, str]:
        raise HTTPException(
            status_code=401,
            detail=f"bad credentials: Bearer {OPENAI_KEY}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/typed")
    def typed(limit: int) -> dict[str, int]:
        return {"limit": limit}

    return app


@pytest.fixture()
def client(diagnostics_dir: Path) -> TestClient:
    return TestClient(_build_app())


# ---------------------------------------------------------------------------
# Responses are opaque (acceptance criterion 2)
# ---------------------------------------------------------------------------


def test_unhandled_exception_returns_only_an_opaque_reference(client: TestClient) -> None:
    response = client.get("/api/boom")

    assert response.status_code == 500
    body = response.json()
    assert set(body) == {"detail", "error_id"}
    assert ERROR_ID.fullmatch(body["error_id"])
    assert body["detail"] == f"{PUBLIC_ERROR_MESSAGE} Reference: {body['error_id']}"
    assert "no-store" in response.headers["cache-control"]
    for leaked in (OPENAI_KEY, EMAIL, "ValueError", "Traceback", "boom", ".py", "upstream"):
        assert leaked not in response.text


def test_browser_navigation_gets_an_escaped_reference_page(client: TestClient) -> None:
    response = client.get("/boom/x", headers={"Accept": "text/html,application/xhtml+xml"})

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/html")
    reference = ERROR_ID.search(response.text)
    assert reference is not None
    for leaked in (OPENAI_KEY, EMAIL, "RuntimeError", "Traceback", "upstream"):
        assert leaked not in response.text


def test_api_paths_stay_json_even_when_the_client_accepts_html(client: TestClient) -> None:
    response = client.get("/api/boom", headers={"Accept": "text/html"})

    assert response.headers["content-type"].startswith("application/json")


def test_every_failure_gets_a_distinct_unguessable_reference(client: TestClient) -> None:
    ids = {client.get("/api/boom").json()["error_id"] for _ in range(5)}

    assert len(ids) == 5
    assert all(ERROR_ID.fullmatch(value) for value in ids)


def test_http_exception_detail_is_scrubbed_but_status_and_headers_survive(
    client: TestClient,
) -> None:
    response = client.get("/denied")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert OPENAI_KEY not in response.text
    assert response.json()["detail"].startswith("bad credentials")


def test_deliberate_http_errors_keep_their_user_facing_message(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_validation_errors_do_not_echo_the_submitted_value(client: TestClient) -> None:
    response = client.get(f"/typed?limit={OPENAI_KEY}")

    assert response.status_code == 422
    assert OPENAI_KEY not in response.text
    error = response.json()["detail"][0]
    assert error["loc"] == ["query", "limit"]
    assert error["type"] == "int_parsing"
    assert set(error) == {"type", "loc", "msg"}


# ---------------------------------------------------------------------------
# Detail stays in protected diagnostics
# ---------------------------------------------------------------------------


def test_traceback_goes_to_private_diagnostics_keyed_by_the_reference(
    client: TestClient, diagnostics_dir: Path
) -> None:
    error_id = client.get("/boom/widgets").json()["error_id"]

    path = diagnostics_dir / diagnostics.DIAGNOSTICS_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(diagnostics_dir.stat().st_mode) & 0o077 == 0
    (entry,) = _entries(diagnostics_dir)
    assert entry["error_id"] == error_id
    assert entry["exception"] == "RuntimeError"
    assert entry["method"] == "GET"
    assert entry["route"] == "/boom/{item}"  # the template, never the concrete URL
    assert "widgets" not in json.dumps(entry)
    assert "Traceback (most recent call last)" in entry["trace"]
    assert "in boom" in entry["trace"]  # operators keep the useful frames
    assert OPENAI_KEY not in entry["trace"]  # but secrets never reach even the sink
    assert EMAIL not in entry["trace"]


def test_diagnostics_file_is_tightened_if_it_already_exists_loosely(
    diagnostics_dir: Path,
) -> None:
    diagnostics_dir.mkdir()
    path = diagnostics_dir / diagnostics.DIAGNOSTICS_FILENAME
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)

    assert diagnostics.record_exception("err_0123456789abcdef", ValueError("x"))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_general_log_has_the_reference_but_no_message_or_traceback(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        error_id = client.get("/boom/x").json()["error_id"]

    assert error_id in caplog.text
    assert "RuntimeError" in caplog.text
    assert "/boom/{item}" in caplog.text
    for leaked in (OPENAI_KEY, EMAIL, "Traceback", "upstream"):
        assert leaked not in caplog.text


def test_unwritable_sink_falls_back_to_redacted_server_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("DIAGNOSTICS_DIR", str(blocker / "diagnostics"))

    with caplog.at_level(logging.ERROR):
        response = TestClient(_build_app()).get("/api/boom")

    assert response.status_code == 500
    assert OPENAI_KEY not in response.text
    assert "Diagnostics sink unavailable" in caplog.text
    assert response.json()["error_id"] in caplog.text
    assert OPENAI_KEY not in caplog.text and EMAIL not in caplog.text


def test_diagnostics_default_to_a_directory_beside_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DIAGNOSTICS_DIR", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "workout_agent.db"))

    assert diagnostics.diagnostics_path() == (
        tmp_path / "data" / "diagnostics" / diagnostics.DIAGNOSTICS_FILENAME
    ).resolve()


def test_diagnostics_file_rotates_instead_of_growing_without_bound(
    diagnostics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "MAX_FILE_BYTES", 400)

    for _ in range(6):
        diagnostics.record_exception(diagnostics.new_error_id(), RuntimeError("x" * 120))

    assert (diagnostics_dir / f"{diagnostics.DIAGNOSTICS_FILENAME}.1").exists()
    sizes = [path.stat().st_size for path in diagnostics_dir.iterdir()]
    assert max(sizes) < 3000


def test_trace_is_bounded(diagnostics_dir: Path) -> None:
    def deep(level: int) -> None:
        if level:
            deep(level - 1)
        raise RuntimeError("y" * 200_000)

    try:
        deep(3)
    except RuntimeError as exc:
        diagnostics.record_exception("err_0123456789abcdef", exc)

    (entry,) = _entries(diagnostics_dir)
    assert len(entry["trace"]) <= diagnostics.MAX_TRACE_CHARS


# ---------------------------------------------------------------------------
# ASGI containment semantics
# ---------------------------------------------------------------------------


def test_exception_after_the_response_started_is_recorded_not_replaced(
    diagnostics_dir: Path,
) -> None:
    sent: list[dict[str, Any]] = []

    async def failing_stream(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError(SECRET_MESSAGE)

    async def collect(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "path": "/stream", "headers": []}
    asyncio.run(OpaqueErrorMiddleware(failing_stream)(scope, receive, collect))

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    (entry,) = _entries(diagnostics_dir)
    assert entry["exception"] == "RuntimeError"
    assert OPENAI_KEY not in json.dumps(entry)


def test_non_http_scopes_pass_through_untouched() -> None:
    seen: list[str] = []

    async def lifespan_app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async def receive() -> dict[str, Any]:
        return {}

    async def send(message: dict[str, Any]) -> None:
        return None

    asyncio.run(OpaqueErrorMiddleware(lifespan_app)({"type": "lifespan"}, receive, send))

    assert seen == ["lifespan"]


def test_install_is_idempotent() -> None:
    app = FastAPI()

    install_error_boundary(app)
    install_error_boundary(app)

    assert sum(item.cls is OpaqueErrorMiddleware for item in app.user_middleware) == 1


# ---------------------------------------------------------------------------
# The real application and the production entrypoint
# ---------------------------------------------------------------------------


def _session_cookie(user_id: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user": "user1@example.com", "user_id": user_id}).encode()
    )
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


def _prepend_route(app: FastAPI, path: str, endpoint: Any) -> None:
    # The SPA catch-all is registered last in the real app, so test routes must
    # be matched ahead of it.
    app.router.routes.insert(0, APIRoute(path, endpoint, methods=["GET"]))


@pytest.fixture()
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diagnostics_dir: Path) -> Any:
    db_file = str(tmp_path / "web.db")
    init_db(db_file)
    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "error-boundary-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "error-boundary-client-secret")
    monkeypatch.setenv("WEB_AUTH_SECRET", SESSION_SECRET)

    import webapp.app as app_module

    importlib.reload(app_module)
    user = get_or_create_user("user1@example.com", "User One", db_path=db_file)
    monkeypatch.setattr(app_module, "_check_api_auth", lambda request: user["id"])
    return app_module, user["id"]


def test_real_application_installs_the_boundary_outermost(web: Any) -> None:
    app_module, _user_id = web

    assert app_module.app.user_middleware[0].cls is OpaqueErrorMiddleware


def test_real_application_hides_failures_behind_an_opaque_reference(
    web: Any, diagnostics_dir: Path
) -> None:
    app_module, user_id = web

    def explode() -> None:
        raise RuntimeError(f"database exploded: refresh_token={OPENAI_KEY}")

    _prepend_route(app_module.app, "/api/__explode", explode)
    client = TestClient(app_module.app)
    client.cookies.set("session", _session_cookie(user_id))

    response = client.get("/api/__explode")

    assert response.status_code == 500
    assert OPENAI_KEY not in response.text and "database exploded" not in response.text
    assert _entries(diagnostics_dir)[0]["error_id"] == response.json()["error_id"]


def test_reflected_credentials_are_scrubbed_from_a_real_error_response(web: Any) -> None:
    app_module, user_id = web
    client = TestClient(app_module.app)
    client.cookies.set("session", _session_cookie(user_id))

    # A user pasting their key into the wrong field is a realistic mistake; the
    # 400 used to echo the "provider" value straight back.
    response = client.post(
        "/api/settings/key", json={"provider": OPENAI_KEY, "api_key": "unused"}
    )

    assert response.status_code == 400
    assert OPENAI_KEY not in response.text
    assert response.json()["detail"].startswith("Unknown provider")


def test_rejected_sign_in_neither_shows_nor_logs_the_address(
    web: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app_module, _user_id = web

    class _FakeGoogle:
        async def authorize_access_token(self, request: Any) -> dict[str, Any]:
            return {"userinfo": {"email": EMAIL, "name": "Synthetic Person"}}

    monkeypatch.setattr(app_module, "ALLOWED_EMAILS", ["someone.else@example.com"])
    monkeypatch.setattr(app_module, "oauth", types.SimpleNamespace(google=_FakeGoogle()))

    with caplog.at_level(logging.DEBUG):
        response = TestClient(app_module.app).get(
            f"/auth?code={GOOGLE_CODE}&state=synthetic-state", follow_redirects=False
        )

    assert response.status_code == 403
    assert EMAIL not in response.text and "Synthetic Person" not in response.text
    assert EMAIL not in caplog.text
    assert "allow-list" in caplog.text


def test_failed_token_exchange_logs_the_failure_class_only(
    web: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app_module, _user_id = web

    class _BrokenGoogle:
        async def authorize_access_token(self, request: Any) -> dict[str, Any]:
            raise LookupError(f"invalid_grant code={GOOGLE_CODE} for {EMAIL}")

    monkeypatch.setattr(app_module, "oauth", types.SimpleNamespace(google=_BrokenGoogle()))

    with caplog.at_level(logging.DEBUG):
        response = TestClient(app_module.app).get(
            f"/auth?code={GOOGLE_CODE}&state=s", follow_redirects=False
        )

    assert response.status_code in {302, 303, 307}
    assert "LookupError" in caplog.text
    assert GOOGLE_CODE not in caplog.text and EMAIL not in caplog.text


def test_production_entrypoint_wraps_opaque_errors_in_security_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diagnostics_dir: Path
) -> None:
    db_file = str(tmp_path / "entrypoint.db")
    init_db(db_file)
    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    for name in (
        "WEB_PUBLIC_URL",
        "WEB_AUTH_SECRET",
        "WEB_GOOGLE_CLIENT_ID",
        "WEB_GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    monkeypatch.delitem(sys.modules, "webapp.secure_app", raising=False)
    secure = importlib.import_module("webapp.secure_app")
    try:

        def explode() -> None:
            raise RuntimeError(SECRET_MESSAGE)

        _prepend_route(secure.application, "/api/__explode", explode)
        with TestClient(secure.app) as client:
            response = client.get("/api/__explode")
    finally:
        sys.modules.pop("webapp.secure_app", None)

    assert response.status_code == 500
    assert ERROR_ID.fullmatch(response.json()["error_id"])
    assert OPENAI_KEY not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers
    assert "no-store" in response.headers["cache-control"]
    assert _entries(diagnostics_dir)[0]["error_id"] == response.json()["error_id"]
