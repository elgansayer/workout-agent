"""Provider error mapping, bounded probes and the rate-limited connection checker."""

from __future__ import annotations

import importlib
import json
import logging
import sys
from collections.abc import Callable
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import ai_provider
import anthropic
import openai
import pytest
from ai_connection import (
    AIConnectionChecker,
    ConnectionCheckRateLimited,
    describe,
    http_status_for,
)
from ai_provider import (
    PROBE_MAX_TOKENS,
    PROBE_PROMPT,
    PROBE_TIMEOUT_SECONDS,
    PROVIDERS,
    AIProvider,
    AIProviderError,
    AIProviderErrorCode,
    ClaudeProvider,
    DeepSeekProvider,
    GeminiProvider,
    OpenAIProvider,
    classify_provider_error,
)
from google.api_core import exceptions as google_exceptions

Code = AIProviderErrorCode

# Synthetic credentials only. KEY must never appear in any output.
KEY = "synthetic-key-0123456789abcdef"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
MODEL = "claude-sonnet-5-5"


# ---------------------------------------------------------------------------
# Provider error classification (real SDK exception types, no network)
# ---------------------------------------------------------------------------


def _flavour(sdk: Any) -> Any:
    """The httpx flavour (``httpx`` or ``httpx2``) this SDK release builds on."""
    base = sdk.DefaultHttpxClient.__mro__[1]
    return importlib.import_module(base.__module__.split(".")[0])


def _request(sdk: Any = openai) -> Any:
    return _flavour(sdk).Request("POST", "https://provider.invalid/v1/chat")


def _status_error(
    cls: type[Exception],
    status: int,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> Exception:
    sdk = sys.modules[cls.__module__.split(".")[0]]
    response = _flavour(sdk).Response(
        status,
        request=_request(sdk),
        json=body if body is not None else {},
        headers=headers,
    )
    # The message deliberately quotes the key, as real SDK errors can.
    return cls(f"upstream said no for {KEY}", response=response, body=body)


CLASSIFICATION_CASES: list[tuple[str, Callable[[], BaseException], Code]] = [
    ("openai-401", lambda: _status_error(openai.AuthenticationError, 401), Code.INVALID_CREDENTIALS),
    ("openai-403", lambda: _status_error(openai.PermissionDeniedError, 403), Code.PERMISSION_DENIED),
    ("openai-404", lambda: _status_error(openai.NotFoundError, 404), Code.MODEL_NOT_FOUND),
    (
        "openai-404-not-chat",
        lambda: _status_error(
            openai.NotFoundError,
            404,
            body={
                "message": "This is not a chat model and thus not supported in the "
                "v1/chat/completions endpoint.",
            },
        ),
        Code.MODEL_UNSUPPORTED,
    ),
    ("openai-400", lambda: _status_error(openai.BadRequestError, 400), Code.INVALID_REQUEST),
    ("openai-429", lambda: _status_error(openai.RateLimitError, 429), Code.RATE_LIMITED),
    (
        "openai-429-insufficient-quota",
        lambda: _status_error(
            openai.RateLimitError,
            429,
            body={"code": "insufficient_quota", "message": "You exceeded your quota"},
        ),
        Code.QUOTA_EXCEEDED,
    ),
    # DeepSeek signals an empty balance with 402 through the same SDK.
    ("deepseek-402", lambda: _status_error(openai.APIStatusError, 402), Code.QUOTA_EXCEEDED),
    ("openai-500", lambda: _status_error(openai.InternalServerError, 500), Code.PROVIDER_UNAVAILABLE),
    ("openai-timeout", lambda: openai.APITimeoutError(request=_request(openai)), Code.TIMEOUT),
    ("openai-connection", lambda: openai.APIConnectionError(request=_request(openai)), Code.PROVIDER_UNAVAILABLE),
    ("anthropic-401", lambda: _status_error(anthropic.AuthenticationError, 401), Code.INVALID_CREDENTIALS),
    ("anthropic-403", lambda: _status_error(anthropic.PermissionDeniedError, 403), Code.PERMISSION_DENIED),
    ("anthropic-404", lambda: _status_error(anthropic.NotFoundError, 404), Code.MODEL_NOT_FOUND),
    ("anthropic-429", lambda: _status_error(anthropic.RateLimitError, 429), Code.RATE_LIMITED),
    (
        "anthropic-empty-balance",
        lambda: _status_error(
            anthropic.BadRequestError,
            400,
            body={"error": {"message": "Your credit balance is too low to access the API"}},
        ),
        Code.QUOTA_EXCEEDED,
    ),
    ("anthropic-529", lambda: _status_error(anthropic.APIStatusError, 529), Code.PROVIDER_UNAVAILABLE),
    ("anthropic-timeout", lambda: anthropic.APITimeoutError(request=_request(anthropic)), Code.TIMEOUT),
    (
        "gemini-bad-key",
        lambda: google_exceptions.InvalidArgument(
            f"API key not valid. Please pass a valid API key. ({KEY})",
        ),
        Code.INVALID_CREDENTIALS,
    ),
    ("gemini-401", lambda: google_exceptions.Unauthenticated("no"), Code.INVALID_CREDENTIALS),
    ("gemini-403", lambda: google_exceptions.PermissionDenied("no"), Code.PERMISSION_DENIED),
    ("gemini-404", lambda: google_exceptions.NotFound("no"), Code.MODEL_NOT_FOUND),
    ("gemini-429", lambda: google_exceptions.ResourceExhausted("no"), Code.RATE_LIMITED),
    ("gemini-503", lambda: google_exceptions.ServiceUnavailable("no"), Code.PROVIDER_UNAVAILABLE),
    ("gemini-504", lambda: google_exceptions.DeadlineExceeded("no"), Code.TIMEOUT),
    ("builtin-timeout", lambda: TimeoutError(KEY), Code.TIMEOUT),
    ("builtin-connection", lambda: ConnectionError(KEY), Code.PROVIDER_UNAVAILABLE),
    ("missing-sdk", lambda: ImportError(KEY), Code.SDK_UNAVAILABLE),
    ("unrecognised", lambda: RuntimeError(f"surprise {KEY}"), Code.UNKNOWN),
]


@pytest.mark.parametrize(
    ("make_error", "expected"),
    [case[1:] for case in CLASSIFICATION_CASES],
    ids=[case[0] for case in CLASSIFICATION_CASES],
)
def test_provider_errors_map_to_stable_secret_free_codes(
    make_error: Callable[[], BaseException],
    expected: Code,
) -> None:
    error = classify_provider_error(make_error())

    assert error.code == expected
    # Only the code survives: no provider text, no chained original exception.
    assert str(error) == expected.value
    assert KEY not in repr(error) and KEY not in str(error.args)
    assert error.__cause__ is None


def test_retry_after_is_carried_and_clamped() -> None:
    plain = classify_provider_error(
        _status_error(openai.RateLimitError, 429, headers={"retry-after": "7"}),
    )
    huge = classify_provider_error(
        _status_error(openai.RateLimitError, 429, headers={"retry-after": "86400"}),
    )
    junk = classify_provider_error(
        _status_error(openai.RateLimitError, 429, headers={"retry-after": "tomorrow"}),
    )

    assert plain.retry_after == 7 and plain.retryable
    assert huge.retry_after == 300
    assert junk.retry_after is None


def test_classifier_is_idempotent_and_never_raises_on_odd_exceptions() -> None:
    class _BrokenStr(Exception):
        def __str__(self) -> str:
            raise RuntimeError("cannot render")

    original = AIProviderError(Code.TIMEOUT)
    assert classify_provider_error(original) is original
    assert classify_provider_error(_BrokenStr()).code == Code.UNKNOWN


# ---------------------------------------------------------------------------
# Adapter probes: real SDK clients over an in-memory HTTP transport
# ---------------------------------------------------------------------------

_CLAUDE_OK = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": MODEL,
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 5, "output_tokens": 1},
}
_OPENAI_OK = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-4o",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        },
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}


def _transport(
    sdk: Any,
    requests: list[Any],
    status: int,
    body: Any,
) -> Any:
    flavour = _flavour(sdk)

    def handler(request: Any) -> Any:
        requests.append(request)
        return flavour.Response(status, json=body)

    return flavour.Client(transport=flavour.MockTransport(handler))


def _claude(requests: list[Any], status: int, body: Any) -> ClaudeProvider:
    provider = ClaudeProvider(KEY, MODEL)
    provider._client = anthropic.Anthropic(
        api_key=KEY,
        http_client=_transport(anthropic, requests, status, body),
    )
    return provider


def _openai_like(
    cls: type[AIProvider],
    requests: list[Any],
    status: int,
    body: Any,
) -> AIProvider:
    provider = cls(KEY, "model-under-test")  # type: ignore[call-arg]
    provider._client = openai.OpenAI(  # type: ignore[attr-defined]
        api_key=KEY,
        base_url="https://provider.invalid/v1",
        http_client=_transport(openai, requests, status, body),
    )
    return provider


def test_claude_probe_sends_one_bounded_request() -> None:
    requests: list[Any] = []

    _claude(requests, 200, _CLAUDE_OK).probe()

    assert len(requests) == 1
    request = requests[0]
    sent = json.loads(request.content)
    assert request.url.path.endswith("/v1/messages")
    # Anthropic SDK 2.x moved the API key to an Authorization bearer token.
    api_key_header = request.headers.get("x-api-key")
    bearer_header = request.headers.get("authorization")
    assert api_key_header == KEY or bearer_header == f"Bearer {KEY}"
    assert sent["model"] == MODEL
    assert sent["max_tokens"] == PROBE_MAX_TOKENS
    assert sent["messages"] == [{"role": "user", "content": PROBE_PROMPT}]
    assert request.extensions["timeout"]["read"] == PROBE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("cls", "limit_field", "absent_field"),
    [
        (OpenAIProvider, "max_completion_tokens", "max_tokens"),
        (DeepSeekProvider, "max_tokens", "max_completion_tokens"),
    ],
)
def test_openai_compatible_probes_use_each_apis_token_limit(
    cls: type[AIProvider],
    limit_field: str,
    absent_field: str,
) -> None:
    requests: list[Any] = []

    _openai_like(cls, requests, 200, _OPENAI_OK).probe()

    assert len(requests) == 1
    sent = json.loads(requests[0].content)
    assert sent[limit_field] == PROBE_MAX_TOKENS
    assert absent_field not in sent
    assert sent["model"] == "model-under-test"
    assert sent["messages"] == [{"role": "user", "content": PROBE_PROMPT}]
    assert requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert requests[0].extensions["timeout"]["read"] == PROBE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"error": {"message": f"Incorrect API key provided: {KEY}"}}, Code.INVALID_CREDENTIALS),
        (404, {"error": {"message": "The model `x` does not exist"}}, Code.MODEL_NOT_FOUND),
        (429, {"error": {"code": "insufficient_quota", "message": "quota"}}, Code.QUOTA_EXCEEDED),
        (500, {"error": {"message": "boom"}}, Code.PROVIDER_UNAVAILABLE),
    ],
)
def test_probe_failures_are_classified_and_never_retried(
    status: int,
    body: Any,
    expected: Code,
) -> None:
    for build in (
        lambda reqs: _openai_like(OpenAIProvider, reqs, status, body),
        lambda reqs: _claude(reqs, status, body),
    ):
        requests: list[Any] = []

        with pytest.raises(AIProviderError) as info:
            build(requests).probe()

        assert info.value.code == expected
        # max_retries=0: a failing probe is exactly one request, not three.
        assert len(requests) == 1
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__
        assert KEY not in repr(info.value)


def test_gemini_probe_is_bounded_and_does_not_retry() -> None:
    import google.generativeai as genai

    calls: list[tuple[str, dict[str, Any]]] = []
    configured: list[str] = []

    class _Model:
        def __init__(self, name: str) -> None:
            self.name = name

        def generate_content(self, prompt: str, **kwargs: Any) -> Any:
            calls.append((prompt, kwargs))
            return SimpleNamespace(text="")

    with (
        patch.object(genai, "configure", lambda *, api_key: configured.append(api_key)),
        patch.object(genai, "GenerativeModel", _Model),
    ):
        GeminiProvider(KEY, "gemini-3.8-flash").probe()

    prompt, kwargs = calls[0]
    assert prompt == PROBE_PROMPT
    assert kwargs["generation_config"] == {"max_output_tokens": PROBE_MAX_TOKENS}
    assert kwargs["request_options"] == {"timeout": PROBE_TIMEOUT_SECONDS, "retry": None}
    # configure() ran again immediately before the call (tenant isolation).
    assert configured == [KEY, KEY]


def test_gemini_probe_failure_is_classified_without_provider_text() -> None:
    import google.generativeai as genai

    class _Model:
        def __init__(self, name: str) -> None:
            self.name = name

        def generate_content(self, prompt: str, **kwargs: Any) -> Any:
            raise google_exceptions.PermissionDenied(f"key {KEY} was reported as leaked")

    with (
        patch.object(genai, "configure", lambda *, api_key: None),
        patch.object(genai, "GenerativeModel", _Model),
        pytest.raises(AIProviderError) as info,
    ):
        GeminiProvider(KEY, "gemini-3.8-flash").probe()

    assert info.value.code == Code.PERMISSION_DENIED
    assert KEY not in repr(info.value)


def test_providers_without_a_bounded_probe_fall_back_to_one_generation() -> None:
    class _Plain(AIProvider):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def generate(self, prompt: str, *, stream: bool = False) -> str:
            self.prompts.append(prompt)
            raise RuntimeError(f"vendor exploded with {KEY}")

        def name(self) -> str:
            return "plain"

    provider = _Plain()
    with pytest.raises(AIProviderError) as info:
        provider.probe()

    assert provider.prompts == [PROBE_PROMPT]
    assert info.value.code == Code.UNKNOWN
    assert KEY not in repr(info.value)


def test_probe_logs_the_code_but_never_the_key(caplog: pytest.LogCaptureFixture) -> None:
    requests: list[Any] = []
    body = {"error": {"message": f"Incorrect API key provided: {KEY}"}}

    with caplog.at_level(logging.DEBUG), pytest.raises(AIProviderError):
        _openai_like(OpenAIProvider, requests, 401, body).probe()

    assert "invalid_credentials" in caplog.text
    assert KEY not in caplog.text


@pytest.mark.parametrize("provider_id", sorted(PROVIDERS))
def test_every_registered_adapter_declares_its_provider_id(provider_id: str) -> None:
    assert PROVIDERS[provider_id]["class"].provider_id == provider_id


# ---------------------------------------------------------------------------
# Checker: cache, revalidation, rate limit, tenant isolation
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Script:
    """Registers scripted fake adapters for every provider id."""

    def __init__(self) -> None:
        self.outcomes: list[BaseException | None] = []
        self.built: list[tuple[str, str]] = []
        self.probes = 0

    def factory(self, *, api_key: str, model: str) -> AIProvider:
        self.built.append((api_key, model))
        script = self

        class _Scripted(AIProvider):
            def generate(self, prompt: str, *, stream: bool = False) -> str:
                return "ok"

            def name(self) -> str:
                return "scripted"

            def _probe(self, timeout: float) -> None:
                script.probes += 1
                outcome = script.outcomes.pop(0) if script.outcomes else None
                if outcome is not None:
                    raise outcome

        return _Scripted()


@pytest.fixture()
def script(monkeypatch: pytest.MonkeyPatch) -> _Script:
    scripted = _Script()
    for provider_id, spec in PROVIDERS.items():
        monkeypatch.setitem(
            ai_provider.PROVIDERS,
            provider_id,
            {"class": scripted.factory, "default_model": spec["default_model"]},
        )
    return scripted


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


def _checker(clock: _Clock, **kwargs: Any) -> AIConnectionChecker:
    return AIConnectionChecker(clock=clock, now=lambda: NOW, **kwargs)


def test_successful_check_reports_ok_without_persisting_anything(
    script: _Script,
    clock: _Clock,
) -> None:
    result = _checker(clock).check("user-a", "claude", MODEL, KEY)

    assert result.ok and result.code == "ok" and not result.cached
    assert result.provider == "claude" and result.model == MODEL
    assert result.checked_at == "2026-11-11T12:00:00+00:00"
    assert "Anthropic Claude" in result.message and MODEL in result.message
    assert script.built == [(KEY, MODEL)] and script.probes == 1
    payload = result.to_payload()
    assert payload["status"] == "ok" and payload["retryable"] is False
    assert KEY not in json.dumps(payload)


def test_identical_checks_are_cached_briefly_then_revalidated(
    script: _Script,
    clock: _Clock,
) -> None:
    checker = _checker(clock)

    first = checker.check("user-a", "claude", MODEL, KEY)
    clock.advance(299)
    second = checker.check("user-a", "claude", MODEL, KEY)
    clock.advance(2)
    third = checker.check("user-a", "claude", MODEL, KEY)

    assert (first.cached, second.cached, third.cached) == (False, True, False)
    assert script.probes == 2
    assert second.checked_at == first.checked_at


def test_changing_the_model_or_the_key_forces_a_fresh_check(
    script: _Script,
    clock: _Clock,
) -> None:
    checker = _checker(clock)

    checker.check("user-a", "claude", MODEL, KEY)
    changed_model = checker.check("user-a", "claude", "claude-opus-4-8", KEY)
    changed_key = checker.check("user-a", "claude", MODEL, KEY + "-rotated")
    unchanged = checker.check("user-a", "claude", MODEL, KEY)

    assert not changed_model.cached and not changed_key.cached
    assert unchanged.cached
    assert script.built == [
        (KEY, MODEL),
        (KEY, "claude-opus-4-8"),
        (KEY + "-rotated", MODEL),
    ]


def test_a_provider_change_is_a_separate_check(script: _Script, clock: _Clock) -> None:
    checker = _checker(clock)

    checker.check("user-a", "claude", MODEL, KEY)
    other = checker.check("user-a", "openai", "gpt-4o", KEY)

    assert not other.cached and script.probes == 2


def test_cache_is_tenant_scoped_even_for_identical_keys(
    script: _Script,
    clock: _Clock,
) -> None:
    checker = _checker(clock)

    checker.check("user-a", "claude", MODEL, KEY)
    other_tenant = checker.check("user-b", "claude", MODEL, KEY)

    assert not other_tenant.cached
    assert script.probes == 2


def test_invalidate_forgets_only_the_requested_tenant_and_provider(
    script: _Script,
    clock: _Clock,
) -> None:
    checker = _checker(clock)
    checker.check("user-a", "claude", MODEL, KEY)
    checker.check("user-a", "openai", "gpt-4o", KEY)
    checker.check("user-b", "claude", MODEL, KEY)

    assert checker.invalidate("user-a", "claude") == 1

    assert not checker.check("user-a", "claude", MODEL, KEY).cached
    assert checker.check("user-a", "openai", "gpt-4o", KEY).cached
    assert checker.check("user-b", "claude", MODEL, KEY).cached
    assert checker.invalidate("user-a") == 2


def test_definitive_failures_are_cached_only_for_a_short_time(
    script: _Script,
    clock: _Clock,
) -> None:
    checker = _checker(clock)
    script.outcomes = [
        _status_error(anthropic.AuthenticationError, 401),
        None,
    ]

    first = checker.check("user-a", "claude", MODEL, KEY)
    clock.advance(10)
    repeat = checker.check("user-a", "claude", MODEL, KEY)
    clock.advance(25)
    after = checker.check("user-a", "claude", MODEL, KEY)

    assert not first.ok and first.code == "invalid_credentials"
    assert not first.retryable
    assert repeat.cached and repeat.code == "invalid_credentials"
    assert after.ok and not after.cached
    assert script.probes == 2


def test_transient_failures_are_never_remembered(script: _Script, clock: _Clock) -> None:
    checker = _checker(clock)
    script.outcomes = [
        openai.APITimeoutError(request=_request(openai)),
        _status_error(openai.InternalServerError, 500),
        None,
    ]

    results = [checker.check("user-a", "claude", MODEL, KEY) for _ in range(3)]

    assert [r.code for r in results] == ["timeout", "provider_unavailable", "ok"]
    assert [r.retryable for r in results] == [True, True, False]
    assert not any(r.cached for r in results)
    assert script.probes == 3


def test_provider_throttling_reports_retry_after(script: _Script, clock: _Clock) -> None:
    script.outcomes = [
        _status_error(openai.RateLimitError, 429, headers={"retry-after": "12"}),
    ]

    result = _checker(clock).check("user-a", "openai", "gpt-4o", KEY)

    assert result.code == "rate_limited" and result.retryable
    assert result.retry_after == 12
    assert http_status_for(result) == 429


def test_live_checks_are_rate_limited_per_user(script: _Script, clock: _Clock) -> None:
    checker = _checker(clock, max_live_checks=2, window=60)

    checker.check("user-a", "claude", MODEL, KEY + "-1")
    clock.advance(10)
    checker.check("user-a", "claude", MODEL, KEY + "-2")
    with pytest.raises(ConnectionCheckRateLimited) as info:
        checker.check("user-a", "claude", MODEL, KEY + "-3")

    assert info.value.retry_after == 50
    assert script.probes == 2
    # Already-verified results are free, other tenants have their own budget,
    # and the window slides.
    assert checker.check("user-a", "claude", MODEL, KEY + "-1").cached
    assert not checker.check("user-b", "claude", MODEL, KEY + "-3").cached
    clock.advance(51)
    assert checker.check("user-a", "claude", MODEL, KEY + "-3").ok


def test_static_rejections_cost_nothing_and_never_reach_the_provider(
    script: _Script,
    clock: _Clock,
) -> None:
    checker = _checker(clock, max_live_checks=1)

    results = [
        checker.check("user-a", "claude", "claude-sonnet-4-20250514", KEY)
        for _ in range(5)
    ]

    assert {r.code for r in results} == {"model_retired"}
    assert script.built == [] and script.probes == 0
    assert results[0].replacement_model == PROVIDERS["claude"]["default_model"]
    assert PROVIDERS["claude"]["default_model"] in results[0].message
    assert http_status_for(results[0]) == 422
    # The single live check is still available afterwards.
    assert checker.check("user-a", "claude", MODEL, KEY).ok


def test_deprecated_models_name_their_shutdown_date(
    script: _Script,
    clock: _Clock,
) -> None:
    result = _checker(clock).validate_model("gemini", "gemini-2.5-pro")

    assert result is not None
    assert result.code == "model_deprecated"
    assert "1 December 2026" in result.message
    assert script.probes == 0


@pytest.mark.parametrize(
    ("api_key", "code"),
    [
        ("", "missing_credentials"),
        ("   ", "missing_credentials"),
        ("two words " + KEY, "credentials_malformed"),
        ("short", "credentials_malformed"),
        (KEY + "\nX-Injected: 1", "credentials_malformed"),
        ("k" * 513, "credentials_malformed"),
    ],
)
def test_unusable_credentials_are_rejected_before_any_provider_call(
    script: _Script,
    clock: _Clock,
    api_key: str,
    code: str,
) -> None:
    result = _checker(clock).check("user-a", "claude", MODEL, api_key)

    assert not result.ok and result.code == code
    assert api_key.strip() == "" or api_key.strip() not in result.message
    assert script.built == []


def test_secrets_never_reach_results_cache_or_logs(
    script: _Script,
    clock: _Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checker = _checker(clock)
    script.outcomes = [RuntimeError(f"vendor echoed {KEY}"), None]

    with caplog.at_level(logging.DEBUG):
        failed = checker.check("user-a", "claude", MODEL, KEY)
        ok = checker.check("user-a", "claude", MODEL, KEY + "-2")

    for result in (failed, ok):
        assert KEY not in json.dumps(result.to_payload())
    assert KEY not in caplog.text
    assert KEY not in repr(checker.__dict__)
    assert KEY not in repr(list(checker._cache))
    assert failed.code == "unknown"


def test_missing_sdk_is_reported_as_a_deployment_problem(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    def _no_sdk(*, api_key: str, model: str) -> AIProvider:
        raise ImportError("The `anthropic` package is required")

    monkeypatch.setitem(
        ai_provider.PROVIDERS,
        "claude",
        {"class": _no_sdk, "default_model": MODEL},
    )

    result = _checker(clock).check("user-a", "claude", MODEL, KEY)

    assert result.code == "sdk_unavailable"
    assert http_status_for(result) == 503


def test_cache_is_bounded(script: _Script, clock: _Clock) -> None:
    checker = _checker(clock, max_entries=2, max_live_checks=100)

    for index in range(5):
        checker.check("user-a", "claude", MODEL, f"{KEY}-{index}")

    assert len(checker._cache) == 2
    # The most recent entries survive; the oldest was evicted.
    assert checker.check("user-a", "claude", MODEL, f"{KEY}-4").cached
    assert not checker.check("user-a", "claude", MODEL, f"{KEY}-0").cached


def test_programmer_errors_fail_closed(script: _Script, clock: _Clock) -> None:
    checker = _checker(clock)

    with pytest.raises(ValueError, match="user id"):
        checker.check("", "claude", MODEL, KEY)
    with pytest.raises(ValueError, match="Unknown AI provider"):
        checker.check("user-a", "not-a-provider", MODEL, KEY)
    with pytest.raises(ValueError):
        AIConnectionChecker(max_live_checks=0)
    assert script.probes == 0


def test_http_status_mapping_distinguishes_user_fixable_from_retryable(
    script: _Script,
    clock: _Clock,
) -> None:
    checker = _checker(clock)
    ok = checker.check("user-a", "claude", MODEL, KEY)
    script.outcomes = [
        _status_error(anthropic.AuthenticationError, 401),
        openai.APITimeoutError(request=_request(openai)),
    ]
    bad_key = checker.check("user-a", "claude", MODEL, KEY + "-b")
    timeout = checker.check("user-a", "claude", MODEL, KEY + "-c")

    assert [http_status_for(r) for r in (ok, bad_key, timeout)] == [200, 422, 503]


@pytest.mark.parametrize("code", list(AIProviderErrorCode))
def test_every_code_has_an_actionable_message_without_template_leftovers(
    code: AIProviderErrorCode,
) -> None:
    text = describe(
        code,
        provider="openai",
        model="gpt-4o",
        replacement="gpt-4o",
        retires_on=date(2027, 1, 14),
    )

    assert text and "{" not in text and "}" not in text
    assert len(text) < 300
