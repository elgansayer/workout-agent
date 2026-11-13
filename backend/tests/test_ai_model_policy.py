"""Static model policy: unsupported, deprecated and retired model IDs."""

from __future__ import annotations

from datetime import date

import ai_model_policy
import pytest
from ai_model_policy import (
    MAX_MODEL_ID_LENGTH,
    evaluate_model,
    is_plausible_model_id,
)
from ai_provider import PROVIDERS, AIProviderErrorCode

TODAY = date(2026, 11, 11)

RETIRED = AIProviderErrorCode.MODEL_RETIRED
DEPRECATED = AIProviderErrorCode.MODEL_DEPRECATED
UNSUPPORTED = AIProviderErrorCode.MODEL_UNSUPPORTED


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_registry_defaults_are_never_rejected(provider: str) -> None:
    """A default the policy rejects would make a blank model field unusable."""
    default = PROVIDERS[provider]["default_model"]

    # Checked today and far in the future: defaults must match no rule at all,
    # so this cannot become a date-triggered failure.
    assert evaluate_model(provider, default, today=TODAY) is None
    assert evaluate_model(provider, default, today=date(2100, 1, 1)) is None


@pytest.mark.parametrize(
    ("provider", "model", "code"),
    [
        # Claude: the previous registry default was retired on 15 June 2026.
        ("claude", "claude-sonnet-4-20250514", RETIRED),
        ("claude", "claude-sonnet-4-0", RETIRED),
        ("claude", "claude-opus-4-1-20250805", RETIRED),
        ("claude", "claude-3-5-sonnet-20241022", RETIRED),
        ("claude", "claude-3-5-sonnet-latest", RETIRED),
        ("claude", "claude-3-7-sonnet-20250219", RETIRED),
        ("claude", "claude-3-opus-20240229", RETIRED),
        ("claude", "claude-3-haiku-20240307", RETIRED),
        ("claude", "claude-2.1", RETIRED),
        ("claude", "claude-instant-1.2", RETIRED),
        ("claude", "claude-sonnet-4-5-20250929", DEPRECATED),
        # OpenAI
        ("openai", "gpt-4", RETIRED),
        ("openai", "gpt-4-turbo", RETIRED),
        ("openai", "gpt-3.5-turbo", RETIRED),
        ("openai", "o1-preview", RETIRED),
        ("openai", "chatgpt-4o-latest", RETIRED),
        ("openai", "gpt-4o-2024-05-13", RETIRED),
        ("openai", "text-davinci-003", RETIRED),
        ("openai", "gpt-5", DEPRECATED),
        ("openai", "gpt-5-mini-2025-08-07", DEPRECATED),
        ("openai", "o3", DEPRECATED),
        ("openai", "gpt-4o-2024-08-06", DEPRECATED),
        ("openai", "text-embedding-3-small", UNSUPPORTED),
        ("openai", "whisper-1", UNSUPPORTED),
        ("openai", "gpt-4o-mini-tts", UNSUPPORTED),
        ("openai", "gpt-4o-realtime-preview", UNSUPPORTED),
        ("openai", "dall-e-3", UNSUPPORTED),
        # Gemini
        ("gemini", "gemini-2.0-flash", RETIRED),
        ("gemini", "gemini-2.0-flash-lite", RETIRED),
        ("gemini", "gemini-2.5-pro-preview-06-05", RETIRED),
        ("gemini", "gemini-3-pro-preview", RETIRED),
        ("gemini", "gemini-2.5-pro", DEPRECATED),
        ("gemini", "models/gemini-2.5-flash", DEPRECATED),
        ("gemini", "gemini-embedding-2", UNSUPPORTED),
        ("gemini", "imagen-4.0-generate", UNSUPPORTED),
        ("gemini", "gemini-2.5-flash-preview-tts", UNSUPPORTED),
        ("gemini", "gemini-2.5-flash-image", UNSUPPORTED),
        ("gemini", "gemini-live-2.5-flash-preview", UNSUPPORTED),
        # DeepSeek
        ("deepseek", "deepseek-chat", RETIRED),
        ("deepseek", "deepseek-reasoner", RETIRED),
        ("deepseek", "deepseek-v4-flash", RETIRED),
    ],
)
def test_known_unusable_models_are_rejected(
    provider: str,
    model: str,
    code: AIProviderErrorCode,
) -> None:
    decision = evaluate_model(provider, model, today=TODAY)

    assert decision is not None
    assert decision.code == code


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("claude", "claude-sonnet-5-5"),
        ("claude", "claude-opus-5-5"),
        ("claude", "claude-haiku-5-5"),
        ("claude", "claude-haiku-4-5-20251001"),
        ("claude", "claude-opus-4-8"),
        ("claude", "claude-fable-5-2"),
        ("openai", "gpt-4o"),
        ("openai", "gpt-4o-mini"),
        ("openai", "gpt-4.1"),
        ("openai", "gpt-4.1-mini"),
        ("openai", "gpt-5.1"),
        ("openai", "ft:gpt-4o-mini-2024-07-18:acme::abc123"),
        ("gemini", "gemini-3.8-flash"),
        ("gemini", "models/gemini-3.8-flash"),
        ("gemini", "gemini-2.5-flash-lite"),
        ("gemini", "tunedModels/my-live-coach-123"),
        ("deepseek", "deepseek-flash"),
        ("deepseek", "deepseek-v4-pro"),
        # Unknown names are the live check's call, not the policy's.
        ("claude", "claude-tomorrow-9"),
        ("openai", "gpt-9-future"),
    ],
)
def test_current_and_unknown_models_are_left_to_the_live_check(
    provider: str,
    model: str,
) -> None:
    assert evaluate_model(provider, model, today=TODAY) is None


def test_deprecated_model_becomes_retired_on_its_shutdown_date() -> None:
    model = "claude-sonnet-4-5-20250929"

    assert evaluate_model("claude", model, today=date(2026, 9, 29)) is None
    announced = evaluate_model("claude", model, today=date(2026, 9, 30))
    last_day = evaluate_model("claude", model, today=date(2026, 11, 29))
    retired = evaluate_model("claude", model, today=date(2026, 11, 30))

    assert announced is not None and announced.code == DEPRECATED
    assert last_day is not None and last_day.code == DEPRECATED
    assert last_day.retires_on == date(2026, 11, 30)
    assert retired is not None and retired.code == RETIRED
    assert retired.retires_on is None


def test_rejections_suggest_the_registry_default_as_replacement() -> None:
    for provider, model in (
        ("claude", "claude-sonnet-4-20250514"),
        ("openai", "gpt-4"),
        ("gemini", "gemini-2.0-flash"),
        ("deepseek", "deepseek-chat"),
    ):
        decision = evaluate_model(provider, model, today=TODAY)

        assert decision is not None
        assert decision.replacement == PROVIDERS[provider]["default_model"]
        assert evaluate_model(provider, decision.replacement, today=TODAY) is None


def test_matching_ignores_case_and_surrounding_space() -> None:
    decision = evaluate_model("claude", "  Claude-Sonnet-4-20250514 ", today=TODAY)

    assert decision is not None and decision.code == RETIRED


def test_opaque_user_named_assets_are_not_judged_by_their_names() -> None:
    # "-live-" / "-audio-" in a user's own name must not look like a family.
    assert evaluate_model("openai", "ft:gpt-4o:acme:audio-notes:abc", today=TODAY) is None
    assert evaluate_model("gemini", "tunedModels/embedding-fan-1", today=TODAY) is None


@pytest.mark.parametrize(
    "model",
    [
        "",
        "   ",
        "has space",
        "line\nbreak",
        "-leading-dash",
        "../etc/passwd",
        "models/../other",
        "models//x",
        "trailing/",
        "unicode-modèl",
        "semi;colon",
        "x" * (MAX_MODEL_ID_LENGTH + 1),
    ],
)
def test_implausible_model_ids_are_invalid_for_every_provider(model: str) -> None:
    for provider in PROVIDERS:
        decision = evaluate_model(provider, model, today=TODAY)

        assert decision is not None
        assert decision.code == AIProviderErrorCode.MODEL_INVALID


def test_is_plausible_model_id_accepts_real_identifier_shapes() -> None:
    for model in (
        "gpt-4o",
        "claude-sonnet-5-5",
        "models/gemini-3.8-flash",
        "ft:gpt-4o-mini:org::id",
        "x" * MAX_MODEL_ID_LENGTH,
    ):
        assert is_plausible_model_id(model)


def test_rule_tables_only_reference_registered_providers() -> None:
    assert set(ai_model_policy._LIFECYCLE) <= set(PROVIDERS)
    assert set(ai_model_policy._UNSUPPORTED) <= set(PROVIDERS)
    for rules in ai_model_policy._LIFECYCLE.values():
        for rule in rules:
            assert rule.deprecated_on <= rule.retires_on
            assert rule.retires_on <= date(2030, 1, 1)


def test_policy_review_date_is_recorded() -> None:
    # A reviewer-visible marker: bump it when the tables are re-checked against
    # the providers' deprecation pages (see docs/AI_PROVIDER_CONNECTION_TESTS.md).
    assert date(2026, 11, 11) <= ai_model_policy.POLICY_REVIEWED_ON
