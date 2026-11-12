"""Opaque error responses and protected diagnostics (issue #766).

Every secret is synthetic and assembled at run time from short pieces.
"""

from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException
from webapp.error_responses import (
    DIAGNOSTICS_LOGGER_NAME,
    INTERNAL_ERROR_DETAIL,
    ErrorBoundaryMiddleware,
    http_exception_handler,
    install_error_handling,
    new_error_id,
    validation_exception_handler,
)
from webapp.request_correlation import REQUEST_ID_HEADER, RequestCorrelationMiddleware

_ERROR_ID = re.compile(r"err_[0-9a-f]{16}")
_REQUEST_ID_VALUE = re.compile(r"[0-9a-f]{32}")
_BODY = "Zq9" * 8
DB_PASSWORD = "hunter" + "2" * 8
BEARER = "opaque-" + "bearer-" + _BODY
OPENAI_KEY = "sk-" + "proj-" + _BODY
CONNECTOR_TOKEN = "connector-" + "token-" + _BODY
PRIVATE_NOTE = "my left knee clicks when I squat"


class _Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    note: str | None = None


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/boom")
    def boom() -> None:
        raise RuntimeError(
            f"connect failed password={DB_PASSWORD} Authorization: Bearer {BEARER}"
        )

    @app.get("/page")
    def page() -> None:
        raise RuntimeError(f"template exploded api_key={OPENAI_KEY}")

    @app.get("/api/unavailable")
    def unavailable() -> None:
        try:
            raise ValueError(f"upstream refused token={CONNECTOR_TOKEN}")
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Provider unavailable (token={CONNECTOR_TOKEN})",
            ) from exc

    @app.get("/api/missing")
    def missing() -> None:
        raise HTTPException(status_code=404, detail="Record not found")

    @app.get("/api/echo")
    def echo(guess: str) -> None:
        raise HTTPException(status_code=400, detail=f"Unknown AI provider '{guess}'.")

    @app.get("/api/conflict")
    def conflict() -> None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Source changed (api_key={OPENAI_KEY})",
                "conflict": {"routine_id": "r-1", "violations": ["rep range"]},
            },
        )

    @app.get("/api/challenge")
    def challenge() -> None:
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )

    @app.get("/api/not-modified")
    def not_modified() -> None:
        raise HTTPException(status_code=304)

    @app.post("/api/submit")
    def submit(body: _Submission) -> dict[str, int]:
        return {"count": body.count}

    @app.get("/api/stream")
    def stream() -> StreamingResponse:
        def chunks() -> Iterator[str]:
            yield "first chunk"
            raise RuntimeError(f"stream died token={CONNECTOR_TOKEN}")

        return StreamingResponse(chunks(), media_type="text/plain")

    install_error_handling(app)
    app.add_middleware(RequestCorrelationMiddleware)
    return app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def _diagnostics(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == DIAGNOSTICS_LOGGER_NAME]


# ---------------------------------------------------------------------------
# Unhandled exceptions
# ---------------------------------------------------------------------------


def test_unhandled_exception_returns_an_opaque_body_with_an_error_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    response = client.get("/api/boom")

    assert response.status_code == 500
    body = response.json()
    assert set(body) == {"detail", "error_id"}
    assert body["detail"] == INTERNAL_ERROR_DETAIL
    assert _ERROR_ID.fullmatch(body["error_id"])
    for leaked in (DB_PASSWORD, BEARER, "RuntimeError", "connect failed", "Traceback", "boom"):
        assert leaked not in response.text, leaked
    assert "no-store" in response.headers["cache-control"]
    assert response.headers[REQUEST_ID_HEADER]


def test_the_trace_stays_in_the_diagnostics_log_redacted_and_correlated(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    response = client.get("/api/boom")

    (record,) = _diagnostics(caplog)
    message = record.getMessage()
    assert f"error_id={response.json()['error_id']}" in message
    assert f"request_id={response.headers[REQUEST_ID_HEADER]}" in message
    assert "status=500 method=GET path=/api/boom exception=RuntimeError" in message
    # The operator gets the traceback ...
    assert "Traceback (most recent call last)" in caplog.text
    assert "RuntimeError: connect failed" in caplog.text
    # ... but the secrets inside the exception message never reach any log.
    for secret in (DB_PASSWORD, BEARER):
        assert secret not in caplog.text, secret


def test_each_failure_gets_its_own_unguessable_identifier(client: TestClient) -> None:
    identifiers = {client.get("/api/boom").json()["error_id"] for _ in range(25)}

    assert len(identifiers) == 25
    assert all(_ERROR_ID.fullmatch(identifier) for identifier in identifiers)
    assert _ERROR_ID.fullmatch(new_error_id())


def test_browser_navigation_gets_a_plain_error_page_with_the_reference(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    response = client.get("/page", headers={"Accept": "text/html,application/xhtml+xml"})

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/html")
    (record,) = _diagnostics(caplog)
    error_id = re.search(r"error_id=(err_[0-9a-f]{16})", record.getMessage())
    assert error_id is not None
    assert error_id.group(1) in response.text
    assert "<script" not in response.text.lower()
    for leaked in (OPENAI_KEY, "template exploded", "RuntimeError"):
        assert leaked not in response.text


def test_api_paths_stay_json_even_when_the_client_prefers_html(client: TestClient) -> None:
    response = client.get("/api/boom", headers={"Accept": "text/html"})

    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == INTERNAL_ERROR_DETAIL


def test_a_hostile_path_cannot_forge_log_lines(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def failing_app(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    hostile = "/x%0AERROR forged line%0D%0Aserver_error error_id=err_0000000000000000"
    hostile_client = TestClient(
        ErrorBoundaryMiddleware(failing_app), raise_server_exceptions=False
    )
    response = hostile_client.get(hostile)

    assert response.status_code == 500
    (record,) = _diagnostics(caplog)
    message = record.getMessage()
    assert "\n" not in message and "\r" not in message
    assert "\\n" in message  # the newline is escaped, not interpreted
    assert message.count("error_id=") == 2  # the real one and the escaped, inert text
    assert f"error_id={response.json()['error_id']} " in message


def test_failure_after_the_response_started_is_not_replaced_and_still_correlated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with TestClient(_app(), raise_server_exceptions=True) as raising, pytest.raises(
        RuntimeError, match="stream died"
    ):
        raising.get("/api/stream")

    (record,) = _diagnostics(caplog)
    assert "exception=RuntimeError" in record.getMessage()
    assert record.exc_info is None  # the server logs the trace; no duplicate here
    assert CONNECTOR_TOKEN not in caplog.text


def test_errors_from_the_outer_layers_carry_security_headers_and_a_request_id() -> None:
    from webapp.security_headers import SecurityHeadersMiddleware

    async def failing_layer(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError(f"proxy layer failed password={DB_PASSWORD}")

    # The production ordering: security headers outermost, the boundary just inside.
    stack = SecurityHeadersMiddleware(ErrorBoundaryMiddleware(failing_layer))
    client = TestClient(stack, raise_server_exceptions=False)

    api = client.get("/api/anything")
    page = client.get("/dashboard", headers={"Accept": "text/html"})

    for response in (api, page):
        assert response.status_code == 500
        assert "content-security-policy" in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert _REQUEST_ID_VALUE.fullmatch(response.headers[REQUEST_ID_HEADER])
        assert DB_PASSWORD not in response.text
        assert "RuntimeError" not in response.text
    assert _ERROR_ID.fullmatch(api.json()["error_id"])
    assert _ERROR_ID.search(page.text)


# ---------------------------------------------------------------------------
# Handled errors
# ---------------------------------------------------------------------------


def test_client_errors_keep_their_shape_and_get_no_error_id(client: TestClient) -> None:
    response = client.get("/api/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Record not found"}
    unknown = client.get("/api/does-not-exist")
    assert unknown.status_code == 404
    assert set(unknown.json()) == {"detail"}


def test_server_errors_keep_their_curated_detail_but_scrub_secrets_and_get_an_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    response = client.get("/api/unavailable")

    assert response.status_code == 503
    body = response.json()
    assert body["detail"].startswith("Provider unavailable (token=")
    assert CONNECTOR_TOKEN not in response.text
    assert _ERROR_ID.fullmatch(body["error_id"])
    (record,) = _diagnostics(caplog)
    assert f"error_id={body['error_id']}" in record.getMessage()
    assert "status=503" in record.getMessage()
    # The ``raise ... from`` cause used to be dropped silently; it is now kept
    # (redacted) where only an operator can read it.
    assert "ValueError: upstream refused" in caplog.text
    assert CONNECTOR_TOKEN not in caplog.text


def test_structured_details_keep_their_keys_and_scrub_only_string_values(
    client: TestClient,
) -> None:
    response = client.get("/api/conflict")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["conflict"] == {"routine_id": "r-1", "violations": ["rep range"]}
    assert detail["message"].startswith("Source changed (api_key=")
    assert OPENAI_KEY not in response.text
    assert "error_id" not in response.json()


def test_headers_and_bodiless_statuses_survive_the_handler(client: TestClient) -> None:
    challenge = client.get("/api/challenge")
    assert challenge.status_code == 401
    assert challenge.headers["www-authenticate"] == "Bearer"
    assert challenge.json() == {"detail": "Unauthorized"}

    not_modified = client.get("/api/not-modified")
    assert not_modified.status_code == 304
    assert not_modified.content == b""


def test_responses_never_consult_the_shared_secret_registry(client: TestClient) -> None:
    """Masking a registered value in a response would be an oracle across tenants."""
    from log_redaction import forget_registered_secrets, register_secret

    guess = "guess-" + "that-is-someones-live-credential-" + "12345"
    register_secret(guess)
    try:
        echoed = client.get("/api/echo", params={"guess": guess})
        invalid = client.post("/api/submit", json={"count": 1, guess: "x"})
    finally:
        forget_registered_secrets()

    assert echoed.status_code == 400 and guess in echoed.json()["detail"]
    assert "[REDACTED" not in echoed.text
    assert invalid.status_code == 422 and guess in invalid.text
    assert "[REDACTED" not in invalid.text


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_validation_errors_report_fields_without_echoing_submitted_values(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/submit",
        json={
            "count": f"not-a-number {OPENAI_KEY}",
            "note": PRIVATE_NOTE,
            OPENAI_KEY: "unexpected field named like a key",
        },
    )

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors and all(set(error) == {"type", "loc", "msg"} for error in errors)
    assert {tuple(error["loc"][:2]) for error in errors} >= {("body", "count")}
    for leaked in (OPENAI_KEY, PRIVATE_NOTE, "not-a-number", '"input"', '"ctx"', '"url"'):
        assert leaked not in response.text, leaked


def test_a_valid_request_is_unaffected(client: TestClient) -> None:
    assert client.post("/api/submit", json={"count": 3}).json() == {"count": 3}


# ---------------------------------------------------------------------------
# Wiring in the real application
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from database import init_db

    db_path = str(tmp_path / "error-responses.db")
    init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    import webapp.app as module

    return importlib.reload(module)


def test_the_real_application_installs_the_handlers_and_boundary_inside_correlation(
    app_module: Any,
) -> None:
    app = app_module.app

    assert app.exception_handlers[StarletteHTTPException] is http_exception_handler
    assert app.exception_handlers[RequestValidationError] is validation_exception_handler
    layers = [middleware.cls for middleware in app.user_middleware]  # outermost first
    assert layers.index(RequestCorrelationMiddleware) < layers.index(ErrorBoundaryMiddleware)
    session_layers = [
        index for index, layer in enumerate(layers) if layer.__name__ == "HardenedSessionMiddleware"
    ]
    assert session_layers and layers.index(ErrorBoundaryMiddleware) < session_layers[0]


def test_the_production_entrypoint_wraps_the_outer_layers_in_an_error_boundary() -> None:
    import webapp.secure_app as secure

    layer: Any = secure.app
    seen = []
    while layer is not None:
        seen.append(type(layer).__name__)
        layer = getattr(layer, "app", None)

    assert seen.index("SecurityHeadersMiddleware") < seen.index("ErrorBoundaryMiddleware")
    assert seen.index("ErrorBoundaryMiddleware") < seen.index("OperationalHealthMiddleware")
    assert seen.index("OperationalHealthMiddleware") < seen.index("ProxySecurityMiddleware")


def test_an_unhandled_failure_in_the_real_application_is_opaque(
    app_module: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def exploding(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(f"settings store failed password={DB_PASSWORD} api_key={OPENAI_KEY}")

    monkeypatch.setattr(app_module, "get_user_settings", exploding)

    with TestClient(app_module.app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/api/settings/display")

    assert response.status_code == 500
    assert set(response.json()) == {"detail", "error_id"}
    assert response.headers[REQUEST_ID_HEADER]
    for leaked in (DB_PASSWORD, OPENAI_KEY, "settings store failed", "RuntimeError"):
        assert leaked not in response.text, leaked
    for secret in (DB_PASSWORD, OPENAI_KEY):
        assert secret not in caplog.text, secret
    (record,) = _diagnostics(caplog)
    assert f"error_id={response.json()['error_id']}" in record.getMessage()


def test_the_real_settings_route_does_not_echo_rejected_input(app_module: Any) -> None:
    app = FastAPI()
    app.add_api_route(
        "/api/settings/display", app_module.save_display_settings, methods=["POST"]
    )
    install_error_handling(app)

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.post(
            "/api/settings/display", json={"timezone": 5, "diagnosis": PRIVATE_NOTE}
        )

    assert response.status_code == 422
    assert PRIVATE_NOTE not in response.text
    assert '"input"' not in response.text
