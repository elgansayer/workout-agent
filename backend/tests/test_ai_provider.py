"""Tests for ai_provider.py and the resolve_provider wiring."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ai_provider import (
    PROVIDERS,
    AIProvider,
    ClaudeProvider,
    DeepSeekProvider,
    GeminiProvider,
    OpenAIProvider,
    available_providers,
    get_provider,
)
from ai_resolver import resolve_provider


def test_get_provider_gemini_default_model():
    provider = get_provider("gemini", "fake-key")
    assert isinstance(provider, GeminiProvider)
    assert "gemini" in provider.name().lower()


def test_gemini_provider_keeps_api_keys_isolated_between_users():
    """Lazy SDK clients must bind to the provider owner's key, not the last key."""
    import google.generativeai as genai

    active_configuration: dict[str, str] = {}

    class _LazyModel:
        def __init__(self, model: str) -> None:
            self.model = model

        def generate_content(self, prompt: str, *, stream: bool = False):
            assert stream is False
            return SimpleNamespace(text=active_configuration["api_key"])

    def _configure(*, api_key: str) -> None:
        active_configuration["api_key"] = api_key

    with (
        patch.object(genai, "configure", _configure),
        patch.object(genai, "GenerativeModel", _LazyModel),
    ):
        first_user = GeminiProvider("tenant-a-key")
        GeminiProvider("tenant-b-key")

        assert first_user.generate("prompt") == "tenant-a-key"


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
    with (
        patch("ai_provider.PROVIDERS", _register_fake_providers()),
        patch(
            "database.get_user_preferences",
            return_value={"preferred_ai": "claude", "ai_model": None},
        ),
        patch(
            "database.get_user_api_key",
            return_value={"api_key": "user-claude-key"},
        ),
    ):
        provider = resolve_provider(
            user_id="user-1",
            fallback_api_key="server-key",
        )
    assert "claude" in provider.name().lower()


def test_resolve_provider_user_prefers_claude_no_key_raises():
    """User picks Claude but has no key → error (never bill server key)."""
    with (
        patch("ai_provider.PROVIDERS", _register_fake_providers()),
        patch(
            "database.get_user_preferences",
            return_value={"preferred_ai": "claude", "ai_model": None},
        ),
        patch(
            "database.get_user_api_key",
            return_value=None,
        ),
        pytest.raises(ValueError, match="No claude key"),
    ):
        resolve_provider(
            user_id="user-1",
            fallback_api_key="server-key",
        )


def test_resolve_provider_user_prefers_deepseek_with_model():
    """User picks DeepSeek with a custom model."""
    with (
        patch("ai_provider.PROVIDERS", _register_fake_providers()),
        patch(
            "database.get_user_preferences",
            return_value={"preferred_ai": "deepseek", "ai_model": "deepseek-coder"},
        ),
        patch(
            "database.get_user_api_key",
            return_value={"api_key": "user-ds-key"},
        ),
    ):
        provider = resolve_provider(
            user_id="user-1",
            fallback_api_key="server-key",
        )
    assert "deepseek" in provider.name().lower()
    assert "deepseek-coder" in provider.name()


def test_resolve_provider_user_no_prefs_defaults_to_gemini():
    """No prefs row at all → defaults to gemini, falls back to server key."""
    with (
        patch("ai_provider.PROVIDERS", _register_fake_providers()),
        patch(
            "database.get_user_preferences",
            return_value={},
        ),
        patch(
            "database.get_user_api_key",
            return_value=None,
        ),
    ):
        provider = resolve_provider(
            user_id="user-1",
            fallback_api_key="server-key",
            fallback_model="gemini-2.5-pro",
        )
    assert "gemini" in provider.name().lower()
    assert "gemini-2.5-pro" in provider.name()


def test_resolve_provider_uses_model_stored_with_provider_key():
    """Per-provider model metadata survives the Settings key workflow."""
    with (
        patch("ai_provider.PROVIDERS", _register_fake_providers()),
        patch(
            "database.get_user_preferences",
            return_value={"preferred_ai": "claude", "ai_model": None},
        ),
        patch(
            "database.get_user_api_key",
            return_value={
                "api_key": "user-claude-key",
                "extra": {"model": "claude-custom"},
            },
        ),
    ):
        provider = resolve_provider(user_id="user-1")

    assert "claude-custom" in provider.name()


def test_resolve_provider_preference_model_wins_over_stale_key_metadata():
    with (
        patch("ai_provider.PROVIDERS", _register_fake_providers()),
        patch(
            "database.get_user_preferences",
            return_value={"preferred_ai": "openai", "ai_model": "gpt-current"},
        ),
        patch(
            "database.get_user_api_key",
            return_value={
                "api_key": "user-openai-key",
                "extra": {"model": "gpt-stale"},
            },
        ),
    ):
        provider = resolve_provider(user_id="user-1")

    assert "gpt-current" in provider.name()
    assert "gpt-stale" not in provider.name()


def test_available_providers_is_a_projection_of_registry():
    providers = available_providers()

    assert [provider["id"] for provider in providers] == list(PROVIDERS)
    assert {provider["id"]: provider["default_model"] for provider in providers} == {
        provider_id: spec["default_model"] for provider_id, spec in PROVIDERS.items()
    }


def test_generation_callsite_inventory_and_sdk_boundary():
    backend = Path(__file__).resolve().parents[1]
    expected_calls = {
        "gemini_engine.py": 3,
        "insight_cron.py": 2,
        "webapp/app.py": 3,
    }

    assert {
        relative: (backend / relative)
        .read_text(encoding="utf-8")
        .count("provider.generate(")
        for relative in expected_calls
    } == expected_calls

    banned_modules = {"anthropic", "google.generativeai", "openai"}
    violations: list[str] = []
    for module_path in backend.rglob("*.py"):
        if module_path == backend / "ai_provider.py" or "tests" in module_path.parts:
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported = {node.module or ""}
            else:
                continue
            if imported & banned_modules:
                violations.append(str(module_path.relative_to(backend)))
    assert violations == []

    for relative in ("main.py", "checkin.py", "insight_cron.py", "webapp/app.py"):
        source = (backend / relative).read_text(encoding="utf-8")
        assert "from ai_resolver import resolve_provider" in source


def test_registered_provider_sdks_are_required_and_importable():
    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(
        encoding="utf-8"
    )
    dependency_contract = {
        "gemini": ("google-generativeai", "google.generativeai"),
        "claude": ("anthropic", "anthropic"),
        "openai": ("openai", "openai"),
        # DeepSeek intentionally uses its OpenAI-compatible API.
        "deepseek": ("openai", "openai"),
    }

    assert set(dependency_contract) == set(PROVIDERS)
    for requirement, module in dependency_contract.values():
        assert requirement in requirements
        importlib.import_module(module)


def test_agent_and_web_images_install_the_shared_provider_requirements():
    root = Path(__file__).resolve().parents[2]
    agent_dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    web_dockerfile = (root / "Dockerfile.web").read_text(encoding="utf-8")

    assert "COPY backend/requirements.txt" in agent_dockerfile
    assert "pip install --no-cache-dir -r requirements.txt" in agent_dockerfile
    assert (
        "COPY backend/requirements.txt backend/requirements-web.txt" in web_dockerfile
    )
    assert (
        "pip install --no-cache-dir -r requirements.txt -r requirements-web.txt"
        in web_dockerfile
    )


def test_production_services_share_encryption_and_gemini_fallback_config():
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.portainer.yml").read_text(encoding="utf-8")

    assert (
        compose.count("ENCRYPTION_KEY: ${ENCRYPTION_KEY:?ENCRYPTION_KEY is required}")
        == 2
    )
    assert compose.count("GEMINI_API_KEY: ${GEMINI_API_KEY:-}") == 2
