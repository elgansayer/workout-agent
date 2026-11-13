# AI provider connection tests and model validation

Tracking issue: #798

Saving an API key and a free-text model name does not prove the provider can serve them. Settings now verifies the credentials **and** the model with the provider before anything is stored or activated, and tells the user what to fix in plain, non-secret language.

## What the user sees

| Action in Settings → AI Provider | What happens |
| --- | --- |
| **Test** next to the key field | Checks the typed key (or, with the field empty, the user's own stored key) and the model field with the provider. Nothing is saved. |
| **Save / Update** | The key is verified first. Only a key the provider accepts is written; otherwise the old key (if any) is kept and the reason is shown. |
| **Save AI preferences** | Changing the provider or model is verified with the stored key before it becomes active. Saving unrelated profile fields never triggers a check. |

A blank model field is checked against the registry default, exactly as it will be used at run time.

## API

### `POST /api/settings/ai/test`

Authenticated, CSRF-protected like every other mutation. The owner is always the session user; owner IDs in the body are rejected by the mutation guard.

```json
{ "provider": "claude", "api_key": "optional", "model": "optional" }
```

`api_key` is optional: when omitted, the caller's own stored key is tested (it is never returned). The response is HTTP 200 with a stable machine-readable `code`:

```json
{
  "status": "error",
  "provider": "claude",
  "model": "claude-sonnet-4-20250514",
  "code": "model_retired",
  "message": "'claude-sonnet-4-20250514' has been retired by Anthropic Claude and no longer works. Choose a current model, for example 'claude-sonnet-5-5'.",
  "retryable": false,
  "cached": false,
  "checked_at": "2026-11-11T12:00:00+00:00",
  "replacement_model": "claude-sonnet-5-5",
  "retry_after": null
}
```

Only malformed requests (`400`), missing authentication (`401`) and the throttles below (`429` with `Retry-After`) use error statuses.

### Saving and activating

`POST /api/settings/key` (AI providers only) and `POST /api/settings/preferences` (when the provider or model changes) run the same check and refuse to proceed on failure. The body keeps `detail` as a plain string so existing clients render it, and adds the structured fields above:

| Situation | Status |
| --- | --- |
| Key or model definitively unusable (`invalid_credentials`, `model_*`, ...) | `422` |
| Provider throttling or our per-user throttle | `429` + `Retry-After` |
| Provider unreachable, timed out, or client library missing | `503` |

Nothing is persisted on any of these: neither plaintext nor ciphertext, and a previously verified key stays untouched by a failed update. Preferences are applied atomically, so a refused model change does not apply the other fields in the same request.

With no stored key for the chosen provider there is nothing to verify against, so activation applies only the static model policy; the later key save performs the live check. Server-level fallback keys (`GEMINI_API_KEY`) are never tested on a user's behalf.

## Error codes

Every message is fixed text chosen by code. Provider response bodies, request IDs and exception messages are never returned, logged or chained, because SDK errors can quote part of the submitted key.

| Code | Meaning | What the user is told to do |
| --- | --- | --- |
| `missing_credentials` | No key typed and none stored | Enter or save a key |
| `credentials_malformed` | Whitespace/control characters or bad length | Paste only the key |
| `invalid_credentials` | Provider rejected the key (401, or Gemini's 400 "API key not valid") | Check the whole key was pasted and is not revoked |
| `permission_denied` | Key recognised but refused (403) | Key restrictions, account or region |
| `quota_exceeded` | No credit/quota (402, `insufficient_quota`, empty balance) | Add credit / check billing |
| `rate_limited` | Provider throttling (429) | Wait and retry (`Retry-After` honoured, capped at 5 min) |
| `model_invalid` | Not a plausible identifier | Fix the name |
| `model_unsupported` | Embedding, speech, image or Responses-only family | Choose a text chat model |
| `model_deprecated` | Provider announced a shutdown date that has not arrived | Switch before the stated date |
| `model_retired` | Shutdown date passed | Choose a current model |
| `model_not_found` | Provider does not offer the model to this key (404) | Check spelling / choose another |
| `invalid_request` | Other 4xx | Check the model name |
| `timeout`, `provider_unavailable` | Transient | Try again shortly |
| `sdk_unavailable` | Client library missing on the server | Operator must fix the deployment |
| `unknown` | Unclassified failure | Retry; check provider status |

`rate_limited`, `timeout`, `provider_unavailable` and `unknown` are `retryable`.

## Model policy

`backend/ai_model_policy.py` screens a model ID **before** any provider call is spent:

1. **Invalid** – not shaped like an identifier (`[A-Za-z0-9._:/-]`, ≤ 200 characters, no empty/`.`/`..` path segments, so an ID can never rewrite the URL an SDK builds from it).
2. **Unsupported** – non-text families that exist at the provider but cannot produce coaching text through the chat interface (embeddings, speech, image, moderation, realtime/audio, Gemini TTS/live/image).
3. **Deprecated / retired** – a lifecycle table of models the providers themselves announced as going away, with their shutdown dates. Before the date a model is *deprecated* (rejected, with the date); from the date it is *retired*.

Anything else is passed to the live check rather than guessed at, so a model works as soon as the provider serves it. User-named assets (Gemini `tunedModels/...`, OpenAI `ft:...`) are never judged by their names. Rejections suggest the provider's registry default as the replacement.

### Keeping the table current

The lifecycle table is reference data with a recorded review date (`POLICY_REVIEWED_ON`). It was last compared against:

* Anthropic: <https://platform.claude.com/docs/en/about-claude/model-deprecations>
* OpenAI: <https://developers.openai.com/api/docs/deprecations>
* Google: <https://ai.google.dev/gemini-api/docs/deprecations>
* DeepSeek: <https://api-docs.deepseek.com/updates>

To refresh it: add or adjust rows in `_LIFECYCLE` / `_UNSUPPORTED`, extend `tests/test_ai_model_policy.py`, bump `POLICY_REVIEWED_ON`, and confirm every registry default still passes (a test enforces this). The weekly provider-parity audit includes this review. The table is deliberately not exhaustive: a retired model it does not list is still caught by the live check as `model_not_found`.

### Registry defaults changed by this work

The policy cannot coexist with defaults it rejects, and two defaults were already unusable upstream:

| Provider | Previous default | Status at the provider | New default |
| --- | --- | --- | --- |
| `claude` | `claude-sonnet-4-20250514` | Retired 15 June 2026 | `claude-sonnet-5-5` |
| `deepseek` | `deepseek-chat` | Deprecated 24 April 2026, discontinuation announced for 24 July 2026; docs now say to use `deepseek-flash` | `deepseek-flash` |

`gemini-3.8-flash` and `gpt-4o` are not flagged by their providers and are unchanged. Stored per-user models are never rewritten; a user keeping a now-deprecated model keeps working until the provider turns it off, and is only asked to change it when they next change the key or the model.

## Rate limiting, caching and revalidation

Implemented in `backend/ai_connection.py`:

* **Rate limit** – at most **5 live provider checks per user per 60 s** (sliding window), on top of the existing per-address limit (10/min for the test route). Cache hits and static rejections are free and do not count.
* **Brief cache** – a success is reused for **5 minutes**, a definitive failure for **30 seconds**. Retryable failures (`rate_limited`, `timeout`, `provider_unavailable`, `unknown`) are never remembered. At most 512 entries are kept.
* **Revalidation** – the cache key is `(user, provider, model, HMAC(key))`. A different key or model is always a cache miss, so any key or model change is verified again; removing a key also drops its entries. The key is never held in the cache: only a per-process keyed HMAC fingerprint is.
* **Tenant isolation** – the user ID is part of every cache and rate-limit key and always comes from the session; two users submitting the same key never share a result.

The state is in-process. With several web replicas each replica enforces its own window and cache, so limits are per replica but never looser per user than configured.

## What is sent to the provider

One request, bounded to 16 output tokens, 15 seconds and no automatic retries, containing the fixed text `Reply with the single word: ok` and the user's own key (as the authentication header). No profile, workout, health or coaching data is ever included. Each adapter builds the request through the repository provider abstraction (`ai_provider.get_provider(...).probe()`); no feature module imports a provider SDK.

## Verification

```bash
.venv/bin/python -m pytest -q backend/tests/test_ai_model_policy.py \
  backend/tests/test_ai_connection.py backend/tests/test_ai_connection_routes.py \
  backend/tests/test_ai_provider.py
cd frontend && npx ng test --watch=false --include src/app/components/settings/settings.spec.ts
```

The suite uses real SDK exception types and real SDK clients over an in-memory HTTP transport, so request shape (token limit field, timeout, no retries) and error mapping are exercised without network access or credentials. Behaviour against the live provider APIs is **not** exercised by the automated suite; use **Test** with a real key after a deployment that changes an adapter.
