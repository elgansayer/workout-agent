"""End-to-end account-policy enforcement through the real FastAPI application.

Covers the acceptance criteria for server-side allowlist/invitation/status
enforcement: login completion decides on the server, policy changes revoke live
sessions, and every denial is uniform to the client, logged without secrets.

Google itself is the only thing faked: ``authorize_access_token`` returns a
synthetic, verified identity. Everything after that (policy, persistence,
session issue, the authentication boundary, cookie expiry) is the production
code path.
"""

from __future__ import annotations

import base64
import importlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

import account_policy
import database
from fastapi import FastAPI
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from webapp.auth_boundary import AuthenticationBoundaryMiddleware

SECRET = "auth-policy-route-test-secret-long-enough-0123456789"
BACKEND_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
DENIED_LOCATION = "/login?error=access_denied"

ALICE = "alice@example.com"
BOB = "bob@example.com"
SUSPENDED = "suspended@example.com"
DELETED = "deleted@example.com"
UNINVITED = "uninvited@example.com"
OUTSIDER = "outsider@example.com"


def _configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    db_path: str,
    *,
    allowed: str | None,
    invite_only: bool,
) -> None:
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    monkeypatch.setenv("WEB_AUTH_SECRET", SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "policy-route-client")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "policy-route-client-secret")
    if allowed is None:
        monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_EMAILS", allowed)
    if invite_only:
        monkeypatch.setenv("INVITE_ONLY", "1")
    else:
        monkeypatch.delenv("INVITE_ONLY", raising=False)


class Harness:
    """One 'server process': a freshly imported app bound to a database."""

    def __init__(self, module: Any, db_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.module = module
        self.db = db_path
        self.identity: dict[str, Any] | None = None
        self._monkeypatch = monkeypatch

        async def fake_authorize_access_token(request: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"userinfo": self.identity}

        # The route calls ``oauth.google.authorize_access_token``; patching the
        # registered client is the single point where Google is simulated.
        monkeypatch.setattr(
            module.oauth.google, "authorize_access_token", fake_authorize_access_token
        )

    @contextmanager
    def client(self) -> Iterator[TestClient]:
        # The context manager runs the lifespan (init_db + startup reconcile).
        with TestClient(self.module.app, follow_redirects=False) as client:
            yield client

    def sign_in(self, client: TestClient, email: str, **claims: Any) -> Any:
        info: dict[str, Any] = {"email": email, "email_verified": True, "name": "Test Person"}
        info.update(claims)
        self.identity = info
        return client.get("/auth")

    def sign_in_ok(self, client: TestClient, email: str = ALICE) -> str:
        response = self.sign_in(client, email)
        assert response.status_code in {302, 307}, response.text
        assert response.headers["location"] == "/"
        cookie = issued_session_cookie(response)
        # A test may have pre-seeded a cookie; keep exactly one in the jar.
        client.cookies.clear()
        client.cookies.set("session", cookie)
        return cookie

    def account(self, email: str) -> database.AccountState:
        state = database.find_account_state(email, self.db)
        assert state is not None
        return state

    def seed_account(self, email: str, status: str | None = None) -> str:
        outcome, state = database.admit_account(
            email, display_name=None, require_invitation=False, db_path=self.db, now=NOW
        )
        assert outcome == database.ADMIT_CREATED and state is not None
        if status:
            database.set_account_status(state["id"], status, db_path=self.db, now=NOW)
        return state["id"]


def build_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed: str | None = None,
    invite_only: bool = False,
    db_name: str = "policy-routes.db",
) -> Harness:
    db_path = str(tmp_path / db_name)
    database.init_db(db_path)
    _configure_environment(monkeypatch, db_path, allowed=allowed, invite_only=invite_only)
    import webapp.app as app_module

    return Harness(importlib.reload(app_module), db_path, monkeypatch)


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return build_harness(tmp_path, monkeypatch)


def issued_session_cookie(response: Any) -> str:
    """The signed session value the server asked the browser to store."""
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name.strip() == "session":
            return rest.split(";", 1)[0]
    raise AssertionError("the response did not issue a session cookie")


def decode_session(cookie_value: str) -> dict[str, Any]:
    signed = TimestampSigner(SECRET).unsign(cookie_value.encode("utf-8"))
    return json.loads(base64.b64decode(signed))


def forge_session(payload: dict[str, Any]) -> str:
    """Sign a payload the way a (hypothetically leaked-key) attacker could."""
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(SECRET).sign(data).decode("utf-8")


def snapshot(response: Any) -> tuple[Any, ...]:
    """Everything an observer of a denied response could use to tell reasons apart."""
    headers = {k.lower(): v for k, v in response.headers.items() if k.lower() != "date"}
    return (response.status_code, response.content, tuple(sorted(headers.items())))


# --------------------------------------------------------------------------
# Login completion
# --------------------------------------------------------------------------


def test_login_completion_admits_a_verified_identity_and_binds_the_session(harness: Harness) -> None:
    with harness.client() as client:
        cookie = harness.sign_in_ok(client)
        payload = decode_session(cookie)
        account = harness.account(ALICE)

        assert payload == {"user": ALICE, "user_id": account["id"], "session_epoch": 0}
        me = client.get("/api/me")
        assert me.status_code == 200
        assert me.json() == {"user": ALICE}


def test_login_discards_any_pre_login_session_state(harness: Harness) -> None:
    with harness.client() as client:
        client.cookies.set(
            "session",
            forge_session({"stale": "carried-over", "_state_google_x": {"nonce": "n"}, "user_id": "someone-else"}),
        )
        cookie = harness.sign_in_ok(client)
        assert set(decode_session(cookie)) == {"user", "user_id", "session_epoch"}


def test_signing_in_as_a_different_identity_replaces_the_previous_one(harness: Harness) -> None:
    with harness.client() as client:
        harness.sign_in_ok(client, ALICE)
        harness.sign_in_ok(client, BOB)
        assert client.get("/api/me").json() == {"user": BOB}


def test_session_email_comes_from_the_stored_account_not_the_raw_claim(harness: Harness) -> None:
    with harness.client() as client:
        response = harness.sign_in(client, "  ALICE@Example.COM ")
        assert response.status_code in {302, 307}
        assert client.get("/api/me").json() == {"user": ALICE}


def test_allowlist_is_enforced_on_the_server_at_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = build_harness(tmp_path, monkeypatch, allowed=f"{ALICE}, {BOB}")
    with harness.client() as client:
        refused = harness.sign_in(client, OUTSIDER)
        assert refused.status_code == 303 and refused.headers["location"] == DENIED_LOCATION
        assert client.get("/api/me").status_code == 401
        assert database.find_account_state(OUTSIDER, harness.db) is None

        harness.sign_in_ok(client, ALICE)
        assert client.get("/api/me").status_code == 200


def test_invite_only_admits_exactly_one_account_per_invitation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = build_harness(tmp_path, monkeypatch, invite_only=True)
    database.create_account_invitation(
        ALICE, expires_at=datetime.now(tz=timezone.utc) + timedelta(days=1), db_path=harness.db
    )
    with harness.client() as client:
        assert harness.sign_in(client, BOB).headers["location"] == DENIED_LOCATION
        assert database.find_account_state(BOB, harness.db) is None

        harness.sign_in_ok(client, ALICE)
        first = harness.account(ALICE)
        # Signing in again (or a retried callback) reuses the account.
        harness.sign_in_ok(client, ALICE)
        assert harness.account(ALICE) == first
        statuses = [i["status"] for i in database.list_account_invitations(ALICE, harness.db)]
        assert statuses == ["accepted"]


# --------------------------------------------------------------------------
# Non-enumeration
# --------------------------------------------------------------------------


@pytest.fixture()
def strict_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Allowlist + invite-only, with one account in each interesting state."""
    allowed = f"{ALICE},{SUSPENDED},{DELETED},{UNINVITED}"
    harness = build_harness(tmp_path, monkeypatch, allowed=allowed, invite_only=True)
    harness.seed_account(ALICE)
    harness.seed_account(SUSPENDED, database.ACCOUNT_STATUS_SUSPENDED)
    harness.seed_account(DELETED, database.ACCOUNT_STATUS_DELETED)
    return harness


DENIAL_SCENARIOS: dict[str, tuple[str | None, dict[str, Any] | None]] = {
    "not_allowlisted": (OUTSIDER, {}),
    "not_invited": (UNINVITED, {}),
    "suspended": (SUSPENDED, {}),
    "deleted": (DELETED, {}),
    "unverified": (ALICE, {"email_verified": False}),
    "unverified_missing_claim": (ALICE, {"email_verified": None}),
    "malformed_email": ("not-an-email", {}),
    "no_identity_at_all": (None, None),
}


def _attempt(harness: Harness, email: str | None, claims: dict[str, Any] | None) -> tuple[Any, ...]:
    with harness.client() as client:
        if email is None:
            harness.identity = None
            response = client.get("/auth")
        else:
            response = harness.sign_in(client, email, **(claims or {}))
        # Whatever the reason, the visitor ends up without a session.
        assert client.get("/api/me").status_code == 401
    return snapshot(response)


def test_every_denial_reason_produces_a_byte_identical_response(strict_harness: Harness) -> None:
    observed = {
        name: _attempt(strict_harness, email, claims)
        for name, (email, claims) in DENIAL_SCENARIOS.items()
    }
    assert len(set(observed.values())) == 1, {k: v for k, v in observed.items()}

    status, body, headers = next(iter(observed.values()))
    assert status == 303
    assert body == b""
    assert dict(headers)["location"] == DENIED_LOCATION


def test_denial_responses_never_contain_the_submitted_identity(strict_harness: Harness) -> None:
    for email in (OUTSIDER, SUSPENDED, DELETED, UNINVITED):
        _status, body, headers = _attempt(strict_harness, email, {"name": "Distinctive Person Name"})
        blob = (body + json.dumps(dict(headers)).encode()).lower()
        assert email.split("@")[0].encode() not in blob
        assert b"distinctive" not in blob
        for word in (b"suspend", b"delet", b"invit", b"allowlist", b"not allowed"):
            assert word not in blob


def test_hostile_identity_values_are_not_reflected(strict_harness: Harness) -> None:
    """The previous callback echoed the email into an unescaped HTML body."""
    hostile = "<script>alert(document.cookie)</script>@example.com"
    for email in (hostile, f'"><img src=x onerror=alert(1)>{OUTSIDER}'):
        status, body, headers = _attempt(strict_harness, email, {"name": "<b>x</b>"})
        assert status == 303
        assert b"script" not in body.lower() and b"onerror" not in body.lower()
        assert b"script" not in json.dumps(dict(headers)).encode().lower()


def test_denied_attempts_create_no_accounts_and_spend_no_invitations(strict_harness: Harness) -> None:
    database.create_account_invitation(
        SUSPENDED, expires_at=datetime.now(tz=timezone.utc) + timedelta(days=1), db_path=strict_harness.db
    )
    before = _row_counts(strict_harness.db)
    for email, claims in DENIAL_SCENARIOS.values():
        _attempt(strict_harness, email, claims)
    assert _row_counts(strict_harness.db) == before
    assert [i["status"] for i in database.list_account_invitations(SUSPENDED, strict_harness.db)] == ["pending"]


def _row_counts(db_path: str) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            "users": int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
            "account_invitations": int(
                conn.execute("SELECT COUNT(*) FROM account_invitations").fetchone()[0]
            ),
        }


def test_denial_is_logged_with_a_reason_but_without_identity_or_secrets(
    strict_harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for email, claims in DENIAL_SCENARIOS.values():
        _attempt(strict_harness, email, {**(claims or {}), "access_token": "ya29.synthetic-token"})

    text = "\n".join(r.getMessage() for r in caplog.records)
    for reason in ("not_allowlisted", "not_invited", "account_suspended", "account_deleted", "identity_unverified"):
        assert f"reason={reason}" in text, reason
    for forbidden in (
        "outsider", "uninvited", "suspended@", "deleted@", "example.com",
        "ya29.synthetic-token", SECRET, "policy-route-client-secret",
    ):
        assert forbidden not in text, forbidden


# --------------------------------------------------------------------------
# Request-time enforcement and revocation
# --------------------------------------------------------------------------


def _logged_out_response(harness: Harness) -> Any:
    with harness.client() as stranger:
        return stranger.get("/api/me")


def test_suspension_ends_a_live_session_on_the_very_next_request(harness: Harness) -> None:
    with harness.client() as client:
        harness.sign_in_ok(client)
        assert client.get("/api/me").status_code == 200

        database.set_account_status(harness.account(ALICE)["id"], "suspended", db_path=harness.db)

        revoked = client.get("/api/me")
        logged_out = _logged_out_response(harness)
        # Indistinguishable from never having been signed in...
        assert (revoked.status_code, revoked.json()) == (logged_out.status_code, logged_out.json())
        assert revoked.status_code == 401 and revoked.json() == {"detail": "Not authenticated"}
        # ...and the browser is told to forget the cookie.
        set_cookie = revoked.headers["set-cookie"]
        assert "session=null" in set_cookie and "1970" in set_cookie
        assert client.get("/api/me").status_code == 401


def test_page_navigation_with_a_revoked_session_redirects_to_login(harness: Harness) -> None:
    with harness.client() as client:
        harness.sign_in_ok(client)
        database.revoke_account_sessions(harness.account(ALICE)["id"], harness.db)
        response = client.get("/dashboard")
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_reactivation_does_not_resurrect_sessions_but_a_new_login_works(harness: Harness) -> None:
    with harness.client() as client:
        old_cookie = harness.sign_in_ok(client)
        user_id = harness.account(ALICE)["id"]
        database.set_account_status(user_id, "suspended", db_path=harness.db)
        database.set_account_status(user_id, "active", db_path=harness.db)

        with harness.client() as stale:
            stale.cookies.set("session", old_cookie)
            assert stale.get("/api/me").status_code == 401

        harness.sign_in_ok(client)
        assert client.get("/api/me").status_code == 200


def test_deletion_ends_sessions_and_blocks_sign_in_permanently(harness: Harness) -> None:
    with harness.client() as client:
        harness.sign_in_ok(client)
        database.set_account_status(harness.account(ALICE)["id"], "deleted", db_path=harness.db)
        assert client.get("/api/me").status_code == 401
        assert harness.sign_in(client, ALICE).headers["location"] == DENIED_LOCATION
        assert client.get("/api/me").status_code == 401


def test_revoking_sessions_signs_out_every_device_but_allows_a_new_login(harness: Harness) -> None:
    with harness.client() as laptop, harness.client() as phone:
        harness.sign_in_ok(laptop)
        harness.sign_in_ok(phone)
        database.revoke_account_sessions(harness.account(ALICE)["id"], harness.db)
        assert laptop.get("/api/me").status_code == 401
        assert phone.get("/api/me").status_code == 401

        cookie = harness.sign_in_ok(laptop)
        assert decode_session(cookie)["session_epoch"] == 1
        assert laptop.get("/api/me").status_code == 200
        assert phone.get("/api/me").status_code == 401


def test_revocation_is_per_tenant(harness: Harness) -> None:
    with harness.client() as a, harness.client() as b:
        harness.sign_in_ok(a, ALICE)
        harness.sign_in_ok(b, BOB)
        database.set_account_status(harness.account(ALICE)["id"], "suspended", db_path=harness.db)
        assert a.get("/api/me").status_code == 401
        assert b.get("/api/me").status_code == 200


def test_mutations_with_a_revoked_session_never_reach_route_code(harness: Harness) -> None:
    with harness.client() as client:
        harness.sign_in_ok(client)
        user_id = harness.account(ALICE)["id"]
        database.save_notification(user_id, "Title", "Body", db_path=harness.db)

        database.revoke_account_sessions(user_id, harness.db)
        refused = client.post("/api/notifications/clear")
        assert refused.status_code == 401
        assert len(database.get_notifications(user_id, db_path=harness.db)) == 1

        harness.sign_in_ok(client)
        assert client.post("/api/notifications/clear").status_code == 200
        assert database.get_notifications(user_id, db_path=harness.db) == []


def test_sessions_issued_by_the_previous_release_survive_until_revoked(harness: Harness) -> None:
    user_id = harness.seed_account(ALICE)
    legacy_cookie = forge_session({"user": ALICE, "user_id": user_id})  # no session_epoch
    with harness.client() as client:
        client.cookies.set("session", legacy_cookie)
        assert client.get("/api/me").status_code == 200
        database.revoke_account_sessions(user_id, harness.db)
        assert client.get("/api/me").status_code == 401


def test_validly_signed_cookie_for_an_unknown_account_is_refused(harness: Harness) -> None:
    with harness.client() as client:
        client.cookies.set("session", forge_session({"user": ALICE, "user_id": "no-such-account"}))
        assert client.get("/api/me").status_code == 401


def test_cookie_claims_cannot_override_server_side_state(harness: Harness) -> None:
    """A cookie asserting a higher epoch than the server holds is not trusted."""
    user_id = harness.seed_account(ALICE)
    with harness.client() as client:
        client.cookies.set("session", forge_session({"user": ALICE, "user_id": user_id, "session_epoch": 5}))
        assert client.get("/api/me").status_code == 401


# --------------------------------------------------------------------------
# Allowlist changes (configuration is environment-driven, so they land on restart)
# --------------------------------------------------------------------------


def test_allowlist_removal_revokes_sessions_and_re_adding_does_not_resurrect_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = f"{ALICE},{BOB}"
    first = build_harness(tmp_path, monkeypatch, allowed=both)
    with first.client() as client:
        alice_cookie = first.sign_in_ok(client, ALICE)
        bob_cookie = first.sign_in_ok(client, BOB)

    # Restart with alice removed.
    removed = build_harness(tmp_path, monkeypatch, allowed=BOB)
    with removed.client() as client:
        client.cookies.set("session", alice_cookie)
        assert client.get("/api/me").status_code == 401
        client.cookies.set("session", bob_cookie)
        assert client.get("/api/me").status_code == 200

    # Restart with alice added back: her pre-removal cookie stays dead.
    restored = build_harness(tmp_path, monkeypatch, allowed=both)
    with restored.client() as client:
        client.cookies.set("session", alice_cookie)
        assert client.get("/api/me").status_code == 401
        restored.sign_in_ok(client, ALICE)
        assert client.get("/api/me").status_code == 200


def test_enabling_an_allowlist_ends_sessions_of_accounts_it_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_deployment = build_harness(tmp_path, monkeypatch)
    with open_deployment.client() as client:
        outsider_cookie = open_deployment.sign_in_ok(client, OUTSIDER)

    restricted = build_harness(tmp_path, monkeypatch, allowed=ALICE)
    with restricted.client() as client:
        client.cookies.set("session", outsider_cookie)
        assert client.get("/api/me").status_code == 401


# --------------------------------------------------------------------------
# Outages and boundary invariants
# --------------------------------------------------------------------------


def test_unreadable_policy_store_refuses_requests_without_signing_users_out(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    with harness.client() as client:
        cookie = harness.sign_in_ok(client)

        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise sqlite3.OperationalError("disk I/O error at /secret/path/policy.db")

        with monkeypatch.context() as outage_patch:
            outage_patch.setattr(account_policy, "get_account_state", broken)
            outage = client.get("/api/me")
            assert outage.status_code == 503
            assert outage.json() == {"detail": "Service temporarily unavailable"}
            assert "secret" not in outage.text and "set-cookie" not in outage.headers

        # The store recovered: the very same cookie was never discarded.
        assert client.cookies.get("session") == cookie
        assert client.get("/api/me").status_code == 200


def test_login_with_an_unreadable_policy_store_fails_closed_and_says_unavailable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(account_policy, "admit_account", broken)
    with harness.client() as client:
        response = harness.sign_in(client, ALICE)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=unavailable"
        assert client.get("/api/me").status_code == 401


def test_public_routes_do_not_depend_on_the_policy_store(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("down")

    monkeypatch.setattr(account_policy, "get_account_state", broken)
    with harness.client() as client:
        response = client.get("/sw.js")
        assert response.status_code == 200


def test_authenticated_mode_cannot_run_without_a_session_guard() -> None:
    async def downstream(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        raise AssertionError("never called")

    with pytest.raises(RuntimeError, match="session guard"):
        AuthenticationBoundaryMiddleware(downstream, anonymous_enabled=False)
    # Local anonymous mode needs no guard.
    AuthenticationBoundaryMiddleware(downstream, anonymous_enabled=True)
    AuthenticationBoundaryMiddleware(
        downstream, session_guard=lambda _s: account_policy.SessionDecision(allowed=True)
    )


def test_boundary_install_refuses_to_build_without_an_access_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_environment(monkeypatch, str(tmp_path / "x.db"), allowed=None, invite_only=False)
    bare = FastAPI(title="Workout Agent")
    bare.add_middleware(SessionMiddleware, secret_key=SECRET)
    with pytest.raises(RuntimeError, match="access_policy"):
        bare.build_middleware_stack()


def test_every_session_decision_goes_through_the_guard(harness: Harness) -> None:
    """The boundary is built with the very policy object the login route uses."""
    app = harness.module.app
    assert app.state.access_policy is harness.module.ACCESS_POLICY
    with harness.client() as client:
        client.get("/sw.js")  # first request builds the middleware stack
    boundary = next(
        m for m in app.user_middleware if m.cls is AuthenticationBoundaryMiddleware
    )
    assert boundary.kwargs["session_guard"] == harness.module.ACCESS_POLICY.check_session
    assert boundary.kwargs["anonymous_enabled"] is False


# --------------------------------------------------------------------------
# Startup validation (subprocess: a failed import must not poison this process)
# --------------------------------------------------------------------------


def _import_app_in_subprocess(tmp_path: Path, extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"ALLOWED_EMAILS", "INVITE_ONLY"}}
    env.update(
        {
            "DATABASE_PATH": str(tmp_path / "startup.db"),
            "APP_ENV": "production",
            "ALLOW_ANONYMOUS_WEB": "0",
            "WEB_AUTH_SECRET": SECRET,
            "WEB_GOOGLE_CLIENT_ID": "startup-client",
            "WEB_GOOGLE_CLIENT_SECRET": "startup-client-secret",
        }
    )
    env.update(extra)
    code = f"import sys; sys.path.insert(0, {str(BACKEND_DIR)!r}); import webapp.app"
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False,
    )


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"ALLOWED_EMAILS": "typo-with-no-at-sign"}, "ALLOWED_EMAILS"),
        ({"ALLOWED_EMAILS": f"{ALICE} {BOB}"}, "ALLOWED_EMAILS"),
        ({"ALLOWED_EMAILS": ",,"}, "ALLOWED_EMAILS"),
        ({"INVITE_ONLY": "maybe"}, "INVITE_ONLY"),
    ],
)
def test_unsafe_access_configuration_stops_startup_without_echoing_values(
    tmp_path: Path, extra: dict[str, str], needle: str
) -> None:
    result = _import_app_in_subprocess(tmp_path, extra)
    assert result.returncode != 0
    assert "AccessPolicyConfigurationError" in result.stderr and needle in result.stderr
    for value in extra.values():
        if len(value) > 3:
            assert value not in result.stderr
    assert "startup-client-secret" not in result.stderr and SECRET not in result.stderr


def test_valid_access_configuration_starts(tmp_path: Path) -> None:
    result = _import_app_in_subprocess(tmp_path, {"ALLOWED_EMAILS": f"{ALICE}, {BOB}", "INVITE_ONLY": "true"})
    assert result.returncode == 0, result.stderr
