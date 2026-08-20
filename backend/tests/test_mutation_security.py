from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from webapp.mutation_security import MutationSecurityMiddleware

_SECRET = "mutation-security-test-secret-that-is-long-enough"
_MUTATION_PATHS = (
    "/api/settings/key",  # credentials
    "/api/profile",  # profile
    "/api/programmes/activate",  # programmes
    "/api/checkins",  # check-ins
    "/api/notifications/42/read",  # notifications
    "/api/connectors/hevy/disconnect",  # connector state
)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/test/session/{user_id}")
    def login(user_id: str, request: Request) -> dict[str, str]:
        request.session["user"] = f"{user_id}@example.test"
        request.session["user_id"] = user_id
        return {"status": "ok"}

    @app.get("/test/incomplete-session/{user_id}")
    def incomplete_login(user_id: str, request: Request) -> dict[str, str]:
        request.session.clear()
        request.session["user_id"] = user_id
        return {"status": "ok"}

    def add_mutation(path: str) -> None:
        async def mutate(request: Request) -> dict[str, str]:
            return {
                "owner": request.state.mutation_user_id,
                "method": request.method,
            }

        app.add_api_route(path, mutate, methods=["POST"])

    for path in _MUTATION_PATHS:
        add_mutation(path)

    @app.api_route("/api/action", methods=["PUT", "PATCH", "DELETE"])
    async def generic_action(request: Request) -> dict[str, str]:
        return {"owner": request.state.mutation_user_id, "method": request.method}

    @app.post("/api/upload")
    async def upload(request: Request) -> dict[str, str]:
        # Reading the body proves the guard's defence-in-depth inspection does
        # not consume a downstream form/upload request.
        await request.body()
        return {"owner": request.state.mutation_user_id}

    @app.get("/public-read")
    def public_read() -> dict[str, str]:
        return {"status": "ok"}

    # webapp's bootstrap patches FastAPI.add_middleware so adding SessionMiddleware
    # automatically inserts the mutation guard immediately inside it.
    app.add_middleware(SessionMiddleware, secret_key=_SECRET)
    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_build_app())


def _login(client: TestClient, user_id: str = "user-a") -> None:
    response = client.get(f"/test/session/{user_id}")
    assert response.status_code == 200


@pytest.mark.parametrize("path", _MUTATION_PATHS)
def test_every_product_mutation_category_rejects_anonymous_users(
    client: TestClient,
    path: str,
) -> None:
    response = client.post(path, json={})
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


@pytest.mark.parametrize("method", ("put", "patch", "delete"))
def test_all_state_changing_http_methods_fail_closed(
    client: TestClient,
    method: str,
) -> None:
    response = getattr(client, method)("/api/action", json={})
    assert response.status_code == 401


def test_incomplete_session_is_not_an_authenticated_mutation_identity(
    client: TestClient,
) -> None:
    assert client.get("/test/incomplete-session/user-a").status_code == 200
    response = client.post("/api/profile", json={})
    assert response.status_code == 401


@pytest.mark.parametrize("path", _MUTATION_PATHS)
def test_cross_user_json_owner_claim_is_non_enumerating(
    client: TestClient,
    path: str,
) -> None:
    _login(client)
    response = client.post(path, json={"user_id": "user-b"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


def test_nested_cross_user_owner_claim_is_rejected(client: TestClient) -> None:
    _login(client)
    response = client.post(
        "/api/programmes/activate",
        json={"programme": {"metadata": {"owner_id": "user-b"}}},
    )
    assert response.status_code == 404


def test_matching_owner_claim_never_replaces_session_owner(client: TestClient) -> None:
    _login(client)
    response = client.post("/api/settings/key", json={"user_id": "user-a"})
    assert response.status_code == 200
    assert response.json()["owner"] == "user-a"


@pytest.mark.parametrize(
    ("kwargs", "expected_status"),
    (
        ({"params": {"owner_id": "user-b"}}, 404),
        ({"headers": {"X-Account-Id": "user-b"}}, 404),
        ({"data": {"owner_id": "user-b"}}, 404),
        ({"params": {"owner_id": "user-a"}}, 200),
        ({"headers": {"X-Account-Id": "user-a"}}, 200),
        ({"data": {"owner_id": "user-a"}}, 200),
    ),
)
def test_query_header_and_form_owner_claims_are_bound_to_session(
    client: TestClient,
    kwargs: dict[str, object],
    expected_status: int,
) -> None:
    _login(client)
    response = client.post("/api/profile", **kwargs)
    assert response.status_code == expected_status


def test_multipart_upload_rejects_cross_user_owner_claim(client: TestClient) -> None:
    _login(client)
    response = client.post(
        "/api/upload",
        data={"user_id": "user-b"},
        files={"file": ("checkin.txt", b"synthetic check-in", "text/plain")},
    )
    assert response.status_code == 404


def test_authenticated_upload_is_replayed_to_downstream_route(client: TestClient) -> None:
    _login(client)
    response = client.post(
        "/api/upload",
        data={"user_id": "user-a"},
        files={"file": ("checkin.txt", b"synthetic check-in", "text/plain")},
    )
    assert response.status_code == 200
    assert response.json() == {"owner": "user-a"}


def test_safe_get_requests_are_unchanged(client: TestClient) -> None:
    response = client.get("/public-read")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_session_middleware_installation_injects_guard_inside_session() -> None:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=_SECRET)

    middleware_classes = [middleware.cls for middleware in app.user_middleware]
    assert middleware_classes[:2] == [SessionMiddleware, MutationSecurityMiddleware]


_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _representative_path(path: str) -> str:
    def replace(match: re.Match[str]) -> str:
        _name, _, converter = match.group(1).partition(":")
        return "1" if converter in {"int", "float"} else "test"

    return re.sub(r"\{([^{}]+)\}", replace, path)


@pytest.fixture()
def actual_anonymous_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load the real app in its explicit read-only anonymous test mode."""

    repo_root = Path(__file__).resolve().parents[2]
    (repo_root / "frontend" / "dist" / "frontend" / "browser").mkdir(
        parents=True,
        exist_ok=True,
    )
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "mutation-inventory.db"))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    monkeypatch.delenv("WEB_AUTH_SECRET", raising=False)
    monkeypatch.delenv("WEB_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("WEB_GOOGLE_CLIENT_SECRET", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    return app_module.app


def test_real_application_has_no_anonymous_mutation_escape_hatch(
    actual_anonymous_app: Any,
) -> None:
    """Even the deliberate anonymous dev mode is strictly read-only."""

    routes = [route for route in actual_anonymous_app.routes if isinstance(route, APIRoute)]
    mutation_routes = [
        (route, method)
        for route in routes
        for method in sorted((route.methods or set()) & _MUTATION_METHODS)
    ]
    assert mutation_routes, "Expected the production app to expose mutation routes"

    discovered_paths = {route.path for route, _method in mutation_routes}
    assert {
        "/api/settings/key",
        "/api/settings/preferences",
        "/api/programmes/activate",
        "/api/notifications/{notification_id}/read",
        "/google-health/disconnect",
    } <= discovered_paths

    failures: list[str] = []
    with TestClient(actual_anonymous_app) as client:
        for route, method in mutation_routes:
            response = client.request(
                method,
                _representative_path(route.path),
                follow_redirects=False,
            )
            if response.status_code != 401:
                failures.append(
                    f"{method} {route.path} -> {response.status_code}; expected 401"
                )
    assert not failures, "\n".join(failures)
