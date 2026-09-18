"""Tests for ai_provider.py and the resolve_provider wiring."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from ai_provider import (
    AIProvider,
    ClaudeProvider,
    DeepSeekProvider,
    GeminiProvider,
    OpenAIProvider,
    available_providers,
    get_provider,
    resolve_provider,
)


def test_get_provider_gemini_default_model():
    provider = get_provider("gemini", "fake-key")
    assert isinstance(provider, GeminiProvider)
    assert "gemini" in provider.name().lower()


def test_get_provider_openai_custom_model():
    provider = get_provider("openai", "fake-key", model="gpt-4o-mini")
    assert isinstance(provider, OpenAIProvider)
    assert "gpt-4o-mini" in provider.name()


def test_get_provider_claude():
    with patch("anthropic.Anthropic", return_value=None):
        provider = get_provider("claude", "fake-key")
        assert isinstance(provider, ClaudeProvider)


def test_get_provider_deepseek():
    with patch("openai.OpenAI", return_value=None):
        provider = get_provider("deepseek", "fake-key")
        assert isinstance(provider, DeepSeekProvider)


def test_get_provider_unknown_raises():
    with pytest.raises(ValueError, match="Unknown AI provider"):
        get_provider("nonsense", "fake-key")


def test_gemini_provider_generates_and_streams() -> None:
    model = MagicMock()
    model.generate_content.side_effect = [
        SimpleNamespace(text="  complete response  "),
        [
            SimpleNamespace(text="first "),
            SimpleNamespace(text=""),
            SimpleNamespace(text="second"),
        ],
    ]

    with (
        patch("google.generativeai.configure") as configure,
        patch(
            "google.generativeai.GenerativeModel",
            return_value=model,
        ) as model_factory,
    ):
        provider = GeminiProvider("synthetic-key", model="gemini-test")

    assert provider.generate("prompt") == "complete response"
    stream = provider.generate("prompt", stream=True)
    assert not isinstance(stream, str)
    assert list(stream) == ["first ", "second"]
    configure.assert_called_once_with(api_key="synthetic-key")
    model_factory.assert_called_once_with("gemini-test")
    assert model.generate_content.call_args_list == [
        call("prompt"),
        call("prompt", stream=True),
    ]


def test_claude_provider_generates_and_streams() -> None:
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(kind="metadata"), SimpleNamespace(text="  answer  ")],
    )
    stream_context = MagicMock()
    stream_context.__enter__.return_value = SimpleNamespace(
        text_stream=iter(["first ", "second"]),
    )
    client.messages.stream.return_value = stream_context

    with patch("anthropic.Anthropic", return_value=client) as client_factory:
        provider = ClaudeProvider("synthetic-key", model="claude-test")

    assert provider.generate("prompt") == "answer"
    stream = provider.generate("prompt", stream=True)
    assert not isinstance(stream, str)
    assert list(stream) == ["first ", "second"]
    assert provider.name() == "Claude (claude-test)"
    client_factory.assert_called_once_with(api_key="synthetic-key")
    client.messages.create.assert_called_once_with(
        model="claude-test",
        max_tokens=4096,
        messages=[{"role": "user", "content": "prompt"}],
    )
    client.messages.stream.assert_called_once_with(
        model="claude-test",
        max_tokens=4096,
        messages=[{"role": "user", "content": "prompt"}],
    )


@pytest.mark.parametrize(
    ("provider_type", "model", "expected_name", "client_kwargs"),
    [
        (
            OpenAIProvider,
            "gpt-test",
            "OpenAI (gpt-test)",
            {"api_key": "synthetic-key"},
        ),
        (
            DeepSeekProvider,
            "deepseek-test",
            "DeepSeek (deepseek-test)",
            {
                "api_key": "synthetic-key",
                "base_url": DeepSeekProvider.BASE_URL,
            },
        ),
    ],
)
def test_openai_compatible_providers_generate_and_stream(
    provider_type: type[OpenAIProvider | DeepSeekProvider],
    model: str,
    expected_name: str,
    client_kwargs: dict[str, str],
) -> None:
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="  answer  "))],
        ),
        [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="first "))],
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=None))],
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="second"))],
            ),
        ],
    ]

    with patch("openai.OpenAI", return_value=client) as client_factory:
        provider = provider_type("synthetic-key", model=model)

    assert provider.generate("prompt") == "answer"
    stream = provider.generate("prompt", stream=True)
    assert not isinstance(stream, str)
    assert list(stream) == ["first ", "second"]
    assert provider.name() == expected_name
    client_factory.assert_called_once_with(**client_kwargs)
    assert client.chat.completions.create.call_args_list == [
        call(model=model, messages=[{"role": "user", "content": "prompt"}]),
        call(
            model=model,
            messages=[{"role": "user", "content": "prompt"}],
            stream=True,
        ),
    ]


def test_available_providers_exposes_registered_defaults() -> None:
    providers = {provider["id"]: provider for provider in available_providers()}

    assert providers["gemini"]["name"] == "Gemini"
    assert providers["claude"]["default_model"] == "claude-sonnet-4-20250514"
    assert providers["openai"]["default_model"] == "gpt-4o"
    assert providers["deepseek"]["default_model"] == "deepseek-chat"


# ---------------------------------------------------------------------------
# resolve_provider tests
# ---------------------------------------------------------------------------


class _FakeProvider(AIProvider):
    """Simple provider that echoes its config so tests can inspect it."""

    def __init__(self, name: str, api_key: str, model: str | None = None):
        self._name = name
        self._api_key = api_key
        self._model = model

    def generate(self, prompt, *, stream=False):
        return f"{self._name}:{prompt[:20]}"

    def name(self):
        display = f"{self._name} ({self._model})" if self._model else self._name
        return display


def _register_fake_providers():
    """Replace PROVIDERS with fake providers for deterministic testing."""

    return {
        "gemini": {
            "class": lambda api_key, model: _FakeProvider("gemini", api_key, model),
            "default_model": "gemini-2.5-flash",
        },
        "claude": {
            "class": lambda api_key, model: _FakeProvider("claude", api_key, model),
            "default_model": "claude-sonnet-4-20250514",
        },
        "openai": {
            "class": lambda api_key, model: _FakeProvider("openai", api_key, model),
            "default_model": "gpt-4o",
        },
        "deepseek": {
            "class": lambda api_key, model: _FakeProvider("deepseek", api_key, model),
            "default_model": "deepseek-chat",
        },
    }


def test_resolve_provider_no_user_id_falls_back_to_gemini():
    """Without a user_id, uses the fallback key and model with Gemini."""
    with patch("ai_provider.PROVIDERS", _register_fake_providers()):
        provider = resolve_provider(
            user_id=None,
            fallback_api_key="server-key-123",
            fallback_model="gemini-2.5-pro",
        )
        assert "gemini" in provider.name().lower()
        assert "gemini-2.5-pro" in provider.name()


def test_resolve_provider_no_user_id_missing_key_raises():
    with pytest.raises(ValueError, match="No AI provider key"):
        resolve_provider(user_id=None, fallback_api_key=None, fallback_model=None)


def test_resolve_provider_user_prefers_gemini_no_user_key_falls_back(
    monkeypatch, tmp_path
):
    """User picks Gemini but hasn't stored a key → server fallback."""
    db = str(tmp_path / "test.db")
    import database

    database.init_db(db)

    # Create a user with gemini preference and no stored key
    database.get_or_create_user("test@example.com", db_path=db)
    user = database.get_or_create_user("test@example.com", db_path=db)
    database.save_user_preferences(
        user["id"], preferred_ai="gemini", ai_model=None, db_path=db
    )

    with patch("ai_provider.PROVIDERS", _register_fake_providers()):
        provider = resolve_provider(
            user_id=user["id"],
            fallback_api_key="server-key-abc",
            fallback_model="gemini-2.5-flash",
            db_path=db,
        )
    assert "gemini" in provider.name().lower()


def test_resolve_provider_user_prefers_claude_with_key():
    """User has a stored Claude key → returns a Claude provider."""
    with patch("ai_provider.PROVIDERS", _register_fake_providers()), patch(
        "database.get_user_preferences",
        return_value={"preferred_ai": "claude", "ai_model": None},
    ), patch(
        "database.get_user_api_key",
        return_value={"api_key": "user-claude-key"},
    ):
        provider = resolve_provider(
            user_id="user-1",
            fallback_api_key="server-key",
        )
    assert "claude" in provider.name().lower()


def test_resolve_provider_user_prefers_claude_no_key_raises():
    """User picks Claude but has no key → error (never bill server key)."""
    with patch("ai_provider.PROVIDERS", _register_fake_providers()), patch(
        "database.get_user_preferences",
        return_value={"preferred_ai": "claude", "ai_model": None},
    ), patch(
        "database.get_user_api_key",
        return_value=None,
    ), pytest.raises(ValueError, match="No claude key"):
        resolve_provider(
            user_id="user-1",
            fallback_api_key="server-key",
        )


def test_resolve_provider_user_prefers_deepseek_with_model():
    """User picks DeepSeek with a custom model."""
    with patch("ai_provider.PROVIDERS", _register_fake_providers()), patch(
        "database.get_user_preferences",
        return_value={"preferred_ai": "deepseek", "ai_model": "deepseek-coder"},
    ), patch(
        "database.get_user_api_key",
        return_value={"api_key": "user-ds-key"},
    ):
        provider = resolve_provider(
            user_id="user-1",
            fallback_api_key="server-key",
        )
    assert "deepseek" in provider.name().lower()
    assert "deepseek-coder" in provider.name()


def test_resolve_provider_user_no_prefs_defaults_to_gemini():
    """No prefs row at all → defaults to gemini, falls back to server key."""
    with patch("ai_provider.PROVIDERS", _register_fake_providers()), patch(
        "database.get_user_preferences",
        return_value={},
    ), patch(
        "database.get_user_api_key",
        return_value=None,
    ):
        provider = resolve_provider(
            user_id="user-1",
            fallback_api_key="server-key",
            fallback_model="gemini-2.5-pro",
        )
    assert "gemini" in provider.name().lower()
    assert "gemini-2.5-pro" in provider.name()
