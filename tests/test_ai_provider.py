"""Tests for ai_provider.py: provider factory and user-key resolution."""

from __future__ import annotations

import pytest

from ai_provider import (
    ClaudeProvider,
    DeepSeekProvider,
    GeminiProvider,
    OpenAIProvider,
    available_providers,
    get_provider,
    resolve_provider,
)


def test_get_provider_gemini() -> None:
    provider = get_provider("gemini", "test-key")
    assert isinstance(provider, GeminiProvider)
    assert provider.name() == "Gemini (gemini-2.5-flash)"


def test_get_provider_claude() -> None:
    provider = get_provider("claude", "sk-ant-test")
    assert isinstance(provider, ClaudeProvider)
    assert provider.name() == "Claude (claude-sonnet-4-20250514)"


def test_get_provider_openai() -> None:
    provider = get_provider("openai", "sk-test")
    assert isinstance(provider, OpenAIProvider)
    assert provider.name() == "OpenAI (gpt-4o)"


def test_get_provider_deepseek() -> None:
    provider = get_provider("deepseek", "sk-test")
    assert isinstance(provider, DeepSeekProvider)


def test_get_provider_with_custom_model() -> None:
    provider = get_provider("gemini", "test-key", model="gemini-2.0-flash")
    assert "gemini-2.0-flash" in provider.name()


def test_get_provider_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown AI provider"):
        get_provider("nonexistent", "key")


def test_get_provider_case_insensitive() -> None:
    provider = get_provider("GEMINI", "test-key")
    assert isinstance(provider, GeminiProvider)


def test_available_providers_returns_all() -> None:
    providers = available_providers()
    assert len(providers) == 4
    ids = {p["id"] for p in providers}
    assert ids == {"gemini", "claude", "openai", "deepseek"}
    for p in providers:
        assert "default_model" in p


# ---------------------------------------------------------------------------
# resolve_provider tests (with mocked database lookups)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_calls(monkeypatch) -> list:
    """Patch ai_provider.get_provider to record calls and return a GeminiProvider."""
    calls: list = []

    def _fake(provider_name: str, api_key: str, model: str | None = None):
        calls.append((provider_name, api_key, model))
        return GeminiProvider(api_key, model=model or "gemini-2.5-flash")

    monkeypatch.setattr("ai_provider.get_provider", _fake)
    return calls


def test_resolve_uses_preferences(monkeypatch, fake_calls: list) -> None:
    """When user prefs specify openai, resolve_provider uses it with user's key."""
    monkeypatch.setattr(
        "database.get_user_preferences",
        lambda uid, **kw: {"preferred_ai": "openai", "ai_model": "gpt-4o-mini"},
    )
    monkeypatch.setattr(
        "database.get_user_api_key",
        lambda uid, provider, **kw: {"api_key": "sk-user-key"},
    )

    resolve_provider("user-1")
    assert fake_calls == [("openai", "sk-user-key", "gpt-4o-mini")]


def test_resolve_falls_back_to_gemini_server_key(monkeypatch, fake_calls: list) -> None:
    """Without user prefs, Gemini is default and server key is used."""
    monkeypatch.setattr(
        "database.get_user_preferences",
        lambda uid, **kw: {},
    )
    monkeypatch.setattr(
        "database.get_user_api_key",
        lambda uid, provider, **kw: None,
    )

    resolve_provider("user-1", server_gemini_key="server-gemini-key")
    assert fake_calls == [("gemini", "server-gemini-key", None)]


def test_resolve_no_user_id_uses_gemini_server_key(fake_calls: list) -> None:
    """When user_id is None, Gemini is assumed with server_gemini_key."""
    resolve_provider(server_gemini_key="server-key")
    assert fake_calls == [("gemini", "server-key", None)]


def test_resolve_raises_when_no_key_for_non_default(monkeypatch) -> None:
    """Non-default providers must have a user key; server fallback only for Gemini."""
    monkeypatch.setattr(
        "database.get_user_preferences",
        lambda uid, **kw: {"preferred_ai": "claude"},
    )
    monkeypatch.setattr(
        "database.get_user_api_key",
        lambda uid, provider, **kw: None,
    )

    with pytest.raises(ValueError, match="No claude API key configured"):
        resolve_provider("user-1", server_gemini_key="server-key")


def test_resolve_preserves_provider_case(monkeypatch, fake_calls: list) -> None:
    """Provider name normalisation should handle mixed case preferences."""
    monkeypatch.setattr(
        "database.get_user_preferences",
        lambda uid, **kw: {"preferred_ai": "DEEPSEEK"},
    )
    monkeypatch.setattr(
        "database.get_user_api_key",
        lambda uid, provider, **kw: {"api_key": "ds-key"},
    )

    resolve_provider("user-1")
    assert fake_calls[0][0] == "deepseek"


def test_resolve_no_user_record_defaults_to_gemini(
    monkeypatch,
    fake_calls: list,
) -> None:
    """If user has no preferences row, defaults should apply."""
    monkeypatch.setattr(
        "database.get_user_preferences",
        lambda uid, **kw: {},
    )
    monkeypatch.setattr(
        "database.get_user_api_key",
        lambda uid, provider, **kw: None,
    )

    resolve_provider("user-1", server_gemini_key="fallback-key")
    assert fake_calls[0][0] == "gemini"


def test_resolve_none_api_key_treated_as_missing(monkeypatch) -> None:
    """An empty string api_key in the record should trigger fallback."""
    monkeypatch.setattr(
        "database.get_user_preferences",
        lambda uid, **kw: {"preferred_ai": "gemini"},
    )
    monkeypatch.setattr(
        "database.get_user_api_key",
        lambda uid, provider, **kw: {"api_key": ""},
    )

    resolve_provider("user-1", server_gemini_key="server-key")
