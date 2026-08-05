# Daily Security Audit

## Objective
Catch authorization, injection, and secret-handling regressions before they
reach a public deployment.

## Instructions
1. Review all `webapp/app.py` routes added or changed in the last 24 hours
   (`git log --since=1.day -p -- webapp/app.py`). Confirm every route that
   reads/writes user data relies on the auth middleware (or has an explicit,
   justified allow-list entry) — per the `fastapi-route` skill.
2. Grep the same diff window for raw SQL string concatenation
   (`f"...{variable}..."` or `%` formatting feeding into `execute()`/`cursor.execute()`)
   instead of parameterised `?` placeholders — flag and fix any instance
   found.
3. Grep for anything that might log or return a secret in plaintext
   (`api_key`, `client_secret`, `refresh_token`, `ENCRYPTION_KEY`,
   `WEB_AUTH_SECRET` near a `logging.*`/`print()`/JSON response) — per the
   `secrets-and-encryption` skill.
4. Confirm any new external-facing endpoint that calls an AI provider or
   connector goes through the existing rate limiter (`_check_rate_limit`).
5. If a genuine vulnerability is found and the fix is small and
   unambiguous, fix it directly. If the fix is large or requires a judgment
   call (e.g. a broader auth model change), file a clearly-titled `task_add()`
   entry instead of a partial fix.

## 2026-08-05 Audit Results
- **Auth middleware**: ✅ All routes behind AuthMiddleware; allow-list correct.
- **SQL injection**: ✅ All SQL uses parameterised `?` placeholders; no f-string or %-formatting with execute().
- **Secret exposure**: ✅ No secrets exposed in logging, print(), or JSON responses.
- **AI rate limiting**: ✅ All AI/connector endpoints (`xai_reasoning`, `project_peak`, `rag_search`, `verify_hevy`) have `_check_rate_limit`.
