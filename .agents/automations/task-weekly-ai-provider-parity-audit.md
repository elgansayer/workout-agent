# Weekly AI Provider Parity Audit

## Objective
Make sure "bring your own AI" is actually true across every provider, not
just Gemini — this is a core product promise (`AGENTS.md` §3/§6).

## Instructions
1. List every call site that generates AI text (`gemini_engine.py`'s prompt
   functions, `insight_cron.py`, `webapp/app.py`'s chat/RAG/XAI endpoints).
   For each, confirm it resolves the provider via the `ai-provider-wiring`
   skill's pattern (`get_user_preferences` → `get_user_api_key` →
   `ai_provider.get_provider()`) rather than a hardcoded SDK import. Track
   how many call sites are migrated vs. still hardcoded, and note the delta
   from last week.
2. Confirm all four providers in `ai_provider.py`'s `PROVIDERS` registry
   (`gemini`, `claude`, `openai`, `deepseek`) have their SDK dependency
   present in `requirements.txt` and importable in the venv used by both the
   `agent` and `web` containers.
3. Spot-check that `available_providers()`'s output still matches what the
   Settings UI (`webapp/templates/settings.html`) actually renders — a new
   provider added to the registry but not surfaced in the UI (or vice versa)
   is a silent product gap.
4. If a call site is found still hardcoded to Gemini, migrate it following
   the `ai-provider-wiring` skill as part of this task rather than only
   reporting it — this audit exists to make measurable weekly progress on
   the migration, not just re-discover the same gap indefinitely.
