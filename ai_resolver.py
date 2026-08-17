"""Resolve a user's preferred AI provider, key, and model.

This is the single place every call site should go through to get an
AIProvider, per AGENTS.md §3.  Never call a provider SDK directly from
feature code — go through ``resolve_provider()``.
"""

from __future__ import annotations

import logging

from ai_provider import AIProvider, get_provider

logger = logging.getLogger(__name__)


def resolve_provider(
    user_id: str | None = None,
    *,
    fallback_api_key: str | None = None,
    fallback_model: str | None = None,
) -> AIProvider:
    """Return an AIProvider for *user_id*, falling back to server defaults.

    Resolution order:
    1. If *user_id* is given, look up their ``preferred_ai`` / ``ai_model``
       preferences and their API key for that provider.
    2. If the user has no key of their own (or *user_id* is ``None``), fall
       back to *fallback_api_key* / *fallback_model* — these come from the
       server's ``GEMINI_API_KEY`` / ``GEMINI_MODEL`` env vars.

    Rules (per the ai-provider-wiring skill):
    - Never silently use the server key for a *non-default* provider.  If the
      user explicitly chose Claude but has no Claude key stored, raise
      ``ValueError`` rather than billing the operator's Gemini/Claude key.
    - If the user has no preference and no key of their own, fall back to
      Gemini with the server key (the legacy single-tenant default).
    """
    provider_name = "gemini"
    model: str | None = None
    api_key: str | None = None

    if user_id is not None:
        from database import get_user_api_key, get_user_preferences

        prefs = get_user_preferences(user_id)
        if prefs:
            provider_name = prefs.get("preferred_ai") or "gemini"
            prefs_model: str | None = prefs.get("ai_model") or None  # type: ignore[assignment]
            model = prefs_model
        key_record = get_user_api_key(user_id, provider_name)
        if key_record:
            api_key = key_record.get("api_key", "")

    if api_key is None:
        # No user key configured — only fall back to the server key for Gemini
        if provider_name != "gemini":
            raise ValueError(
                f"No {provider_name} API key configured for this user. "
                "Add one in Settings before switching providers."
            )
        api_key = fallback_api_key
    if model is None:
        model = fallback_model

    if not api_key:
        raise ValueError(
            "No API key available — set GEMINI_API_KEY in the server environment "
            "or add your own key in Settings."
        )

    return get_provider(provider_name, api_key=api_key, model=model)