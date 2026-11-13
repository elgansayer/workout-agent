"""Negative and positive HTTP tests for the administrative permission boundary.

Every account and payload is synthetic. The point of these tests is that an
ordinary user (and a support user where the action is destructive) is refused by
the server, not merely by a missing UI control.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from accounts import AccountRole, set_account_role
from database import get_account_state, get_or_create_user, get_user_by_id, init_db
from starlette.testclient import TestClient

_SECRET = "admin-api-test-secret-that-is-long-enough"


class AdminWeb(NamedTuple):
    client: TestClient
    module: ModuleType
    db: str
    ids: dict[str, str]
    login: Callable[[str], Any]


@pytest.fixture()
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AdminWeb:
    db = str(tmp_path / "admin.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "admin-test-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "admin-test-client-secret")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("AUDIT_LOG_HMAC_KEY", raising=False)
    init_db(db)

    ids: dict[str, str] = {}
    for role in AccountRole:
        email = f"{role.value}@example.com"
        user = get_or_create_user(email, db_path=db)
        set_account_role(user["id"], role, db)
        ids[role.value] = user["id"]

    import webapp.app as app_module

    importlib.reload(app_module)
    client = TestClient(app_module.app)

    def login(role: str) -> Any:
        claims = {
            "sub": f"synthetic-google-subject-{role}",
            "email": f"{role}@example.com",
            "email_verified": True,
            "name": f"Synthetic {role.title()}",
        }

        async def authorize_access_token(_request: Any) -> dict[str, Any]:
            return {"userinfo": claims}

        monkeypatch.setattr(
            app_module.oauth.google, "authorize_access_token", authorize_access_token
        )
        client.cookies.clear()
        response = client.get("/auth", follow_redirects=False)
        assert response.status_code in {302, 303, 307}
        return response

    return AdminWeb(client, app_module, db, ids, login)


def _as(web: AdminWeb, role: str) -> TestClient:
    web.login(role)
    return web.client


@pytest.mark.parametrize("role", ["user", "support"])
def test_ordinary_and_support_accounts_cannot_list_accounts(
    web: AdminWeb, role: str
) -> None:
    response = _as(web, role).get("/api/admin/accounts")

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden."}


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_account_managers_can_list_accounts(web: AdminWeb, role: str) -> None:
    response = _as(web, role).get("/api/admin/accounts")

    assert response.status_code == 200
    payload = response.json()["accounts"]
    assert {item["role"] for item in payload} == {
        "owner",
        "admin",
        "support",
        "user",
    }
    cache_control = response.headers["cache-control"]
    assert "private" in cache_control and "no-store" in cache_control


def test_cross_tenant_support_data_is_denied_to_ordinary_users(
    web: AdminWeb,
) -> None:
    response = _as(web, "user").get(
        f"/api/admin/accounts/{web.ids['owner']}/support"
    )

    assert response.status_code == 403


@pytest.mark.parametrize("role", ["support", "admin", "owner"])
def test_support_access_is_available_to_support_and_above(
    web: AdminWeb, role: str
) -> None:
    response = _as(web, role).get(
        f"/api/admin/accounts/{web.ids['user']}/support"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["account"]["id"] == web.ids["user"]
    assert "secret" not in json.dumps(payload).lower()


def test_connector_diagnostics_require_the_diagnostics_permission(web: AdminWeb) -> None:
    target = web.ids["user"]
    denied = _as(web, "user").get(f"/api/admin/accounts/{target}/diagnostics")
    assert denied.status_code == 403

    allowed = _as(web, "support").get(f"/api/admin/accounts/{target}/diagnostics")
    assert allowed.status_code == 200
    assert allowed.json()["components"]["database"]["status"] == "ok"


def test_export_requires_data_export_and_never_returns_credentials(
    web: AdminWeb,
) -> None:
    target = web.ids["user"]
    assert _as(web, "support").get(
        f"/api/admin/accounts/{target}/export"
    ).status_code == 403

    with sqlite3.connect(web.db) as conn:
        conn.execute(
            "INSERT INTO user_api_keys "
            "(user_id, provider, api_key, created_at, updated_at) "
            "VALUES (?, 'gemini', 'never-export-this-secret', 'now', 'now')",
            (target,),
        )
    response = _as(web, "admin").get(f"/api/admin/accounts/{target}/export")

    assert response.status_code == 200
    body = response.text
    assert "never-export-this-secret" not in body
    payload = response.json()
    assert payload["account"]["id"] == target
    assert payload["record_counts"]["user_api_keys"] == 1


def test_a_role_assignment_is_owner_only(web: AdminWeb) -> None:
    target = web.ids["user"]
    assert _as(web, "admin").post(
        f"/api/admin/accounts/{target}/role", json={"role": "support"}
    ).status_code == 403
    assert _as(web, "support").post(
        f"/api/admin/accounts/{target}/role", json={"role": "admin"}
    ).status_code == 403

    response = _as(web, "owner").post(
        f"/api/admin/accounts/{target}/role", json={"role": "support"}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "support"


def test_an_admin_cannot_escalate_itself_to_owner(web: AdminWeb) -> None:
    response = _as(web, "admin").post(
        f"/api/admin/accounts/{web.ids['admin']}/role", json={"role": "owner"}
    )

    assert response.status_code == 403
    assert get_user_by_id(web.ids["admin"], web.db)["role"] == "admin"


def test_the_last_active_owner_cannot_be_demoted(web: AdminWeb) -> None:
    response = _as(web, "owner").post(
        f"/api/admin/accounts/{web.ids['owner']}/role", json={"role": "admin"}
    )

    assert response.status_code == 409


def test_an_admin_can_suspend_a_lower_role_but_not_an_owner(web: AdminWeb) -> None:
    admin = _as(web, "admin")
    assert admin.post(
        f"/api/admin/accounts/{web.ids['owner']}/status",
        json={"status": "suspended"},
    ).status_code == 403
    response = admin.post(
        f"/api/admin/accounts/{web.ids['user']}/status",
        json={"status": "suspended"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "suspended"


def test_ordinary_users_cannot_delete_another_account(web: AdminWeb) -> None:
    response = _as(web, "user").delete(
        f"/api/admin/accounts/{web.ids['support']}"
    )

    assert response.status_code == 403


def test_data_deletion_tombstones_a_lower_role_and_revokes_sessions(
    web: AdminWeb,
) -> None:
    target = web.ids["user"]
    before = get_account_state(target, web.db)
    assert before is not None

    response = _as(web, "admin").delete(f"/api/admin/accounts/{target}")

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    after = get_account_state(target, web.db)
    assert after is not None
    assert after["session_epoch"] == before["session_epoch"] + 1


def test_configuration_is_owner_only_and_non_secret(web: AdminWeb) -> None:
    assert _as(web, "admin").get("/api/admin/config").status_code == 403

    response = _as(web, "owner").get("/api/admin/config")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "app_env",
        "allowlist_configured",
        "allowlist_entries",
        "invite_only",
        "anonymous_web",
    }
    assert "allowlist" not in json.dumps(payload).lower().replace(
        "allowlist_configured", ""
    ).replace("allowlist_entries", "")


def test_me_exposes_the_actor_role_and_permissions_for_ui_gating(
    web: AdminWeb,
) -> None:
    user_me = _as(web, "user").get("/api/me/permissions")
    assert user_me.status_code == 200
    assert user_me.json()["role"] == "user"
    assert user_me.json()["permissions"] == []

    owner_me = _as(web, "owner").get("/api/me/permissions")
    assert owner_me.status_code == 200
    assert owner_me.json()["role"] == "owner"
    assert "configuration.management" in owner_me.json()["permissions"]
