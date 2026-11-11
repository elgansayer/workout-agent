"""Resolve a user's preferred AI provider, encrypted key, and model.

This is the single place every feature call site goes through to obtain an
``AIProvider``. Provider SDKs remain confined to :mod:`ai_provider`.
"""

from __future__ import annotations

from ai_provider import PROVIDERS, AIProvider, get_provider
from secret_vault import (
    CredentialUnavailableError,
    ResolvedCredential,
    SecretVault,
    UnavailableReason,
)
from tenancy import TenantRequiredError, implicit_single_user_allowed


def _user_credential(
    user_id: str,
    provider: str,
    db_path: str,
) -> ResolvedCredential | None:
    """Return the owner's credential, or ``None`` when none is configured.

    A revoked or undecryptable credential is never replaced by a shared server
    fallback: only an absent credential may fall through to the optional Gemini
    server key.
    """
    try:
        return SecretVault(db_path).resolve(
            user_id,
            provider,
            purpose="ai_generate",
        )
    except CredentialUnavailableError as exc:
        if exc.reason is UnavailableReason.NOT_CONFIGURED:
            return None
        raise ValueError(str(exc)) from exc


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

    An AI call always acts for an owner. Omitting *user_id* is accepted only in
    the explicit local single-user mode (:mod:`tenancy`); anywhere else it
    raises :class:`~tenancy.TenantRequiredError` instead of quietly handing the
    shared server key to a caller nobody authenticated.
    """
    if user_id is None:
        if not implicit_single_user_allowed():
            raise TenantRequiredError(
                "an AI provider is resolved for an authenticated user: implicit "
                "single-user access is disabled outside an explicit "
                "non-production runtime."
            )
    elif not isinstance(user_id, str) or not user_id.strip():
        raise TenantRequiredError("user_id must be a non-empty string.")
    server_gemini_key = server_gemini_key or fallback_api_key
    server_gemini_model = server_gemini_model or fallback_model

    provider_name = "gemini"
    model: str | None = None
    api_key: str | None = None

    if user_id is not None:
        from database import get_user_preferences

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

        credential = _user_credential(user_id, provider_name, db_path)
        if credential is not None and credential.api_key is not None:
            api_key = credential.api_key.reveal()
            if model is None and credential.model:
                model = credential.model

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
