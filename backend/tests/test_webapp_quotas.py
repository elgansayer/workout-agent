"""HTTP-level coverage of per-account quotas, retry information and admin audit.

These tests drive the real FastAPI routes with signed session cookies, a real
SQLite database and a stubbed AI provider / Hevy / OAuth exchange, so they
verify wiring (every covered route consumes its quota), tenant isolation,
actionable 429 responses and the audited administrator workflow.
"""

from __future__ import annotations

import base64
import importlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from database import get_or_create_user, init_db, save_user_api_key
from hevy_reader import HevyTrainingData
from itsdangerous import TimestampSigner
from quota_admin import set_override
from quotas import ABUSE_DENIAL_THRESHOLD, QuotaConfigError, QuotaKind
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.testclient import TestClient
from webapp.quota_api import client_address

_SECRET = "quota-routes-test-secret-that-is-long-enough"
_TIERS = ("restricted", "standard", "extended")
_PROGRAMME_PAYLOAD = {
    "selected_routine_ids": ["routine-a"],
    "duration_weeks": 12,
    "goal": "hypertrophy",
    "start_date": "2026-09-01",
    "sessions_per_week": 4,
    "experience": "intermediate",
    "max_session_minutes": 90,
    "adaptation_aggressiveness": "balanced",
}


def _policy(per_kind: dict[str, Any] | None = None, **extra: Any) -> str:
    """JSON for QUOTA_POLICY_OVERRIDES applying *per_kind* to every entitlement."""
    payload: dict[str, Any] = dict(extra)
    if per_kind is not None:
        payload["entitlements"] = {tier: per_kind for tier in _TIERS}
    return json.dumps(payload)


def _session_cookie(user_id: str, email: str) -> str:
    payload = base64.b64encode(json.dumps({"user": email, "user_id": user_id}).encode())
    return TimestampSigner(_SECRET).sign(payload).decode()


@dataclass
class Site:
    module: Any
    db: str
    admin: str
    member: str
    other: str

    def client(
        self,
        user_id: str | None = None,
        email: str | None = None,
        *,
        address: str = "203.0.113.7",
        follow_redirects: bool = True,
    ) -> TestClient:
        client = TestClient(
            self.module.app,
            client=(address, 50000),
            follow_redirects=follow_redirects,
        )
        if user_id is not None and email is not None:
            client.cookies.set("session", _session_cookie(user_id, email))
        return client

    def as_member(self) -> TestClient:
        return self.client(self.member, "member@example.com")

    def as_other(self) -> TestClient:
        return self.client(self.other, "other@example.com")

    def as_admin(self) -> TestClient:
        return self.client(self.admin, "admin@example.com")


@pytest.fixture()
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    db = str(tmp_path / "quota-web.db")
    init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "quota-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "quota-client-secret")
    monkeypatch.setenv("QUOTA_ADMIN_EMAILS", "admin@example.com")
    for name in ("QUOTA_POLICY_OVERRIDES", "ALLOWED_EMAILS", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    users = {
        name: get_or_create_user(f"{name}@example.com", name, db_path=db)["id"]
        for name in ("admin", "member", "other")
    }
    return Site(app_module, db, users["admin"], users["member"], users["other"])


class _Provider:
    def __init__(self, reply: str = "Synthetic coach reply.") -> None:
        self.calls = 0
        self.reply = reply

    def generate(self, prompt: Any, *, stream: bool = False) -> Any:
        self.calls += 1
        return iter([self.reply]) if stream else self.reply


def _install_provider(monkeypatch: pytest.MonkeyPatch, reply: str = "Synthetic reply.") -> _Provider:
    provider = _Provider(reply)
    monkeypatch.setattr(
        "webapp.app.resolve_provider", lambda user_id=None, **kwargs: provider
    )
    return provider


def _used(client: TestClient, kind: str, rule: int = 0) -> int:
    snapshot = client.get("/api/quotas").json()
    return int(snapshot["quotas"][kind]["rules"][rule]["used"])


def _assert_actionable_denial(response: Any, kind: str, *, limit: int, window: int) -> int:
    assert response.status_code == 429
    retry = int(response.headers["Retry-After"])
    assert 1 <= retry <= window
    body = response.json()
    assert body["error"] == "quota_exceeded"
    assert body["quota"] == {
        "kind": kind,
        "limit": limit,
        "window_seconds": window,
        "retry_after_seconds": retry,
    }
    assert "Try again in" in body["detail"] and f"limited to {limit}" in body["detail"]
    return retry


# --- AI requests --------------------------------------------------------------


def test_ai_requests_are_limited_per_account_with_actionable_retry_information(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _install_provider(monkeypatch)
    member = site.as_member()

    for _ in range(15):
        assert member.get("/api/rag_search?q=hello").status_code == 200
    denied = member.get("/api/rag_search?q=hello")

    retry = _assert_actionable_denial(denied, "ai_request", limit=15, window=60)
    assert f"Try again in {retry} second" in denied.json()["detail"]
    assert provider.calls == 15  # the refused request never reached the provider
    # Another account is unaffected by this account's exhaustion.
    assert site.as_other().get("/api/rag_search?q=hello").status_code == 200


def test_all_ai_routes_share_one_allowance(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(
        monkeypatch, '{"Deadlift_Projected": 1.0, "Pullups_Projected": 2.0, "Validation": "ok"}'
    )
    monkeypatch.setenv("QUOTA_POLICY_OVERRIDES", _policy({"ai_request": [[3, 60]]}))
    member = site.as_member()

    assert member.get("/api/project_peak").status_code == 200
    assert member.get("/api/xai_reasoning/2026-08-09_Deadlift").status_code == 200
    assert member.get("/api/rag_search?q=hello").status_code == 200

    for path in ("/api/project_peak", "/api/xai_reasoning/2026-08-10_Squat", "/api/rag_search?q=x"):
        assert member.get(path).status_code == 429


def test_only_requests_that_reach_a_provider_are_charged(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = site.as_member()

    # No provider key is configured: the request is refused before any AI call.
    assert member.get("/api/rag_search?q=hello").status_code == 400
    assert _used(member, "ai_request") == 0

    provider = _install_provider(monkeypatch, "Because volume rose.")
    assert member.get("/api/xai_reasoning/nounderscore").json() == {
        "reasoning": "Invalid context ID"
    }
    assert _used(member, "ai_request") == 0

    first = member.get("/api/xai_reasoning/2026-08-09_Deadlift")
    cached = member.get("/api/xai_reasoning/2026-08-09_Deadlift")

    assert first.json() == cached.json() == {"reasoning": "Because volume rose."}
    assert provider.calls == 1
    assert _used(member, "ai_request") == 1


# --- imports, sync frequency, notifications and reports -----------------------


def _empty_training() -> HevyTrainingData:
    return HevyTrainingData(
        username="fixture-user",
        workout_count=0,
        exercise_templates={},
        routines=[],
        recent_workouts=[],
        folders={},
    )


def test_hevy_imports_share_one_allowance_across_the_programme_routes(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_user_api_key(site.member, "hevy", "synthetic-hevy-key", db_path=site.db)
    fetches: list[str] = []
    monkeypatch.setattr(
        "hevy_reader.fetch_user_training",
        lambda key, workout_limit=56: fetches.append(key) or _empty_training(),
    )
    monkeypatch.setenv("QUOTA_POLICY_OVERRIDES", _policy({"import": [[3, 60]]}))
    member = site.as_member()

    for _ in range(3):
        assert member.get("/api/programmes").status_code == 200
    denied = member.get("/api/programmes")
    preview = member.post("/api/programmes/preview", json=_PROGRAMME_PAYLOAD)
    activate = member.post(
        "/api/programmes/activate", json={**_PROGRAMME_PAYLOAD, "preview_token": "x" * 20}
    )

    _assert_actionable_denial(denied, "import", limit=3, window=60)
    assert preview.status_code == 429 and activate.status_code == 429
    assert len(fetches) == 3  # nothing past the limit contacted Hevy
    assert site.as_other().get("/api/programmes").json()["connection"]["state"] == "disconnected"


def test_an_account_without_a_hevy_key_is_not_charged_for_imports(site: Site) -> None:
    member = site.as_member()

    assert member.get("/api/programmes").json()["connection"]["state"] == "disconnected"
    assert member.post("/api/programmes/preview", json=_PROGRAMME_PAYLOAD).status_code == 400
    assert _used(member, "import") == 0


def test_history_sync_frequency_is_limited_per_account(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_user_api_key(site.member, "hevy", "synthetic-hevy-key", db_path=site.db)
    synced: list[str | None] = []
    monkeypatch.setattr(
        "scripts.sync_history.sync_all",
        lambda key, db, user_id=None: synced.append(user_id)
        or {"workouts_found": 3, "processed": 3},
    )
    member = site.as_member()

    assert member.post("/api/settings/sync-history").json()["processed"] == 3
    assert member.post("/api/settings/sync-history").status_code == 200
    denied = member.post("/api/settings/sync-history")

    _assert_actionable_denial(denied, "sync", limit=2, window=300)
    assert synced == [site.member, site.member]
    assert member.get("/api/quotas").json()["quotas"]["sync"]["rules"][0]["remaining"] == 0


def test_missing_hevy_key_does_not_consume_the_sync_allowance(site: Site) -> None:
    member = site.as_member()

    assert member.post("/api/settings/sync-history").status_code == 400
    assert _used(member, "sync") == 0


def test_notification_generation_is_limited_per_account(site: Site) -> None:
    member = site.as_member()

    for _ in range(10):
        assert member.post("/api/notifications/test-coach").status_code == 200
    denied = member.post("/api/notifications/test-coach")

    _assert_actionable_denial(denied, "notification", limit=10, window=3600)
    assert member.get("/api/notifications").json()["unread_count"] == 10
    assert site.as_other().post("/api/notifications/test-coach").status_code == 200


def test_expensive_reports_share_one_allowance(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUOTA_POLICY_OVERRIDES", _policy({"report": [[2, 60]]}))
    member = site.as_member()

    assert member.get("/api/progress").status_code == 200
    assert member.get("/api/stats").status_code == 200
    denied_stats = member.get("/api/stats")
    denied_progress = member.get("/api/progress")

    _assert_actionable_denial(denied_stats, "report", limit=2, window=60)
    assert denied_progress.status_code == 429
    assert site.as_other().get("/api/progress").status_code == 200


def test_a_switched_off_quota_is_forbidden_without_a_retry_time(site: Site) -> None:
    set_override(
        site.admin, site.member, "report", 0, None, "Suspected scraping", site.db
    )

    response = site.as_member().get("/api/progress")

    assert response.status_code == 403
    assert response.json()["error"] == "quota_disabled"
    assert "turned off" in response.json()["detail"]
    assert "Retry-After" not in response.headers


def test_a_time_boxed_switch_off_reports_when_it_ends(site: Site) -> None:
    set_override(site.admin, site.member, "report", 0, 600, "Temporary hold", site.db)

    response = site.as_member().get("/api/progress")

    assert response.status_code == 429
    assert 595 <= int(response.headers["Retry-After"]) <= 600
    assert response.json()["error"] == "quota_disabled"


# --- sign-in ------------------------------------------------------------------


def _stub_oauth(site: Site, monkeypatch: pytest.MonkeyPatch, **replacements: Any) -> None:
    google = site.module.oauth.google
    for name, replacement in replacements.items():
        monkeypatch.setattr(google, name, replacement)


def test_a_sign_in_callback_flood_is_stopped_before_any_token_exchange(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchanges: list[int] = []

    async def failing_exchange(request: Any) -> Any:
        exchanges.append(1)
        raise RuntimeError("invalid authorization code")

    _stub_oauth(site, monkeypatch, authorize_access_token=failing_exchange)
    monkeypatch.setenv("QUOTA_POLICY_OVERRIDES", _policy(login_by_address=[[3, 300]]))
    attacker = site.client(address="203.0.113.50", follow_redirects=False)

    for index in range(3):
        # Rotating a spoofed forwarding header must not buy a new allowance.
        response = attacker.get(
            "/auth?code=guess", headers={"X-Forwarded-For": f"198.51.100.{index}"}
        )
        assert response.status_code == 307 and response.headers["location"] == "/login"
    blocked = attacker.get("/auth?code=guess", headers={"X-Forwarded-For": "198.51.100.99"})

    _assert_actionable_denial(blocked, "login", limit=3, window=300)
    assert len(exchanges) == 3  # no outbound call once throttled
    elsewhere = site.client(address="203.0.113.51", follow_redirects=False)
    assert elsewhere.get("/auth?code=guess").status_code == 307


def test_browser_navigation_receives_a_readable_page_with_retry_information(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_exchange(request: Any) -> Any:
        raise RuntimeError("invalid authorization code")

    _stub_oauth(site, monkeypatch, authorize_access_token=failing_exchange)
    monkeypatch.setenv("QUOTA_POLICY_OVERRIDES", _policy(login_by_address=[[1, 300]]))
    browser = site.client(address="203.0.113.52", follow_redirects=False)
    html_headers = {"Accept": "text/html,application/xhtml+xml"}
    browser.get("/auth", headers=html_headers)

    blocked = browser.get("/auth", headers=html_headers)

    assert blocked.status_code == 429
    assert blocked.headers["content-type"].startswith("text/html")
    assert 1 <= int(blocked.headers["Retry-After"]) <= 300
    assert "Sign-in attempts are limited to 1 per 5 minutes" in blocked.text
    assert "Try again in" in blocked.text


def test_the_sign_in_start_is_throttled_per_address(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def authorize_redirect(request: Any, redirect_uri: str) -> Any:
        return RedirectResponse("https://accounts.example.com/signin")

    _stub_oauth(site, monkeypatch, authorize_redirect=authorize_redirect)
    monkeypatch.setenv("QUOTA_POLICY_OVERRIDES", _policy(login_by_address=[[2, 300]]))
    visitor = site.client(address="203.0.113.53", follow_redirects=False)

    assert visitor.get("/login/google").status_code == 307
    assert visitor.get("/login/google").status_code == 307
    blocked = visitor.get("/login/google")

    _assert_actionable_denial(blocked, "login", limit=2, window=300)
    other = site.client(address="203.0.113.54", follow_redirects=False)
    assert other.get("/login/google").status_code == 307


def test_no_session_is_issued_when_the_account_is_over_its_sign_in_allowance(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exchange(request: Any) -> Any:
        return {"userinfo": {"email": "member@example.com", "name": "member"}}

    _stub_oauth(site, monkeypatch, authorize_access_token=exchange)
    monkeypatch.setenv("QUOTA_POLICY_OVERRIDES", _policy({"login": [[2, 3600]]}))

    for index in range(2):
        browser = site.client(address=f"203.0.113.6{index}", follow_redirects=False)
        signed_in = browser.get("/auth")
        assert signed_in.status_code == 307 and signed_in.headers["location"] == "/"
        assert browser.get("/api/me").status_code == 200

    fresh = site.client(address="203.0.113.70", follow_redirects=False)
    refused = fresh.get("/auth")

    _assert_actionable_denial(refused, "login", limit=2, window=3600)
    assert "session" not in refused.cookies
    assert fresh.get("/api/me").status_code == 401


# --- observability ------------------------------------------------------------


def test_quota_usage_is_visible_only_for_the_signed_in_account(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch)
    member = site.as_member()
    for _ in range(3):
        member.get("/api/rag_search?q=hello")

    mine = member.get("/api/quotas")
    theirs = site.as_other().get("/api/quotas")

    assert mine.status_code == 200
    assert mine.json()["entitlement"] == "standard"
    assert set(mine.json()["quotas"]) == {kind.value for kind in QuotaKind}
    assert mine.json()["quotas"]["ai_request"]["rules"][0]["used"] == 3
    assert theirs.json()["quotas"]["ai_request"]["rules"][0]["used"] == 0
    assert site.other not in mine.text and site.member not in theirs.text
    assert "no-store" in mine.headers["cache-control"]
    assert site.client().get("/api/quotas").status_code == 401


# --- failing closed -----------------------------------------------------------


def test_a_quota_storage_outage_fails_closed(
    site: Site, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = _install_provider(monkeypatch)
    with sqlite3.connect(site.db) as conn:
        conn.execute("DROP TABLE quota_usage")
    member = site.as_member()

    refused = member.get("/api/rag_search?q=hello")
    report = member.get("/api/progress")

    for response in (refused, report):
        assert response.status_code == 503
        assert response.json()["error"] == "quota_unavailable"
        assert response.headers["Retry-After"] == "5"
        assert str(tmp_path) not in response.text and "quota_usage" not in response.text
    assert provider.calls == 0


def test_an_invalid_quota_policy_refuses_to_start(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUOTA_POLICY_OVERRIDES", '{"entitlements": {"standard": {"nope": []}}}')
    try:
        with pytest.raises(QuotaConfigError):
            importlib.reload(site.module)
    finally:
        monkeypatch.delenv("QUOTA_POLICY_OVERRIDES")
        importlib.reload(site.module)


def test_the_client_address_is_the_validated_one_never_a_forwarding_header() -> None:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"198.51.100.99"), (b"x-real-ip", b"198.51.100.98")],
        "client": ("10.0.0.5", 1234),
        "state": {"validated_client_ip": "203.0.113.4"},
    }
    assert client_address(Request(scope)) == "203.0.113.4"

    scope["state"] = {}
    assert client_address(Request(scope)) == "10.0.0.5"

    scope["client"] = None
    assert client_address(Request(scope)) == "unknown"


# --- administrator routes -----------------------------------------------------


def _admin_requests(site: Site) -> list[tuple[str, str, dict[str, Any] | None]]:
    account = f"/api/admin/quotas/accounts/{site.member}"
    return [
        ("GET", "/api/admin/quotas/overview", None),
        ("GET", "/api/admin/quotas/audit", None),
        ("POST", f"{account}/entitlement", {"entitlement": "extended", "reason": "valid reason"}),
        (
            "POST",
            f"{account}/override",
            {"kind": "report", "limit_percent": 200, "reason": "valid reason"},
        ),
        ("POST", f"{account}/abuse-review", {"decision": "restrict", "reason": "valid reason"}),
    ]


def test_administrator_routes_require_a_signed_in_administrator(site: Site) -> None:
    for method, path, body in _admin_requests(site):
        anonymous = site.client().request(method, path, json=body)
        member = site.as_member().request(method, path, json=body)
        assert anonymous.status_code == 401, path
        assert member.status_code == 403, path
        assert member.json() == {"detail": "Administrator access required."}
        # Authorisation precedes body validation: no probing with malformed bodies.
        invalid = site.as_member().request(method, path, json={"junk": 1} if body else None)
        assert invalid.status_code == 403, path

    assert _audit_rows(site) == []
    assert site.as_member().get("/api/quotas").json()["entitlement"] == "standard"


def test_administrator_routes_fail_closed_when_no_administrator_is_configured(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QUOTA_ADMIN_EMAILS")

    for method, path, body in _admin_requests(site):
        assert site.as_admin().request(method, path, json=body).status_code == 403, path

    assert _audit_rows(site) == []


def _audit_rows(site: Site) -> list[tuple[Any, ...]]:
    with sqlite3.connect(site.db) as conn:
        return conn.execute("SELECT action FROM quota_audit_log").fetchall()


def test_an_anonymous_deployment_has_no_administrators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "anonymous.db")
    init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "1")
    monkeypatch.setenv("QUOTA_ADMIN_EMAILS", "legacy@local")
    for name in ("WEB_GOOGLE_CLIENT_ID", "WEB_GOOGLE_CLIENT_SECRET", "WEB_AUTH_SECRET"):
        monkeypatch.delenv(name, raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as client:
        assert client.get("/api/quotas").status_code == 200
        assert client.get("/api/admin/quotas/overview").status_code == 401
        assert client.get("/api/admin/quotas/audit").status_code == 401


def test_an_administrator_can_review_abuse_and_every_action_is_audited(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUOTA_POLICY_OVERRIDES", _policy({"report": [[1, 3600]]}))
    # Refusals are normally tallied at most once a second; tally every one here
    # so the burst below crosses the review threshold without sleeping.
    monkeypatch.setattr("quotas.DENIAL_SAMPLE_SECONDS", 0.0)
    member, admin = site.as_member(), site.as_admin()
    assert member.get("/api/progress").status_code == 200
    for _ in range(ABUSE_DENIAL_THRESHOLD):
        assert member.get("/api/progress").status_code == 429

    overview = admin.get("/api/admin/quotas/overview")
    queue = overview.json()["queue"]
    assert [entry["account_id"] for entry in queue] == [site.member]
    assert queue[0]["denials_by_kind"] == {"report": ABUSE_DENIAL_THRESHOLD}
    assert overview.json()["usage"]["report"]["denied"] >= ABUSE_DENIAL_THRESHOLD

    review = admin.post(
        f"/api/admin/quotas/accounts/{site.member}/abuse-review",
        json={"decision": "restrict", "reason": "Scripted polling of expensive reports"},
    )

    assert review.status_code == 200
    assert review.json()["entitlement"] == "restricted"
    assert admin.get("/api/admin/quotas/overview").json()["queue"] == []
    assert member.get("/api/quotas").json()["entitlement"] == "restricted"
    entries = admin.get("/api/admin/quotas/audit", params={"account": site.member}).json()[
        "entries"
    ]
    assert [(e["action"], e["actor_account_id"], e["account_id"]) for e in entries] == [
        ("abuse_review", site.admin, site.member)
    ]
    assert entries[0]["reason"] == "Scripted polling of expensive reports"
    assert entries[0]["detail"]["evidence"]["denials_by_kind"] == {
        "report": ABUSE_DENIAL_THRESHOLD
    }
    assert site.as_other().get("/api/quotas").json()["entitlement"] == "standard"


def test_an_administrator_can_change_entitlement_and_override_limits(site: Site) -> None:
    member, admin = site.as_member(), site.as_admin()
    account = f"/api/admin/quotas/accounts/{site.member}"

    granted = admin.post(
        f"{account}/entitlement", json={"entitlement": "extended", "reason": "Beta champion"}
    )
    boosted = admin.post(
        f"{account}/override",
        json={
            "kind": "ai_request",
            "limit_percent": 50,
            "expires_in_seconds": 3600,
            "reason": "Support ticket 7",
        },
    )

    assert granted.status_code == boosted.status_code == 200
    ai = member.get("/api/quotas").json()["quotas"]["ai_request"]
    assert ai["rules"][0]["limit"] == 15  # 50% of the extended 30
    assert ai["override"]["limit_percent"] == 50

    cleared = admin.post(
        f"{account}/override",
        json={"kind": "ai_request", "limit_percent": None, "reason": "Ticket resolved"},
    )

    assert cleared.status_code == 200 and cleared.json()["override"] is None
    restored = member.get("/api/quotas").json()["quotas"]["ai_request"]
    assert restored["rules"][0]["limit"] == 30 and restored["override"] is None
    actions = [
        entry["action"]
        for entry in admin.get("/api/admin/quotas/audit").json()["entries"]
    ]
    assert actions == ["override_clear", "override_set", "entitlement_set"]


def test_administrator_routes_report_invalid_requests_actionably(site: Site) -> None:
    admin = site.as_admin()
    account = f"/api/admin/quotas/accounts/{site.member}"

    unknown_tier = admin.post(
        f"{account}/entitlement", json={"entitlement": "platinum", "reason": "valid reason"}
    )
    no_reason = admin.post(
        f"{account}/entitlement", json={"entitlement": "extended", "reason": "x"}
    )
    ghost = admin.post(
        "/api/admin/quotas/accounts/no-such-account/entitlement",
        json={"entitlement": "extended", "reason": "valid reason"},
    )
    bad_decision = admin.post(
        f"{account}/abuse-review", json={"decision": "ban", "reason": "valid reason"}
    )
    nothing_to_clear = admin.post(
        f"{account}/override",
        json={"kind": "report", "limit_percent": None, "reason": "valid reason"},
    )

    assert unknown_tier.status_code == 422
    assert "restricted, standard, extended" in unknown_tier.json()["detail"]
    assert no_reason.status_code == 422 and "reason" in no_reason.json()["detail"]
    assert ghost.status_code == 404 and ghost.json() == {"detail": "No such account."}
    assert bad_decision.status_code == 422
    assert nothing_to_clear.status_code == 422
    assert _audit_rows(site) == []


def test_administrator_routes_keep_rejecting_owner_claims_in_the_request(site: Site) -> None:
    admin = site.as_admin()
    account = f"/api/admin/quotas/accounts/{site.member}"
    base = {"entitlement": "extended", "reason": "valid reason"}

    for claim in ({"account_id": site.member}, {"user_id": site.other}):
        response = admin.post(f"{account}/entitlement", json={**base, **claim})
        assert response.status_code == 404  # the mutation guard, unchanged

    unknown_field = admin.post(f"{account}/entitlement", json={**base, "surprise": 1})
    assert unknown_field.status_code == 422
    assert _audit_rows(site) == []


def test_every_quota_route_is_documented(site: Site) -> None:
    docs = (Path(__file__).resolve().parents[2] / "docs" / "ACCOUNT_QUOTAS.md").read_text(
        encoding="utf-8"
    )
    paths = {
        route.path
        for route in site.module.app.routes
        if getattr(route, "path", "").startswith(("/api/quotas", "/api/admin/quotas"))
    }

    assert len(paths) == 6
    for path in paths:
        assert path in docs, path


def test_sign_in_throttle_keys_on_the_address_validated_by_the_production_proxy_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real ASGI entrypoint, clients behind one proxy are counted apart."""
    db = str(tmp_path / "proxy.db")
    init_db(db)
    for name, value in {
        "DATABASE_PATH": db,
        "APP_ENV": "test",
        "WEB_PUBLIC_URL": "http://testserver",
        "WEB_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
        "WEB_AUTH_SECRET": _SECRET,
        "WEB_GOOGLE_CLIENT_ID": "quota-client-id",
        "WEB_GOOGLE_CLIENT_SECRET": "quota-client-secret",
        "QUOTA_POLICY_OVERRIDES": _policy(login_by_address=[[2, 300]]),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)

    import webapp.app as app_module
    import webapp.secure_app as secure_module

    importlib.reload(app_module)
    importlib.reload(secure_module)

    async def failing_exchange(request: Any) -> Any:
        raise RuntimeError("invalid authorization code")

    monkeypatch.setattr(app_module.oauth.google, "authorize_access_token", failing_exchange)

    def through(peer: str, forwarded: str) -> int:
        client = TestClient(secure_module.app, client=(peer, 4000), follow_redirects=False)
        return client.get("/auth?code=guess", headers={"X-Forwarded-For": forwarded}).status_code

    # The trusted proxy (10.0.0.5) forwards two different real clients.
    assert [through("10.0.0.5", "203.0.113.9") for _ in range(2)] == [307, 307]
    assert through("10.0.0.5", "203.0.113.9") == 429
    assert through("10.0.0.5", "203.0.113.10") == 307  # a different client, same proxy

    # An untrusted peer cannot choose its bucket by forging forwarding headers.
    assert through("198.51.100.77", "192.0.2.1") == 307
    assert through("198.51.100.77", "192.0.2.2") == 307
    assert through("198.51.100.77", "192.0.2.3") == 429
