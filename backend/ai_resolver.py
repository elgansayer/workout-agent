"""Resolve a user's preferred AI provider, encrypted key, and model.

This is the single place every feature call site goes through to obtain an
``AIProvider``. Provider SDKs remain confined to :mod:`ai_provider`.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from ai_provider import PROVIDERS, AIProvider, get_provider

_SDK_MODULES = {
    "gemini": "google.generativeai",
    "claude": "anthropic",
    "openai": "openai",
    "deepseek": "openai",
}


@dataclass(frozen=True, slots=True)
class ProviderInspection:
    """Server-verifiable provider facts without constructing an SDK client."""

    provider_name: str
    configured: bool
    available: bool
    reason: str | None = None


def inspect_provider(
    user_id: str | None = None,
    *,
    server_gemini_key: str | None = None,
    db_path: str = "workout_agent.db",
) -> ProviderInspection:
    """Inspect the authenticated user's provider configuration.

    This makes no network calls and does not instantiate a provider client, so
    it is safe to call from read-only readiness endpoints.
    """

    provider_name = "gemini"
    api_key: str | None = None
    if user_id is not None:
        from database import get_user_api_key, get_user_preferences

        prefs = get_user_preferences(user_id, db_path=db_path)
        provider_name = str(prefs.get("preferred_ai") or "gemini").lower().strip()
        if provider_name not in PROVIDERS:
            return ProviderInspection(
                provider_name,
                False,
                False,
                f"Unknown AI provider '{provider_name}'.",
            )
        key_record = get_user_api_key(user_id, provider_name, db_path=db_path)
        if key_record:
            api_key = str(key_record.get("api_key") or "").strip() or None

    if api_key is None and provider_name == "gemini":
        api_key = (server_gemini_key or "").strip() or None
    if not api_key:
        return ProviderInspection(
            provider_name,
            False,
            False,
            f"No {provider_name} key configured. Add one in Settings.",
        )

    module_name = _SDK_MODULES.get(provider_name)
    sdk_present = False
    if module_name is not None:
        try:
            sdk_present = importlib.util.find_spec(module_name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            sdk_present = False
    if not sdk_present:
        return ProviderInspection(
            provider_name,
            True,
            False,
            f"The {provider_name} provider SDK is unavailable on this server.",
        )
    return ProviderInspection(provider_name, True, True)


def resolve_provider(
    user_id: str | None = None,
    *,
    server_gemini_key: str | None = None,
    server_gemini_model: str | None = None,
    fallback_api_key: str | None = None,
    fallback_model: str | None = None,
    db_path: str = "workout_agent.db",
) -> AIProvider:
    """Return the provider selected by *user_id*.

    Resolution order:
    1. Read the user's ``preferred_ai`` and legacy ``ai_model`` preference.
    2. Read that user's encrypted key record and provider-specific model.
    3. Construct the provider exclusively through
       :func:`ai_provider.get_provider`.

    The optional server key is a backwards-compatible Gemini fallback only.
    It is never used for another provider or another user's credential.
    """
    server_gemini_key = server_gemini_key or fallback_api_key
    server_gemini_model = server_gemini_model or fallback_model

    provider_name = "gemini"
    model: str | None = None
    api_key: str | None = None

    if user_id is not None:
        from database import get_user_api_key, get_user_preferences

        prefs = get_user_preferences(user_id, db_path=db_path)
        provider_name = str(prefs.get("preferred_ai") or "gemini").lower().strip()
        if provider_name not in PROVIDERS:
            raise ValueError(
                f"Unknown AI provider '{provider_name}'. "
                f"Choose from: {', '.join(PROVIDERS)}",
            )

        preferred_model = prefs.get("ai_model")
        if isinstance(preferred_model, str) and preferred_model.strip():
            model = preferred_model.strip()

        key_record = get_user_api_key(user_id, provider_name, db_path=db_path)
        if key_record:
            api_key = str(key_record.get("api_key") or "").strip() or None
            if model is None:
                extra = key_record.get("extra")
                if isinstance(extra, dict):
                    stored_model = extra.get("model")
                    if isinstance(stored_model, str) and stored_model.strip():
                        model = stored_model.strip()

    if api_key is None:
        if provider_name != "gemini":
            raise ValueError(
                f"No {provider_name} key configured. "
                "Add a key in Settings -> AI Providers.",
            )
        api_key = server_gemini_key
    if model is None and provider_name == "gemini":
        model = server_gemini_model

    if not api_key:
        raise ValueError(
            "No AI provider key available. Set GEMINI_API_KEY as a server "
            "fallback or add your own key in Settings.",
        )

    return get_provider(provider_name, api_key=api_key, model=model)
