# AI provider parity audit

Audit date: 2026-09-18  
Tracking issue: #737  
Comparison baseline: `main` at `cf55e30f3565927d764165b21decb3327b10698a` on 2026-09-11

## Result

All nine active AI text-generation call sites are provider-neutral. No feature module imports a provider SDK directly; SDK imports remain confined to `backend/ai_provider.py`. The ninth is the user-initiated Settings credential test, which resolves the acting user's sealed key through the same provider abstraction.

| Measure | 2026-09-11 baseline | 2026-09-18 | Delta |
| --- | ---: | ---: | ---: |
| Provider-neutral active call sites | 9 | 8 | -1 |
| Hardcoded provider call sites | 0 | 0 | 0 |
| Unsafe direct prescription writers | 1 | 0 | -1 |

The historical reduction from nine to eight was intentional, not a parity regression. The removed call asked an LLM for free-form JSON and wrote the result directly into active Hevy routines. That path bypassed the deterministic dynamic-programme validation and activation boundary, so routine sync now keeps the validated deterministic prescription. AI-generated programme changes must go through the Hevy-native preview and activation flow. The current inventory lists nine call sites because the credential-vault Settings test adds one explicit, user-initiated provider call; it still resolves through the canonical abstraction and cannot write domain state.

No prior weekly report existed in the repository. The comparison was reconstructed from the stated `main` commit; `main` had not changed between the two audit dates.

## Generation call-site inventory

A call site counts as migrated only when the acting user is carried into the canonical resolver, which performs `get_user_preferences` -> `secret_vault.SecretVault.resolve` -> `ai_provider.get_provider`. The vault decrypts only the acting user's sealed key and audits credential use; it never falls back to another tenant's credential. Functions in `gemini_engine.py` consume an already-resolved `AIProvider`; their production callers own resolution.

| # | Feature and production caller | Generation function | Provider resolution |
| ---: | --- | --- | --- |
| 1 | Scheduled training day (`main.run`) | `gemini_engine.generate_next_workout` | `main._resolve_provider` -> canonical resolver |
| 2 | Scheduled rest day (`main.run`) | `gemini_engine.generate_rest_day_message` | Same user-scoped instance as the training-day path |
| 3 | Periodic check-in (`checkin.run_checkin`) | `gemini_engine.generate_checkin_message` | Canonical resolver with check-in `user_id` and database path |
| 4 | Daily dashboard insight (`insight_cron.generate_daily_header`) | `AIProvider.generate` | `insight_cron._resolve_provider` -> canonical resolver |
| 5 | Weekly correlations (`insight_cron.generate_weekly_correlations`) | `AIProvider.generate` | `insight_cron._resolve_provider` -> canonical resolver |
| 6 | XAI explanation (`GET /api/xai_reasoning/{context_id}`) | `AIProvider.generate` | Authenticated user through `_resolve_provider_for_request` |
| 7 | Peak projection (`GET /api/project_peak`) | `AIProvider.generate` | Authenticated user through `_resolve_provider_for_request` |
| 8 | Coach/RAG stream (`GET /api/rag_search`) | `AIProvider.generate(stream=True)` | Authenticated user through `_resolve_provider_for_request` |
| 9 | Credential test (`POST /api/settings/key/test`) | `AIProvider.generate` | Authenticated user through `secret_vault.SecretVault` and the provider factory |

`GET /api/chat/history` and `POST /api/chat/clear` manage tenant-scoped conversation persistence but do not generate text, so they are not counted separately.

## Provider and container matrix

`available_providers()` is projected from the same registry used by `get_provider()`.

| Registry ID | Adapter SDK | Python import | Default model | Agent image | Web image |
| --- | --- | --- | --- | --- | --- |
| `gemini` | `google-generativeai` | `google.generativeai` | `gemini-3.8-flash` | shared requirements | shared requirements |
| `claude` | `anthropic` | `anthropic` | `claude-sonnet-4-20250514` | shared requirements | shared requirements |
| `openai` | `openai` | `openai` | `gpt-4o` | shared requirements | shared requirements |
| `deepseek` | `openai` (OpenAI-compatible API) | `openai` | `deepseek-chat` | shared requirements | shared requirements |

Both `Dockerfile` and `Dockerfile.web` install `backend/requirements.txt`. Import checks run against the repository virtual environment. The production Portainer definition supplies the same mandatory `ENCRYPTION_KEY` to both services so the agent can decrypt credentials saved by the web service; an environment Gemini key is now only an optional fallback.

The legacy Gemini SDK resolves clients lazily from process-global configuration. The Gemini adapter serialises configuration and generation so concurrent users cannot bind a request to another tenant's API-key client.

## Settings parity

The Angular Settings page renders the provider list and default model placeholders returned by `GET /api/settings`; neither layer keeps a second hardcoded provider list. The API response is derived from `available_providers()`, returns only masked credential status and lifecycle metadata, and never returns a stored secret after submission. Saving preferences persists both the selected provider and its current model. Request-time provider resolution is deliberately uncached so credential and preference changes take effect without a process restart.

Settings also proves a key and model before they are stored or activated: a **Test** action, verification on key save and on provider/model changes, per-user rate limiting, a brief tenant-scoped result cache and a static deprecated/unsupported-model policy. The contract, error codes and operating notes are in [AI provider connection tests](AI_PROVIDER_CONNECTION_TESTS.md).

The production scheduler now invokes coaching and insights once per persisted user. Each coaching run resolves that user's encrypted Hevy key and Google Health token through `secret_vault`; server-level legacy Hevy and Google Health refresh tokens are restricted to the deployment owner (the verified account the pre-account data was assigned to, or the local tenant in the explicit non-production mode; see `docs/USER_MODEL.md`) so a shared fallback cannot cross tenant boundaries. A revoked or undecryptable credential fails with an explicit reason rather than falling back to a server key.

## Repeatable checks

```bash
rg -n "provider\.generate" backend --glob '*.py' --glob '!backend/tests/**'
rg -n "from (google|anthropic|openai)|import (google|anthropic|openai)" \
  backend --glob '*.py' --glob '!backend/tests/**'
.venv/bin/python -m pytest -q backend/tests/test_ai_provider.py \
  backend/tests/test_insight_cron.py backend/tests/test_webapp.py
cd frontend && npm test -- --watch=false
```
