"""AI connection tests through the web API: auth, tenancy, persistence, limits."""

from __future__ import annotations

import importlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import ai_provider
import database
import openai
import pytest
from ai_connection import AIConnectionChecker
from ai_provider import PROVIDERS, AIProvider
from cryptography.fernet import Fernet
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

pytest.importorskip("fastapi")

_SECRET = "ai-connection-routes-secret-that-is-long-enough"
KEY = "synthetic-key-0123456789abcdef"
OTHER_KEY = "synthetic-other-key-abcdef0123"
MODEL = "claude-sonnet-5-5"


class _Clock:
    def __init__(self) -> None:
        self.now = 5_000.0

    def __call__(self) -> float:
        return self.now


class _Script:
    """Scripted provider adapters registered for every provider id."""

    def __init__(self) -> None:
        self.outcomes: list[BaseException | None] = []
        self.built: list[tuple[str, str]] = []

    def factory(self, *, api_key: str, model: str) -> AIProvider:
        self.built.append((api_key, model))
        script = self

        class _Scripted(AIProvider):
            def generate(self, prompt: str, *, stream: bool = False) -> str:
                return "ok"

            def name(self) -> str:
                return "scripted"

            def _probe(self, timeout: float) -> None:
                outcome = script.outcomes.pop(0) if script.outcomes else None
                if outcome is not None:
                    raise outcome

        return _Scripted()


@dataclass
class _Env:
    app: Any
    db: str
    script: _Script
    clock: _Clock
    user_a: str
    user_b: str
    client_a: TestClient
    client_b: TestClient
    anonymous: TestClient


@pytest.fixture()
def env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> _Env:
    db = str(tmp_path / "ai-connection.db")
    database.init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "client-id.example.test")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())

    import webapp.app as app_module

    importlib.reload(app_module)

    script = _Script()
    for provider_id, spec in PROVIDERS.items():
        monkeypatch.setitem(
            ai_provider.PROVIDERS,
            provider_id,
            {"class": script.factory, "default_model": spec["default_model"]},
        )
    clock = _Clock()
    monkeypatch.setattr(
        app_module,
        "ai_connection_checker",
        AIConnectionChecker(
            clock=clock,
            now=lambda: datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc),
            max_live_checks=50,
        ),
    )

    user_a = database.get_or_create_user("athlete-a@example.test", db_path=db)["id"]
    user_b = database.get_or_create_user("athlete-b@example.test", db_path=db)["id"]
    session_a = app_module.SESSION_STORE.create(user_a)
    session_b = app_module.SESSION_STORE.create(user_b)
    client_a = TestClient(app_module.app)
    client_a.cookies.set(
        "session",
        signed_session_cookie(
            _SECRET,
            user="athlete-a@example.test",
            user_id=user_a,
            session_id=session_a,
        ),
    )
    client_b = TestClient(app_module.app)
    client_b.cookies.set(
        "session",
        signed_session_cookie(
            _SECRET,
            user="athlete-b@example.test",
            user_id=user_b,
            session_id=session_b,
        ),
    )
    return _Env(
        app=app_module,
        db=db,
        script=script,
        clock=clock,
        user_a=user_a,
        user_b=user_b,
        client_a=client_a,
        client_b=client_b,
        anonymous=TestClient(app_module.app),
    )


def _provider_error(cls: type[Exception], status: int, **kw: Any) -> Exception:
    """A real SDK status error whose message quotes the key, like vendors can."""
    sdk = importlib.import_module(cls.__module__.split(".")[0])
    flavour = importlib.import_module(
        sdk.DefaultHttpxClient.__mro__[1].__module__.split(".")[0],
    )
    request = flavour.Request("POST", "https://provider.invalid/v1/chat")
    response = flavour.Response(
        status,
        request=request,
        json=kw.get("body") or {},
        headers=kw.get("headers"),
    )
    return cls(f"vendor echoed {KEY}", response=response, body=kw.get("body"))


def _stored(env: _Env, user_id: str, provider: str = "claude") -> dict[str, Any] | None:
    return database.get_user_api_key(user_id, provider, db_path=env.db)


def _raw_key_rows(env: _Env) -> list[tuple[str, str, str]]:
    with sqlite3.connect(env.db) as connection:
        return connection.execute(
            "SELECT user_id, provider, api_key FROM user_api_keys",
        ).fetchall()


# ---------------------------------------------------------------------------
# POST /api/settings/ai/test
# ---------------------------------------------------------------------------


def test_test_endpoint_requires_authentication(env: _Env) -> None:
    response = env.anonymous.post(
        "/api/settings/ai/test",
        json={"provider": "claude", "api_key": KEY},
    )

    assert response.status_code == 401
    assert env.script.built == []


def test_candidate_key_is_verified_without_being_saved(env: _Env) -> None:
    response = env.client_a.post(
        "/api/settings/ai/test",
        json={"provider": "claude", "api_key": KEY, "model": MODEL},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["code"] == "ok"
    assert body["provider"] == "claude" and body["model"] == MODEL
    assert body["cached"] is False
    assert KEY not in response.text
    assert env.script.built == [(KEY, MODEL)]
    # Nothing was persisted for anyone.
    assert _raw_key_rows(env) == []
    assert _stored(env, env.user_a) is None


@pytest.mark.parametrize(
    ("make_error", "code", "fragment"),
    [
        (
            lambda: _provider_error(openai.AuthenticationError, 401),
            "invalid_credentials",
            "rejected the API key",
        ),
        (
            lambda: _provider_error(openai.NotFoundError, 404),
            "model_not_found",
            "does not offer a model",
        ),
        (
            lambda: _provider_error(openai.PermissionDeniedError, 403),
            "permission_denied",
            "refused access",
        ),
        (
            lambda: _provider_error(
                openai.RateLimitError, 429, body={"code": "insufficient_quota"}
            ),
            "quota_exceeded",
            "no credit or quota",
        ),
        (
            lambda: _provider_error(openai.InternalServerError, 500),
            "provider_unavailable",
            "unavailable",
        ),
    ],
)
def test_provider_failures_become_actionable_non_secret_messages(
    env: _Env,
    make_error: Callable[[], Exception],
    code: str,
    fragment: str,
) -> None:
    env.script.outcomes = [make_error()]

    response = env.client_a.post(
        "/api/settings/ai/test",
        json={"provider": "openai", "api_key": KEY, "model": "gpt-4o"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error" and body["code"] == code
    assert fragment in body["message"]
    assert KEY not in response.text and "vendor echoed" not in response.text
    assert _raw_key_rows(env) == []


def test_omitted_key_tests_only_the_callers_own_stored_key(env: _Env) -> None:
    database.save_user_api_key(env.user_b, "claude", OTHER_KEY, db_path=env.db)

    stranger = env.client_a.post("/api/settings/ai/test", json={"provider": "claude"})
    owner = env.client_b.post("/api/settings/ai/test", json={"provider": "claude"})

    assert stranger.json()["code"] == "missing_credentials"
    assert owner.json()["status"] == "ok"
    # Only B's own key ever reached an adapter, and A never saw it.
    assert env.script.built == [(OTHER_KEY, PROVIDERS["claude"]["default_model"])]
    assert OTHER_KEY not in stranger.text + owner.text


def test_a_typed_key_wins_over_the_stored_one_and_is_not_stored(env: _Env) -> None:
    database.save_user_api_key(env.user_a, "claude", OTHER_KEY, db_path=env.db)

    env.client_a.post("/api/settings/ai/test", json={"provider": "claude", "api_key": KEY})

    assert env.script.built[0][0] == KEY
    assert _stored(env, env.user_a)["api_key"] == OTHER_KEY


@pytest.mark.parametrize(
    ("provider", "model", "code"),
    [
        ("claude", "claude-sonnet-4-20250514", "model_retired"),
        ("gemini", "gemini-2.5-pro", "model_deprecated"),
        ("openai", "text-embedding-3-small", "model_unsupported"),
        ("deepseek", "deepseek-chat", "model_retired"),
        ("openai", "gpt 4o", "model_invalid"),
    ],
)
def test_unusable_models_are_rejected_without_calling_the_provider(
    env: _Env,
    provider: str,
    model: str,
    code: str,
) -> None:
    response = env.client_a.post(
        "/api/settings/ai/test",
        json={"provider": provider, "api_key": KEY, "model": model},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "error" and body["code"] == code
    assert env.script.built == []
    if code in {"model_retired", "model_deprecated", "model_unsupported"}:
        assert body["replacement_model"] == PROVIDERS[provider]["default_model"]


def test_blank_model_tests_the_registry_default(env: _Env) -> None:
    for provider_id, spec in PROVIDERS.items():
        env.client_a.post(
            "/api/settings/ai/test",
            json={"provider": provider_id, "api_key": KEY, "model": "  "},
        )
        assert env.script.built[-1] == (KEY, spec["default_model"])


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "nonsense", "api_key": KEY},
        {"api_key": KEY},
        {"provider": 7, "api_key": KEY},
        {"provider": "claude", "api_key": 123},
        {"provider": "claude", "api_key": KEY, "model": 5},
        {"provider": "claude", "api_key": KEY, "model": "m" * 201},
    ],
)
def test_malformed_requests_are_400_and_never_echo_the_key(
    env: _Env,
    payload: dict[str, Any],
) -> None:
    response = env.client_a.post("/api/settings/ai/test", json=payload)

    assert response.status_code == 400
    assert KEY not in response.text
    assert env.script.built == []


@pytest.mark.parametrize("raw", ["not json", "[]", '"text"', "null"])
def test_non_object_bodies_are_400_not_500(env: _Env, raw: str) -> None:
    response = env.client_a.post(
        "/api/settings/ai/test",
        content=raw,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400


def test_owner_claims_in_the_body_cannot_redirect_the_check(env: _Env) -> None:
    database.save_user_api_key(env.user_b, "claude", OTHER_KEY, db_path=env.db)

    response = env.client_a.post(
        "/api/settings/ai/test",
        json={"provider": "claude", "user_id": env.user_b},
    )

    assert response.status_code == 404
    assert env.script.built == []


def test_results_are_cached_briefly_and_revalidated_after_changes(env: _Env) -> None:
    def test(**fields: Any) -> dict[str, Any]:
        payload = {"provider": "claude", "api_key": KEY, "model": MODEL, **fields}
        return env.client_a.post("/api/settings/ai/test", json=payload).json()

    first = test()
    repeat = test()
    new_model = test(model="claude-opus-4-8")
    new_key = test(api_key=OTHER_KEY)
    env.clock.now += 301
    expired = test()

    assert [r["cached"] for r in (first, repeat, new_model, new_key, expired)] == [
        False,
        True,
        False,
        False,
        False,
    ]
    assert repeat["checked_at"] == first["checked_at"]
    assert len(env.script.built) == 4


def test_cached_success_is_not_shared_with_another_tenant(env: _Env) -> None:
    body = {"provider": "claude", "api_key": KEY, "model": MODEL}

    env.client_a.post("/api/settings/ai/test", json=body)
    other = env.client_b.post("/api/settings/ai/test", json=body).json()

    assert other["cached"] is False
    assert len(env.script.built) == 2


def test_live_checks_are_rate_limited_per_user(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        env.app,
        "ai_connection_checker",
        AIConnectionChecker(clock=env.clock, max_live_checks=2, window=60),
    )

    def probe(client: TestClient, key: str) -> Any:
        return client.post(
            "/api/settings/ai/test",
            json={"provider": "claude", "api_key": key, "model": MODEL},
        )

    assert probe(env.client_a, KEY + "1").status_code == 200
    assert probe(env.client_a, KEY + "2").status_code == 200
    limited = probe(env.client_a, KEY + "3")

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert "Wait 60 seconds" in limited.json()["detail"]
    # Verified results, static rejections and other tenants are unaffected.
    assert probe(env.client_a, KEY + "1").json()["cached"] is True
    retired = env.client_a.post(
        "/api/settings/ai/test",
        json={"provider": "claude", "api_key": KEY, "model": "claude-2.1"},
    )
    assert retired.status_code == 200
    assert probe(env.client_b, KEY + "3").status_code == 200
    assert len(env.script.built) == 3


def test_requests_from_one_address_are_also_throttled(env: _Env) -> None:
    statuses = [
        env.client_a.post(
            "/api/settings/ai/test",
            json={"provider": "claude", "api_key": KEY, "model": MODEL},
        ).status_code
        for _ in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


# ---------------------------------------------------------------------------
# POST /api/settings/key (saving verifies first)
# ---------------------------------------------------------------------------


def _save(client: TestClient, **fields: Any) -> Any:
    payload = {"provider": "claude", "api_key": KEY, "model": MODEL, **fields}
    return client.post("/api/settings/key", json=payload)


def test_valid_key_is_verified_then_saved_encrypted(env: _Env) -> None:
    response = _save(env.client_a)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["provider"] == "claude"
    assert env.script.built == [(KEY, MODEL)]
    record = _stored(env, env.user_a)
    assert record is not None and record["api_key"] == KEY
    assert record["extra"] == {"model": MODEL}
    [(owner, provider, at_rest)] = _raw_key_rows(env)
    assert (owner, provider) == (env.user_a, "claude")
    assert at_rest != KEY and KEY not in at_rest


@pytest.mark.parametrize(
    ("make_error", "status", "code"),
    [
        (lambda: _provider_error(openai.AuthenticationError, 401), 422, "invalid_credentials"),
        (lambda: _provider_error(openai.NotFoundError, 404), 422, "model_not_found"),
        (lambda: _provider_error(openai.PermissionDeniedError, 403), 422, "permission_denied"),
        (lambda: _provider_error(openai.InternalServerError, 500), 503, "provider_unavailable"),
        (
            lambda: _provider_error(
                openai.RateLimitError, 429, headers={"retry-after": "9"}
            ),
            429,
            "rate_limited",
        ),
    ],
)
def test_an_invalid_key_is_refused_and_never_persisted(
    env: _Env,
    make_error: Callable[[], Exception],
    status: int,
    code: str,
) -> None:
    env.script.outcomes = [make_error()]

    response = _save(env.client_a)

    assert response.status_code == status
    body = response.json()
    assert body["code"] == code and body["status"] == "error"
    assert isinstance(body["detail"], str)
    assert body["detail"].endswith("Your key was not saved.")
    assert KEY not in response.text and "vendor echoed" not in response.text
    # Neither plaintext nor ciphertext was written.
    assert _raw_key_rows(env) == []
    assert _stored(env, env.user_a) is None
    if code == "rate_limited":
        assert response.headers["retry-after"] == "9"


def test_a_failed_update_keeps_the_previously_verified_key(env: _Env) -> None:
    assert _save(env.client_a).status_code == 200
    env.script.outcomes = [_provider_error(openai.AuthenticationError, 401)]

    refused = _save(env.client_a, api_key=OTHER_KEY, model="claude-opus-4-8")

    assert refused.status_code == 422
    record = _stored(env, env.user_a)
    assert record["api_key"] == KEY
    assert record["extra"] == {"model": MODEL}


def test_deprecated_and_unsupported_models_are_refused_before_any_provider_call(
    env: _Env,
) -> None:
    retired = _save(env.client_a, model="claude-sonnet-4-20250514")
    unsupported = _save(
        env.client_a, provider="openai", model="whisper-1", api_key=OTHER_KEY
    )

    assert retired.status_code == 422 and retired.json()["code"] == "model_retired"
    assert unsupported.status_code == 422
    assert unsupported.json()["code"] == "model_unsupported"
    assert PROVIDERS["claude"]["default_model"] in retired.json()["detail"]
    assert env.script.built == []
    assert _raw_key_rows(env) == []


def test_blank_model_is_verified_against_the_registry_default(env: _Env) -> None:
    response = env.client_a.post(
        "/api/settings/key",
        json={"provider": "deepseek", "api_key": KEY},
    )

    assert response.status_code == 200
    assert env.script.built == [(KEY, PROVIDERS["deepseek"]["default_model"])]


def test_a_check_just_run_from_the_test_button_is_reused_by_save(env: _Env) -> None:
    env.client_a.post(
        "/api/settings/ai/test",
        json={"provider": "claude", "api_key": KEY, "model": MODEL},
    )

    assert _save(env.client_a).status_code == 200
    assert len(env.script.built) == 1


def test_save_is_throttled_per_user_without_persisting(env: _Env, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        env.app,
        "ai_connection_checker",
        AIConnectionChecker(clock=env.clock, max_live_checks=1, window=60),
    )

    assert _save(env.client_a).status_code == 200
    second = _save(env.client_a, api_key=OTHER_KEY)

    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"
    assert _stored(env, env.user_a)["api_key"] == KEY


def test_saving_keeps_each_tenants_key_separate(env: _Env) -> None:
    _save(env.client_a)
    _save(env.client_b, api_key=OTHER_KEY)

    assert _stored(env, env.user_a)["api_key"] == KEY
    assert _stored(env, env.user_b)["api_key"] == OTHER_KEY


def test_deleting_a_key_forgets_its_cached_verification(env: _Env) -> None:
    assert _save(env.client_a).status_code == 200
    assert env.client_a.post("/api/settings/key/delete", json={"provider": "claude"}).status_code == 200

    again = env.client_a.post(
        "/api/settings/ai/test",
        json={"provider": "claude", "api_key": KEY, "model": MODEL},
    ).json()

    assert again["cached"] is False
    assert len(env.script.built) == 2


# ---------------------------------------------------------------------------
# POST /api/settings/preferences (activation)
# ---------------------------------------------------------------------------


def _prefs(env: _Env) -> dict[str, Any]:
    return database.get_user_preferences(env.user_a, env.db)


def _activate(env: _Env, **fields: Any) -> Any:
    return env.client_a.post("/api/settings/preferences", json=fields)


def test_changing_the_model_of_a_stored_key_is_verified_before_activation(
    env: _Env,
) -> None:
    database.save_user_api_key(env.user_a, "claude", KEY, db_path=env.db)

    response = _activate(env, preferred_ai="claude", ai_model="claude-opus-4-8")

    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert env.script.built == [(KEY, "claude-opus-4-8")]
    assert _prefs(env)["preferred_ai"] == "claude"
    assert _prefs(env)["ai_model"] == "claude-opus-4-8"


def test_activation_of_a_model_the_key_cannot_use_saves_nothing(env: _Env) -> None:
    database.save_user_api_key(env.user_a, "claude", KEY, db_path=env.db)
    env.script.outcomes = [_provider_error(openai.NotFoundError, 404)]
    before = _prefs(env)

    response = _activate(
        env, preferred_ai="claude", ai_model="claude-typo-9", goals=["strength"]
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "model_not_found"
    assert body["detail"].endswith("Your AI settings were not saved.")
    # Atomic: the other fields in the request were not applied either.
    assert _prefs(env) == before
    assert _prefs(env)["preferred_ai"] == "gemini"


def test_deprecated_model_cannot_be_activated(env: _Env) -> None:
    response = _activate(env, preferred_ai="claude", ai_model="claude-3-5-sonnet-20241022")

    assert response.status_code == 422
    assert response.json()["code"] == "model_retired"
    assert env.script.built == []
    assert _prefs(env)["preferred_ai"] == "gemini"


def test_unrelated_profile_edits_never_spend_a_provider_call(env: _Env) -> None:
    database.save_user_api_key(env.user_a, "claude", KEY, db_path=env.db)
    database.save_user_preferences(
        env.user_a, preferred_ai="claude", ai_model=MODEL, db_path=env.db
    )

    same_selection = _activate(
        env, preferred_ai="claude", ai_model=MODEL, goals=["power"]
    )
    no_selection = _activate(env, goals=["strength"])

    assert same_selection.status_code == 200 and no_selection.status_code == 200
    assert env.script.built == []
    assert _prefs(env)["goals"] == ["strength"]


def test_an_already_stored_deprecated_model_does_not_block_unrelated_edits(
    env: _Env,
) -> None:
    database.save_user_preferences(
        env.user_a,
        preferred_ai="deepseek",
        ai_model="deepseek-chat",
        db_path=env.db,
    )

    response = _activate(
        env, preferred_ai="deepseek", ai_model="deepseek-chat", goals=["power"]
    )

    assert response.status_code == 200
    assert _prefs(env)["goals"] == ["power"]
    assert env.script.built == []


def test_activation_without_a_stored_key_applies_the_static_policy_only(
    env: _Env,
) -> None:
    ok = _activate(env, preferred_ai="openai")
    bad = _activate(env, preferred_ai="openai", ai_model="gpt-4-turbo")

    assert ok.status_code == 200
    assert bad.status_code == 422 and bad.json()["code"] == "model_retired"
    assert env.script.built == []
    assert _prefs(env)["ai_model"] == PROVIDERS["openai"]["default_model"]


def test_model_alone_applies_to_the_currently_preferred_provider(env: _Env) -> None:
    database.save_user_preferences(
        env.user_a, preferred_ai="claude", ai_model=MODEL, db_path=env.db
    )
    database.save_user_api_key(env.user_a, "claude", KEY, db_path=env.db)

    response = _activate(env, ai_model="claude-opus-4-8")

    assert response.status_code == 200
    assert env.script.built == [(KEY, "claude-opus-4-8")]


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_no_flow_logs_the_key_or_provider_text(
    env: _Env,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env.script.outcomes = [_provider_error(openai.AuthenticationError, 401)]

    with caplog.at_level(logging.DEBUG):
        _save(env.client_a)
        env.client_a.post(
            "/api/settings/ai/test",
            json={"provider": "claude", "api_key": OTHER_KEY, "model": MODEL},
        )

    assert KEY not in caplog.text and OTHER_KEY not in caplog.text
    assert "vendor echoed" not in caplog.text


def test_settings_page_still_masks_the_verified_key(env: _Env) -> None:
    _save(env.client_a)

    payload = env.client_a.get("/api/settings").json()

    assert payload["user_keys"]["claude"]["has_key"] is True
    assert payload["user_keys"]["claude"]["masked"].endswith(KEY[-4:])
    assert KEY not in json.dumps(payload)
