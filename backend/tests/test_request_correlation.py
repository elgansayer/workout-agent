"""Tests for per-request correlation identifiers."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from webapp.request_correlation import (
    REQUEST_ID_HEADER,
    RequestCorrelationMiddleware,
    get_request_id,
)

_REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/id")
    def read_id(request: Request) -> dict[str, str]:
        # A handler asking twice must observe one stable identifier.
        assert get_request_id(request) == get_request_id(request)
        return {"request_id": get_request_id(request)}

    @app.get("/forbidden")
    def forbidden() -> None:
        raise HTTPException(status_code=403, detail="no")

    app.add_middleware(RequestCorrelationMiddleware)
    return app


def test_every_response_carries_the_id_the_handler_saw() -> None:
    with TestClient(_app()) as client:
        response = client.get("/id")

    header = response.headers[REQUEST_ID_HEADER]
    assert _REQUEST_ID.fullmatch(header)
    assert response.json() == {"request_id": header}


def test_each_request_gets_a_distinct_identifier() -> None:
    with TestClient(_app()) as client:
        identifiers = {client.get("/id").headers[REQUEST_ID_HEADER] for _ in range(20)}

    assert len(identifiers) == 20


def test_client_supplied_identifiers_are_ignored() -> None:
    spoofed = "attacker-chosen-correlation-id"
    with TestClient(_app()) as client:
        response = client.get("/id", headers={REQUEST_ID_HEADER: spoofed})

    assert response.headers[REQUEST_ID_HEADER] != spoofed
    assert response.json()["request_id"] != spoofed
    assert _REQUEST_ID.fullmatch(response.headers[REQUEST_ID_HEADER])


@pytest.mark.parametrize(("path", "status"), [("/forbidden", 403), ("/missing", 404)])
def test_error_responses_are_correlated_too(path: str, status: int) -> None:
    with TestClient(_app()) as client:
        response = client.get(path)

    assert response.status_code == status
    assert _REQUEST_ID.fullmatch(response.headers[REQUEST_ID_HEADER])


def test_a_stable_identifier_is_minted_when_the_middleware_is_absent() -> None:
    app = FastAPI()

    @app.get("/id")
    def read_id(request: Request) -> dict[str, str]:
        first = get_request_id(request)
        return {"first": first, "second": get_request_id(request)}

    with TestClient(app) as client:
        body = client.get("/id").json()

    assert _REQUEST_ID.fullmatch(body["first"]) and body["first"] == body["second"]
    assert REQUEST_ID_HEADER not in client.get("/id").headers
