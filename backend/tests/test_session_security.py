"""Regression tests for hardened, revocable browser sessions (issue #752).

Acceptance criteria covered here:

* cookie attributes - ``Secure``, ``HttpOnly``, ``SameSite``, narrow ``Path`` and a
  host-only ``Domain`` policy;
* rotation after login, idle and absolute expiry, and revocation;
* logout, logout-all-devices, expired-session and stolen-cookie regressions.

All identities are synthetic (``example.test``) and all time is driven by an
injected clock, so nothing depends on wall-clock timing.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import sqlite3
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from connector_health import _SERVER_WIDE_ENV_VARS
from database import get_or_create_user, init_db
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from httpx import Response
from itsdangerous import TimestampSigner
from session_helpers import signed_session_cookie
from webapp.auth_boundary import AuthenticationBoundaryMiddleware
from webapp.mutation_security import MutationSecurityMiddleware
from webapp.session_security import (
    DEFAULT_ABSOLUTE_TIMEOUT_SECONDS,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    HardenedSessionMiddleware,
    SessionLifecycle,
    SessionLifetimePolicy,
    SessionSecurityConfigurationError,
    SessionState,
    SessionStore,
    load_session_security_config,
)

_SECRET = "session-security-secret-that-is-long-enough-1234"
_DAY = 24 * 60 * 60
_DEV_ENV = {"APP_ENV": "test", "ALLOW_ANONYMOUS_WEB": "1"}
_PROD_ENV = {
    "APP_ENV": "production",
    "WEB_AUTH_SECRET": _SECRET,
    "WEB_GOOGLE_CLIENT_ID": "synthetic-client-id",
    "WEB_GOOGLE_CLIENT_SECRET": "synthetic-client-secret",
    "WEB_PUBLIC_URL": "https://workout.example",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Clock:
    """Deterministic, manually advanced clock."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _parse_set_cookie(header: str) -> tuple[str, str, dict[str, str]]:
    """Split a ``Set-Cookie`` header into (name, value, lower-cased attributes)."""

    name_value, *attributes = [part.strip() for part in header.split(";")]
    name, _, value = name_value.partition("=")
    parsed: dict[str, str] = {}
    for attribute in attributes:
        key, _, attribute_value = attribute.partition("=")
        parsed[key.lower()] = attribute_value
    return name, value, parsed


def _session_set_cookies(
    response: Response, name: str
) -> list[tuple[str, dict[str, str]]]:
    found = []
    for header in response.headers.get_list("set-cookie"):
        cookie_name, value, attributes = _parse_set_cookie(header)
        if cookie_name == name:
            found.append((value, attributes))
    return found


def _decode(cookie_value: str) -> dict[str, Any]:
    """Decode the payload of a signed session cookie, verifying its signature."""

    payload = TimestampSigner(_SECRET).unsign(cookie_value.encode())
    decoded = json.loads(base64.b64decode(payload))
    assert isinstance(decoded, dict)
    return decoded


def _row_count(db_path: str, where: str = "1 = 1", args: tuple[Any, ...] = ()) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM user_sessions WHERE {where}",
                args,
            ).fetchone()[0]
        )


# ---------------------------------------------------------------------------
# Cookie and lifetime policy
# ---------------------------------------------------------------------------


def test_development_policy_is_httponly_lax_host_only_and_not_secure() -> None:
    config = load_session_security_config(_DEV_ENV)

    assert config.cookie.name == "session"
    assert config.cookie.secure is False
    assert config.cookie.same_site == "lax"
    assert config.cookie.path == "/"
    assert config.cookie.domain is None
    assert config.lifetime.idle_timeout == DEFAULT_IDLE_TIMEOUT_SECONDS
    assert config.lifetime.absolute_timeout == DEFAULT_ABSOLUTE_TIMEOUT_SECONDS
    # The client-side Max-Age never outlives the server-side absolute limit.
    assert config.cookie.max_age == config.lifetime.absolute_timeout


def test_production_always_uses_a_secure_host_prefixed_cookie() -> None:
    config = load_session_security_config(_PROD_ENV)

    assert config.cookie.secure is True
    assert config.cookie.name == "__Host-session"
    assert config.cookie.path == "/"
    assert config.cookie.domain is None


def test_production_cannot_be_downgraded_by_a_non_https_public_url() -> None:
    # Secure is derived from APP_ENV, so a stray http URL cannot switch it off.
    config = load_session_security_config({**_PROD_ENV, "WEB_PUBLIC_URL": "http://x"})

    assert config.cookie.secure is True


def test_https_public_url_outside_production_is_also_secure() -> None:
    config = load_session_security_config(
        {**_DEV_ENV, "WEB_PUBLIC_URL": "https://staging.example"}
    )

    assert config.cookie.secure is True
    assert config.cookie.name == "__Host-session"


def test_http_localhost_development_keeps_working() -> None:
    config = load_session_security_config(
        {**_DEV_ENV, "WEB_PUBLIC_URL": "http://localhost:8770"}
    )

    assert config.cookie.secure is False
    assert config.cookie.name == "session"


def test_lifetime_limits_are_configurable_and_blank_values_use_defaults() -> None:
    custom = load_session_security_config(
        {
            **_DEV_ENV,
            "SESSION_IDLE_TIMEOUT_SECONDS": "900",
            "SESSION_ABSOLUTE_TIMEOUT_SECONDS": "7200",
        }
    )
    assert (custom.lifetime.idle_timeout, custom.lifetime.absolute_timeout) == (
        900,
        7200,
    )
    assert custom.cookie.max_age == 7200

    blank = load_session_security_config(
        {
            **_DEV_ENV,
            "SESSION_IDLE_TIMEOUT_SECONDS": " ",
            "SESSION_ABSOLUTE_TIMEOUT_SECONDS": "",
        }
    )
    assert blank.lifetime.idle_timeout == DEFAULT_IDLE_TIMEOUT_SECONDS
    assert blank.lifetime.absolute_timeout == DEFAULT_ABSOLUTE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "value",
    ["0", "-5", "abc", "1.5", "1e3", "+60", "59", str(91 * _DAY), "٣٠٠"],
)
@pytest.mark.parametrize(
    "variable", ["SESSION_IDLE_TIMEOUT_SECONDS", "SESSION_ABSOLUTE_TIMEOUT_SECONDS"]
)
def test_malformed_or_out_of_range_limits_stop_startup(
    variable: str, value: str
) -> None:
    with pytest.raises(SessionSecurityConfigurationError):
        load_session_security_config({**_DEV_ENV, variable: value})


def test_idle_limit_may_not_exceed_the_absolute_limit() -> None:
    with pytest.raises(SessionSecurityConfigurationError, match="must not exceed"):
        load_session_security_config(
            {
                **_DEV_ENV,
                "SESSION_IDLE_TIMEOUT_SECONDS": "3600",
                "SESSION_ABSOLUTE_TIMEOUT_SECONDS": "600",
            }
        )


# ---------------------------------------------------------------------------
# Registry (store) behaviour
# ---------------------------------------------------------------------------


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "sessions.db")


@pytest.fixture()
def lifetime() -> SessionLifetimePolicy:
    return SessionLifetimePolicy(idle_timeout=3 * _DAY, absolute_timeout=14 * _DAY)


@pytest.fixture()
def store(db_path: str, lifetime: SessionLifetimePolicy, clock: _Clock) -> SessionStore:
    return SessionStore(db_path, lifetime, clock=clock)


def test_registry_stores_only_a_digest_of_an_unguessable_identifier(
    store: SessionStore, db_path: str
) -> None:
    session_id = store.create("user-a")

    assert len(session_id) >= 43  # 256 bits, URL-safe encoded
    assert session_id != store.create("user-a")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM user_sessions").fetchall()
    assert hashlib.sha256(session_id.encode()).hexdigest() in {row[0] for row in rows}
    assert session_id not in json.dumps(rows)


def test_schema_is_created_on_demand_for_a_brand_new_database(
    tmp_path: Path, lifetime: SessionLifetimePolicy
) -> None:
    store = SessionStore(str(tmp_path / "nested" / "fresh.db"), lifetime)

    assert store.check(store.create("user-a"), "user-a") is SessionState.ACTIVE


def test_user_sessions_table_is_tenant_scoped_and_user_indexed(tmp_path: Path) -> None:
    db = str(tmp_path / "schema.db")
    init_db(db)
    init_db(db)  # idempotent

    with sqlite3.connect(db) as conn:
        columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(user_sessions)")
        }
        foreign_keys = conn.execute("PRAGMA foreign_key_list(user_sessions)").fetchall()
        indexes = [
            [column[2] for column in conn.execute(f"PRAGMA index_info({index[1]})")]
            for index in conn.execute("PRAGMA index_list(user_sessions)")
        ]

    assert columns["user_id"][3] == 1  # NOT NULL
    assert any(fk[2] == "users" and fk[3] == "user_id" for fk in foreign_keys)
    assert ["user_id", "revoked_at"] in indexes


def test_session_expires_exactly_at_the_idle_limit(
    store: SessionStore, clock: _Clock, lifetime: SessionLifetimePolicy
) -> None:
    session_id = store.create("user-a")

    clock.advance(lifetime.idle_timeout - 1)
    assert store.check(session_id, "user-a", touch=False) is SessionState.ACTIVE
    clock.advance(1)
    assert store.check(session_id, "user-a", touch=False) is SessionState.IDLE_EXPIRED


def test_activity_keeps_a_session_alive_past_the_idle_limit(
    store: SessionStore, clock: _Clock, lifetime: SessionLifetimePolicy
) -> None:
    session_id = store.create("user-a")

    for _ in range(4):  # 4 x 2 days > the 3 day idle limit, but never idle that long
        clock.advance(2 * _DAY)
        assert store.check(session_id, "user-a") is SessionState.ACTIVE
    assert clock.now - 1_700_000_000.0 > lifetime.idle_timeout


@pytest.mark.parametrize("idle_limit", [60, 90, 118, 240, 3600])
def test_an_active_user_is_never_idled_out_even_with_a_short_idle_limit(
    db_path: str, clock: _Clock, idle_limit: int
) -> None:
    # The stored last-seen time lags real activity by up to one refresh interval,
    # so the interval must shrink with the idle limit. Before it did, a 60 second
    # limit signed out someone who made a request every 40 seconds.
    policy = SessionLifetimePolicy(
        idle_timeout=idle_limit, absolute_timeout=10 * idle_limit
    )
    store = SessionStore(db_path, policy, clock=clock)
    session_id = store.create("user-a")
    # Always inside the idle limit, and always shorter than the old fixed
    # one-minute refresh interval, which is what used to hide the activity.
    request_every = min(59, (idle_limit * 2) // 3)
    requests_to_make = -(-3 * idle_limit // request_every)  # ceil: ~3x the idle limit

    for _ in range(requests_to_make):
        clock.advance(request_every)
        assert store.check(session_id, "user-a") is SessionState.ACTIVE

    # ...and a genuinely idle session still ends at the configured limit.
    clock.advance(idle_limit)
    assert store.check(session_id, "user-a") is SessionState.IDLE_EXPIRED


def test_refresh_interval_is_a_minute_at_most_and_a_quarter_of_the_idle_limit() -> None:
    day = SessionLifetimePolicy(idle_timeout=3 * _DAY, absolute_timeout=14 * _DAY)
    assert day.touch_interval == day.last_seen_resolution == 60
    assert (
        SessionLifetimePolicy(idle_timeout=240, absolute_timeout=_DAY).touch_interval
        == 60
    )
    assert (
        SessionLifetimePolicy(idle_timeout=60, absolute_timeout=_DAY).touch_interval
        == 15
    )
    # Degenerate limits never produce a zero interval (a write on every request).
    assert (
        SessionLifetimePolicy(idle_timeout=2, absolute_timeout=_DAY).touch_interval == 1
    )


def test_absolute_limit_is_never_extended_by_activity(
    store: SessionStore, clock: _Clock, lifetime: SessionLifetimePolicy
) -> None:
    session_id = store.create("user-a")

    for _ in range(lifetime.absolute_timeout // _DAY - 1):
        clock.advance(_DAY)
        assert store.check(session_id, "user-a") is SessionState.ACTIVE
    clock.advance(_DAY)  # exactly the absolute limit, despite daily activity
    assert store.check(session_id, "user-a") is SessionState.ABSOLUTE_EXPIRED


def test_revocation_is_owner_scoped(store: SessionStore) -> None:
    session_id = store.create("user-a")

    # Knowing a session identifier is not enough to revoke another tenant's session.
    assert store.revoke(session_id, "user-b", reason="logout") is False
    assert store.check(session_id, "user-a") is SessionState.ACTIVE

    assert store.revoke(session_id, "user-a", reason="logout") is True
    assert store.revoke(session_id, "user-a", reason="logout") is False
    assert store.check(session_id, "user-a") is SessionState.REVOKED


def test_a_session_is_only_valid_for_the_user_that_owns_it(store: SessionStore) -> None:
    session_id = store.create("user-a")

    assert store.check(session_id, "user-b") is SessionState.OWNER_MISMATCH
    assert store.check(session_id, "user-a") is SessionState.ACTIVE


def test_revoke_all_is_tenant_scoped_counts_active_and_cannot_be_undone_by_config(
    db_path: str, clock: _Clock, lifetime: SessionLifetimePolicy
) -> None:
    store = SessionStore(db_path, lifetime, clock=clock)
    stale = store.create("user-a")
    clock.advance(lifetime.idle_timeout + 1)  # `stale` is now idle-expired
    first, second = store.create("user-a"), store.create("user-a")
    other_tenant = store.create("user-b")

    assert store.revoke_all("user-a", reason="logout_all") == 2

    assert store.check(first, "user-a") is SessionState.REVOKED
    assert store.check(second, "user-a") is SessionState.REVOKED
    assert store.check(other_tenant, "user-b") is SessionState.ACTIVE
    # Even rows that had already aged out are marked, so loosening the idle
    # limit later cannot resurrect a device the user signed out.
    looser = SessionStore(
        db_path,
        SessionLifetimePolicy(idle_timeout=10 * _DAY, absolute_timeout=30 * _DAY),
        clock=clock,
    )
    assert looser.check(stale, "user-a") is SessionState.REVOKED


@pytest.mark.parametrize(
    "session_id",
    [None, "", "short", "a" * 129, 12345, "has space " * 5, "x" * 31, ["x" * 43]],
)
def test_malformed_identifiers_are_unknown_and_never_error(
    store: SessionStore, session_id: object
) -> None:
    assert store.check(session_id, "user-a") is SessionState.UNKNOWN
    assert store.revoke(session_id, "user-a", reason="logout") is False


def test_unknown_identifier_and_blank_owner_are_rejected(store: SessionStore) -> None:
    real = store.create("user-a")

    assert store.check("x" * 43, "user-a") is SessionState.UNKNOWN
    assert store.check(real, "") is SessionState.UNKNOWN
    assert store.check(real, None) is SessionState.UNKNOWN
    with pytest.raises(ValueError):
        store.create("")


def test_dead_sessions_are_purged_after_the_retention_window(
    store: SessionStore, db_path: str, clock: _Clock, lifetime: SessionLifetimePolicy
) -> None:
    doomed = store.create("user-a")
    store.revoke(doomed, "user-a", reason="logout")
    assert _row_count(db_path) == 1

    clock.advance(lifetime.dead_session_retention + 1)
    store.create("user-b")  # creation opportunistically purges

    assert _row_count(db_path, "user_id = ?", ("user-a",)) == 0
    assert _row_count(db_path, "user_id = ?", ("user-b",)) == 1


def test_a_busy_database_does_not_fail_an_otherwise_valid_request(
    db_path: str, clock: _Clock, lifetime: SessionLifetimePolicy
) -> None:
    store = SessionStore(db_path, lifetime, clock=clock, busy_timeout=0.05)
    session_id = store.create("user-a")
    clock.advance(lifetime.last_seen_resolution + 1)

    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # hold the write lock
    try:
        # Reading still works and the best-effort last-seen refresh is skipped.
        assert store.check(session_id, "user-a") is SessionState.ACTIVE
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


# ---------------------------------------------------------------------------
# Middleware behaviour through a minimal application
# ---------------------------------------------------------------------------


class _Harness:
    """A tiny app mounting the production session middleware and lifecycle."""

    def __init__(
        self,
        db_path: str,
        clock: _Clock,
        *,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.config = load_session_security_config(environ or _DEV_ENV)
        self.store = SessionStore(db_path, self.config.lifetime, clock=clock)
        self.lifecycle = SessionLifecycle(self.store)
        self.allowed: set[str] | None = None
        self.base_url = (
            "https://workout.example"
            if self.config.cookie.secure
            else "http://testserver"
        )
        self.app = self._build()

    def _build(self) -> FastAPI:
        app = FastAPI()
        lifecycle = self.lifecycle

        @app.get("/oauth/start")
        def oauth_start(request: Request) -> dict[str, str]:
            # Mirrors authlib: pre-login OAuth state lives in the session.
            request.session["_state_google_abc"] = {"data": {"nonce": "n"}}
            return {"status": "ok"}

        @app.get("/test/login/{user_id}")
        def login(user_id: str, request: Request) -> dict[str, str]:
            lifecycle.establish(
                request.session, user_id=user_id, email=f"{user_id}@example.test"
            )
            return {"status": "ok"}

        @app.get("/whoami")
        def whoami(request: Request) -> dict[str, str]:
            user_id = request.session.get("user_id")
            if not user_id or not request.session.get("user"):
                raise HTTPException(status_code=401, detail="Not authenticated")
            return {"user_id": user_id}

        @app.post("/test/logout")
        def logout(request: Request) -> dict[str, bool]:
            return {"revoked": lifecycle.end(request.session)}

        @app.post("/test/logout-all")
        def logout_all(request: Request) -> dict[str, int]:
            return {
                "revoked": lifecycle.end_all(
                    request.session, request.session["user_id"]
                )
            }

        app.add_middleware(
            HardenedSessionMiddleware,
            secret_key=_SECRET,
            config=self.config,
            store=self.store,
            identity_allowed=lambda email: (
                self.allowed is None or email in self.allowed
            ),
        )
        return app

    def client(self) -> TestClient:
        return TestClient(self.app, base_url=self.base_url)

    def login(self, user_id: str = "user-a") -> tuple[TestClient, str]:
        """Return a logged-in client and the raw session cookie value."""

        client = self.client()
        response = client.get(f"/test/login/{user_id}")
        assert response.status_code == 200
        cookie = _session_set_cookies(response, self.config.cookie.name)
        assert len(cookie) == 1
        return client, cookie[0][0]

    def thief(self, cookie_value: str) -> TestClient:
        """A different client that replays a copied cookie."""

        client = self.client()
        client.cookies.set(self.config.cookie.name, cookie_value)
        return client


@pytest.fixture()
def secure_harness(db_path: str, clock: _Clock) -> _Harness:
    return _Harness(db_path, clock, environ=_PROD_ENV)


@pytest.fixture()
def harness(db_path: str, clock: _Clock) -> _Harness:
    return _Harness(db_path, clock)


def test_login_cookie_is_secure_httponly_lax_host_only_and_bounded(
    secure_harness: _Harness,
) -> None:
    response = secure_harness.client().get("/test/login/user-a")

    ((value, attributes),) = _session_set_cookies(response, "__Host-session")
    assert value
    assert "httponly" in attributes
    assert "secure" in attributes
    assert attributes["samesite"].lower() == "lax"
    assert attributes["path"] == "/"
    assert "domain" not in attributes  # host-only: never shared with subdomains
    assert int(attributes["max-age"]) == DEFAULT_ABSOLUTE_TIMEOUT_SECONDS
    # A response that sets or reads the session must vary on the cookie.
    assert "cookie" in response.headers["vary"].lower()


def test_development_cookie_keeps_httponly_and_samesite_without_secure(
    harness: _Harness,
) -> None:
    response = harness.client().get("/test/login/user-a")

    ((_, attributes),) = _session_set_cookies(response, "session")
    assert "httponly" in attributes
    assert "secure" not in attributes
    assert attributes["samesite"].lower() == "lax"
    assert attributes["path"] == "/"
    assert "domain" not in attributes


def test_cookie_payload_is_only_identity_plus_the_session_identifier(
    harness: _Harness,
) -> None:
    _, cookie = harness.login()

    payload = _decode(cookie)
    assert set(payload) == {"user", "user_id", "sid"}
    assert _SECRET not in cookie


def test_login_rotates_the_identifier_and_discards_pre_login_state(
    harness: _Harness,
) -> None:
    client = harness.client()
    pre_login = _session_set_cookies(client.get("/oauth/start"), "session")[0][0]
    assert "sid" not in _decode(pre_login)
    assert any(key.startswith("_state_google_") for key in _decode(pre_login))

    post_login = _session_set_cookies(client.get("/test/login/user-a"), "session")[0][0]

    assert post_login != pre_login
    payload = _decode(post_login)
    assert payload["user_id"] == "user-a" and payload["sid"]
    assert not any(key.startswith("_state_") for key in payload)  # fixation defence
    # The pre-login cookie an attacker may have planted never authenticates.
    assert harness.thief(pre_login).get("/whoami").status_code == 401


def test_logging_in_again_revokes_the_previous_identifier(harness: _Harness) -> None:
    client, first_cookie = harness.login("user-a")
    old_copy = harness.thief(first_cookie)
    assert old_copy.get("/whoami").status_code == 200

    second_cookie = _session_set_cookies(client.get("/test/login/user-a"), "session")[
        0
    ][0]

    assert _decode(second_cookie)["sid"] != _decode(first_cookie)["sid"]
    assert client.get("/whoami").status_code == 200
    assert old_copy.get("/whoami").status_code == 401  # rotated-away identifier is dead


def test_switching_account_in_the_same_browser_ends_the_previous_users_session(
    harness: _Harness,
) -> None:
    client, cookie_a = harness.login("user-a")
    copy_of_a = harness.thief(cookie_a)

    client.get("/test/login/user-b")

    assert client.get("/whoami").json() == {"user_id": "user-b"}
    assert copy_of_a.get("/whoami").status_code == 401


def test_logout_revokes_server_side_so_a_stolen_copy_is_dead(
    harness: _Harness, db_path: str
) -> None:
    victim, stolen = harness.login()
    attacker = harness.thief(stolen)
    assert attacker.get("/whoami").status_code == 200  # the theft works pre-logout

    response = victim.post("/test/logout")

    assert response.json() == {"revoked": True}
    ((deleted, attributes),) = _session_set_cookies(response, "session")
    assert deleted == "null" and "1970" in attributes["expires"]  # cookie deleted
    assert victim.get("/whoami").status_code == 401
    replay = attacker.get("/whoami")
    assert (
        replay.status_code == 401
    )  # signature + Max-Age still valid; registry says no
    assert (
        _row_count(db_path, "revoked_at IS NOT NULL AND revoked_reason = 'logout'") == 1
    )


def test_logout_all_ends_every_device_of_that_user_and_only_that_user(
    harness: _Harness, db_path: str
) -> None:
    laptop, laptop_cookie = harness.login("user-a")
    phone, _ = harness.login("user-a")
    stolen = harness.thief(laptop_cookie)
    other_tenant, _ = harness.login("user-b")

    response = laptop.post("/test/logout-all")

    # Two sessions existed; the stolen copy shares the laptop's session.
    assert response.json() == {"revoked": 2}
    assert laptop.get("/whoami").status_code == 401
    assert phone.get("/whoami").status_code == 401
    assert stolen.get("/whoami").status_code == 401
    assert other_tenant.get("/whoami").json() == {"user_id": "user-b"}
    assert _row_count(db_path, "user_id = 'user-b' AND revoked_at IS NULL") == 1


def test_logout_one_device_leaves_the_users_other_devices_signed_in(
    harness: _Harness,
) -> None:
    laptop, _ = harness.login("user-a")
    phone, _ = harness.login("user-a")

    laptop.post("/test/logout")

    assert laptop.get("/whoami").status_code == 401
    assert phone.get("/whoami").status_code == 200


def test_idle_session_is_rejected_and_its_cookie_deleted(
    harness: _Harness, clock: _Clock
) -> None:
    client, cookie = harness.login()
    clock.advance(DEFAULT_IDLE_TIMEOUT_SECONDS)

    response = client.get("/whoami")

    assert response.status_code == 401
    ((value, attributes),) = _session_set_cookies(response, "session")
    assert value == "null" and "1970" in attributes["expires"]
    assert harness.thief(cookie).get("/whoami").status_code == 401


def test_activity_refreshes_the_idle_clock_but_not_the_absolute_limit(
    harness: _Harness, clock: _Clock
) -> None:
    client, _ = harness.login()

    for _ in range(DEFAULT_ABSOLUTE_TIMEOUT_SECONDS // _DAY - 1):
        clock.advance(_DAY)
        assert client.get("/whoami").status_code == 200  # daily use never idles out
    clock.advance(_DAY)

    expired = client.get("/whoami")
    assert expired.status_code == 401  # ... yet the absolute limit still ends it
    assert _session_set_cookies(expired, "session")[0][0] == "null"


def test_expiry_is_enforced_even_though_the_cookie_signature_is_still_valid(
    harness: _Harness, clock: _Clock
) -> None:
    _, cookie = harness.login()
    clock.advance(DEFAULT_ABSOLUTE_TIMEOUT_SECONDS + 1)

    # Real time barely moved, so the signed cookie is still cryptographically
    # fine; only the server-side registry can refuse it.
    assert TimestampSigner(_SECRET).unsign(cookie.encode())
    assert harness.thief(cookie).get("/whoami").status_code == 401


def test_correctly_signed_cookie_without_a_registered_session_is_rejected(
    harness: _Harness,
) -> None:
    # Anyone holding WEB_AUTH_SECRET (or a legacy cookie) can sign this payload.
    legacy = signed_session_cookie(_SECRET, user="a@example.test", user_id="user-a")
    guessed = signed_session_cookie(
        _SECRET, user="a@example.test", user_id="user-a", session_id="g" * 43
    )

    assert harness.thief(legacy).get("/whoami").status_code == 401
    assert harness.thief(guessed).get("/whoami").status_code == 401


def test_signed_cookie_cannot_be_retargeted_at_another_tenant(
    harness: _Harness,
) -> None:
    _, victim_cookie = harness.login("user-a")
    sid = _decode(victim_cookie)["sid"]
    other_tenant, _ = harness.login("user-b")

    # Same registered session identifier, but claiming to be user-b.
    swapped = signed_session_cookie(
        _SECRET, user="user-b@example.test", user_id="user-b", session_id=sid
    )

    assert harness.thief(swapped).get("/whoami").status_code == 401
    assert harness.thief(victim_cookie).get("/whoami").json() == {"user_id": "user-a"}
    assert other_tenant.get("/whoami").json() == {"user_id": "user-b"}


def test_tampered_cookie_signature_is_rejected(harness: _Harness) -> None:
    _, cookie = harness.login()
    payload, timestamp, signature = cookie.rsplit(".", 2)

    flipped = "B" if signature[0] == "A" else "A"
    forged = f"{payload}.{timestamp}.{flipped}{signature[1:]}"

    assert harness.thief(forged).get("/whoami").status_code == 401


def test_revoked_session_cannot_reach_mutation_or_authentication_guards(
    harness: _Harness,
) -> None:
    # The guard empties the session before the global guards see it, so a
    # revoked cookie is treated exactly like a logged-out browser there too.
    victim, cookie = harness.login()
    victim.post("/test/logout")

    assert harness.thief(cookie).post("/test/logout").status_code == 401


def test_removing_an_email_from_the_allow_list_ends_its_sessions(
    harness: _Harness, db_path: str
) -> None:
    client, _ = harness.login("user-a")
    harness.allowed = {"user-a@example.test"}
    assert client.get("/whoami").status_code == 200

    harness.allowed = {"someone-else@example.test"}  # operator removes the address

    assert client.get("/whoami").status_code == 401
    assert _row_count(db_path, "revoked_reason = 'access_removed'") == 1
    harness.allowed = {"user-a@example.test"}  # re-adding must not revive it
    assert client.get("/whoami").status_code == 401


def test_registry_outage_fails_closed_without_deleting_the_cookie(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = harness.login()

    def unavailable(*_args: Any, **_kwargs: Any) -> SessionState:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(harness.store, "check", unavailable)
    response = client.get("/whoami")

    assert response.status_code == 503
    assert response.json() == {"detail": "Session service unavailable"}
    assert response.headers["retry-after"] == "5"
    assert not response.headers.get_list(
        "set-cookie"
    )  # a transient error is not a logout
    monkeypatch.undo()
    assert client.get("/whoami").status_code == 200


def test_anonymous_requests_never_touch_the_registry(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*_args: Any, **_kwargs: Any) -> SessionState:
        raise AssertionError("anonymous traffic must not query the registry")

    monkeypatch.setattr(harness.store, "check", must_not_run)
    client = harness.client()

    assert client.get("/oauth/start").status_code == 200
    assert client.get("/whoami").status_code == 401


def test_stack_hooks_recognise_the_hardened_session_middleware() -> None:
    config = load_session_security_config(_DEV_ENV)
    app = FastAPI(title="Workout Agent")
    app.add_middleware(
        HardenedSessionMiddleware,
        secret_key=_SECRET,
        config=config,
        store=SessionStore("unused.db", config.lifetime),
    )

    app.build_middleware_stack()

    assert [middleware.cls for middleware in app.user_middleware] == [
        HardenedSessionMiddleware,
        AuthenticationBoundaryMiddleware,
        MutationSecurityMiddleware,
    ]


# ---------------------------------------------------------------------------
# Real application: Google callback, logout API, logout-all API
# ---------------------------------------------------------------------------


class _Identity:
    """What the mocked Google token exchange reports for the next login."""

    email = "athlete-a@example.test"


@pytest.fixture()
def real_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Any, None, None]:
    db = str(tmp_path / "real-app.db")
    init_db(db)
    for name, value in {
        **_PROD_ENV,
        "DATABASE_PATH": db,
        "ALLOW_ANONYMOUS_WEB": "0",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("SESSION_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("SESSION_ABSOLUTE_TIMEOUT_SECONDS", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    _Identity.email = "athlete-a@example.test"

    async def fake_token_exchange(request: Request) -> dict[str, Any]:
        return {
            "userinfo": {
                "sub": f"google-{_Identity.email.strip().lower()}",
                "email": _Identity.email,
                "email_verified": True,
                "name": "Synthetic Athlete",
            }
        }

    monkeypatch.setattr(
        app_module.oauth.google, "authorize_access_token", fake_token_exchange
    )
    yield app_module
    # Do not leak production-mode module state into later tests.
    monkeypatch.undo()
    importlib.reload(app_module)


def _real_client(app_module: Any) -> TestClient:
    return TestClient(app_module.app, base_url="https://workout.example")


def _sign_in(app_module: Any, email: str) -> tuple[TestClient, str]:
    _Identity.email = email
    client = _real_client(app_module)
    response = client.get("/auth", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/"
    cookies = _session_set_cookies(response, app_module.SESSION_SECURITY.cookie.name)
    assert len(cookies) == 1
    return client, cookies[0][0]


def test_google_callback_issues_a_registered_hardened_session(real_app: Any) -> None:
    client = _real_client(real_app)
    response = client.get("/auth", follow_redirects=False)

    ((value, attributes),) = _session_set_cookies(response, "__Host-session")
    assert {"httponly", "secure"} <= set(attributes)
    assert attributes["samesite"].lower() == "lax"
    assert attributes["path"] == "/"
    assert "domain" not in attributes
    assert response.headers["cache-control"].startswith("private, no-store")
    assert client.get("/api/me").json() == {"user": "athlete-a@example.test"}
    sid = _decode(value)["sid"]
    assert (
        real_app.SESSION_STORE.check(sid, _decode(value)["user_id"])
        is SessionState.ACTIVE
    )
    assert _row_count(real_app.DB_PATH) == 1


def test_callback_rotates_away_leftover_oauth_state_and_old_identifiers(
    real_app: Any,
) -> None:
    # A genuine pre-login cookie only carries OAuth state (no identity keys).
    pre_login = (
        TimestampSigner(_SECRET)
        .sign(base64.b64encode(json.dumps({"_state_google_x": {"data": {}}}).encode()))
        .decode()
    )

    client = _real_client(real_app)
    client.cookies.set("__Host-session", pre_login)
    response = client.get("/auth", follow_redirects=False)

    fresh = _session_set_cookies(response, "__Host-session")[0][0]
    assert not any(key.startswith("_state_") for key in _decode(fresh))
    assert _decode(fresh)["sid"]
    assert _real_client_with(real_app, pre_login).get("/api/me").status_code == 401

    again = _session_set_cookies(
        client.get("/auth", follow_redirects=False), "__Host-session"
    )
    assert _decode(again[0][0])["sid"] != _decode(fresh)["sid"]
    assert _real_client_with(real_app, fresh).get("/api/me").status_code == 401


def _real_client_with(app_module: Any, cookie_value: str) -> TestClient:
    client = _real_client(app_module)
    client.cookies.set(app_module.SESSION_SECURITY.cookie.name, cookie_value)
    return client


def test_api_logout_revokes_the_session_and_a_stolen_copy_stops_working(
    real_app: Any,
) -> None:
    victim, cookie = _sign_in(real_app, "athlete-a@example.test")
    attacker = _real_client_with(real_app, cookie)
    assert attacker.get("/api/me").status_code == 200

    response = victim.post("/api/auth/logout")

    assert response.status_code == 200 and response.json() == {"status": "ok"}
    ((deleted, _),) = _session_set_cookies(response, "__Host-session")
    assert deleted == "null"
    assert victim.get("/api/me").status_code == 401
    assert attacker.get("/api/me").status_code == 401
    assert attacker.get("/api/settings").status_code == 401


def test_api_logout_all_ends_every_device_for_the_authenticated_user_only(
    real_app: Any,
) -> None:
    laptop, laptop_cookie = _sign_in(real_app, "athlete-a@example.test")
    phone, _ = _sign_in(real_app, "athlete-a@example.test")
    stolen = _real_client_with(real_app, laptop_cookie)
    other_user, _ = _sign_in(real_app, "athlete-b@example.test")

    response = laptop.post("/api/auth/logout-all")

    assert response.status_code == 200
    # Two sessions existed; the stolen copy shares the laptop's session.
    assert response.json() == {"status": "ok", "revoked": 2}
    assert laptop.get("/api/me").status_code == 401
    assert phone.get("/api/me").status_code == 401
    assert stolen.get("/api/me").status_code == 401
    assert other_user.get("/api/me").json() == {"user": "athlete-b@example.test"}


def test_logout_all_ignores_client_supplied_owner_identifiers(real_app: Any) -> None:
    attacker, _ = _sign_in(real_app, "athlete-a@example.test")
    victim, _ = _sign_in(real_app, "athlete-b@example.test")
    victim_id = get_or_create_user("athlete-b@example.test", db_path=real_app.DB_PATH)[
        "id"
    ]

    response = attacker.post("/api/auth/logout-all", json={"user_id": victim_id})

    assert response.status_code == 404  # contradictory owner claim, non-enumerating
    assert victim.get("/api/me").json() == {"user": "athlete-b@example.test"}
    assert attacker.get("/api/me").status_code == 200  # nothing was revoked


@pytest.mark.parametrize("path", ["/api/auth/logout", "/api/auth/logout-all"])
def test_logout_endpoints_require_a_session(real_app: Any, path: str) -> None:
    survivor, _ = _sign_in(real_app, "athlete-a@example.test")

    response = _real_client(real_app).post(path)

    assert response.status_code == 401
    assert survivor.get("/api/me").status_code == 200


def test_expired_session_is_rejected_by_the_real_application(real_app: Any) -> None:
    client, cookie = _sign_in(real_app, "athlete-a@example.test")
    lifetime = real_app.SESSION_SECURITY.lifetime
    real_app.SESSION_STORE.clock = lambda: time.time() + lifetime.idle_timeout + 5

    response = client.get("/api/me")

    assert response.status_code == 401
    assert _session_set_cookies(response, "__Host-session")[0][0] == "null"
    assert _real_client_with(real_app, cookie).get("/api/settings").status_code == 401


def test_removed_allow_list_entry_ends_the_real_session(
    real_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _sign_in(real_app, "athlete-a@example.test")
    monkeypatch.setattr(real_app, "ALLOWED_EMAILS", ["athlete-b@example.test"])

    assert client.get("/api/me").status_code == 401


@pytest.mark.parametrize("fetch_site", ["cross-site", "same-site", "bogus"])
def test_legacy_get_logout_ignores_navigations_from_other_sites(
    real_app: Any, fetch_site: str
) -> None:
    client, _ = _sign_in(real_app, "athlete-a@example.test")

    response = client.get(
        "/logout", headers={"Sec-Fetch-Site": fetch_site}, follow_redirects=False
    )

    # A hostile page (or a sibling subdomain) cannot sign the user out by linking.
    assert response.headers["location"] == "/"
    assert client.get("/api/me").status_code == 200


@pytest.mark.parametrize(
    "headers", [{"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {}]
)
def test_legacy_get_logout_still_signs_out_user_initiated_navigation(
    real_app: Any, headers: dict[str, str]
) -> None:
    client, cookie = _sign_in(real_app, "athlete-a@example.test")

    response = client.get("/logout", headers=headers, follow_redirects=False)

    assert response.status_code == 307 and response.headers["location"] == "/login"
    assert _real_client_with(real_app, cookie).get("/api/me").status_code == 401


def test_secure_entrypoint_lets_a_revoked_cookie_neither_mint_nor_spend_csrf_tokens(
    real_app: Any,
) -> None:
    # Import the production entrypoint fresh so it binds to the reloaded app.
    sys.modules.pop("webapp.secure_app", None)
    try:
        secure_app = importlib.import_module("webapp.secure_app")
        victim = TestClient(secure_app.app, base_url="https://workout.example")
        login = victim.get("/auth", follow_redirects=False)
        cookie = _session_set_cookies(login, "__Host-session")[0][0]
        attacker = TestClient(secure_app.app, base_url="https://workout.example")
        attacker.cookies.set("__Host-session", cookie)

        # The CSRF layer reads the hardened cookie name and accepts a live session.
        stolen_token = attacker.get("/api/csrf-token")
        assert stolen_token.status_code == 200

        logout_token = victim.get("/api/csrf-token").json()["token"]
        logout = victim.post("/api/auth/logout", headers={"X-CSRF-Token": logout_token})
        assert logout.status_code == 200

        assert attacker.get("/api/csrf-token").status_code == 401
        spend = attacker.post(
            "/api/auth/logout-all",
            headers={"X-CSRF-Token": stolen_token.json()["token"]},
        )
        assert spend.status_code == 401
    finally:
        sys.modules.pop("webapp.secure_app", None)


def test_session_limits_are_documented_and_registered_as_server_settings() -> None:
    root = Path(__file__).resolve().parents[2]
    for variable in (
        "SESSION_IDLE_TIMEOUT_SECONDS",
        "SESSION_ABSOLUTE_TIMEOUT_SECONDS",
    ):
        assert variable in _SERVER_WIDE_ENV_VARS
        for env_file in (".env.example", "backend/.env.example"):
            assert f"{variable}=" in (root / env_file).read_text(encoding="utf-8")
        assert variable in (root / "docs/SESSION_SECURITY.md").read_text(
            encoding="utf-8"
        )
        assert variable in (root / "docker-compose.portainer.yml").read_text(
            encoding="utf-8"
        )
