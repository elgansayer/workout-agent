---
name: ai-provider-wiring
description: 'Resolve a users chosen AI provider (Gemini/Claude/OpenAI/DeepSeek) and API key, then call it through ai_provider.py instead of hardcoding the Gemini SDK. Use when touching gemini_engine.py, insight_cron.py, or any webapp/app.py endpoint that generates AI text (chat, RAG search, XAI reasoning, insights).'
---

# AI Provider Wiring

## Why This Exists

`ai_provider.py` already defines a clean `AIProvider` ABC + `get_provider()`
factory supporting Gemini/Claude/OpenAI/DeepSeek, and the Settings UI already
lets a user save a key per provider plus a `preferred_ai`/`ai_model`
preference. But per `AGENTS.md` §3/§7, nothing calls `get_provider()` outside
`ai_provider.py` itself — every real generation call hardcodes
`google.generativeai` against one shared server key. This skill is the
standard pattern for closing that gap one call site at a time.

## When to Use

- Adding a new AI-generated feature (new prompt, new endpoint).
- Touching an existing hardcoded-Gemini call site in `gemini_engine.py`,
  `insight_cron.py`, or `webapp/app.py` (`/api/xai_reasoning`,
  `/api/project_peak`, `/api/rag_search`, chat endpoints).
- Adding a new provider (e.g. a local/self-hosted model) to
  `ai_provider.py`.

## Procedure

1. **Resolve the provider for the acting user**, don't assume Gemini:

   ```python
   from ai_provider import get_provider

   def resolve_provider(user_id: str) -> AIProvider:
       prefs = database.get_user_preferences(user_id)
       provider_name = (prefs and prefs.get("preferred_ai")) or "gemini"
       model = prefs and prefs.get("ai_model")
       api_key = database.get_user_api_key(user_id, provider_name)
       if api_key is None:
           # Fall back to the server's own key only for the default
           # provider — never silently use the server key for a provider
           # the user explicitly configured but whose key lookup failed.
           if provider_name != "gemini":
               raise ValueError(
                   f"No {provider_name} key configured for this user"
               )
           api_key = config.gemini_api_key
       return get_provider(provider_name, api_key, model)
   ```

   Put this resolver in one place (a new small module, e.g.
   `ai_resolver.py`, or a function in `ai_provider.py` itself) rather than
   duplicating it at every call site.

2. **Replace the hardcoded SDK call** with
   `resolve_provider(user_id).generate(prompt, stream=...)`. Preserve the
   existing prompt-building logic (`COACHING_RULES`, insight formatting,
   etc.) — only the "which client sends this prompt" part changes.

3. **Handle provider-specific failure modes generically.** `AIProvider`
   implementations already raise on missing SDK (`ImportError`) or bad key
   (whatever the underlying SDK raises) — catch broadly at the call site and
   surface a user-facing error ("Your DeepSeek key looks invalid — check
   Settings") rather than a stack trace, matching the existing
   `/api/settings/verify-hevy` pattern for connector verification errors.

4. **Threading `user_id` through.** Most of these call sites currently take
   no `user_id` parameter because the whole app is single-tenant (see the
   `multi-tenant-migration` skill) — you may need to add the parameter as
   part of this change. Don't invent a second, parallel way to pass user
   identity; use the same `user_id` the route handler already has from the
   session, or that `main.py` resolves per the scheduler-job skill.

5. **Adding DeepSeek support** (if not already added): DeepSeek exposes an
   OpenAI-compatible API — add a `DeepSeekProvider` to `ai_provider.py`
   reusing the `openai` SDK client with
   `base_url="https://api.deepseek.com"` and default model
   `deepseek-chat`, register it in `PROVIDERS`. Add `openai` to
   `requirements.txt` if not already present (needed for both `OpenAIProvider`
   and `DeepSeekProvider`).

## Verification

Run the `verification-gate` skill's steps. Additionally: write a test that
calls the resolver with a mocked `database.get_user_preferences`/
`get_user_api_key` for at least two different providers and asserts
`get_provider` is invoked with the right provider name and key — this is
currently zero-coverage territory (`AGENTS.md` §7), don't add more of it
uncovered.

## Gotchas

- Never fall back to the server's shared key for a *non-default* provider —
  that would silently bill the operator's Claude/OpenAI/DeepSeek account for
  a request the user thought was using their own key.
- `.generate(..., stream=True)` returns an `Iterator[str]`, not a string —
  don't `await`/index it like the non-streaming path; check how
  `webapp/app.py`'s existing `/api/rag_search` streaming response is wired
  and match that pattern for any new streaming endpoint.
- Provider SDKs (`anthropic`, `openai`) are optional-at-runtime
  (`ClaudeProvider`/`OpenAIProvider`/`DeepSeekProvider` raise `ImportError`
  from inside `__init__`, not at module import time) — keep it that way so a
  Gemini-only deployment doesn't need every SDK installed.
