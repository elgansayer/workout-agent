# Per-account quotas and abuse controls

Shared infrastructure (the server AI key, the SQLite database, the Hevy and Google APIs) is protected from accidental or malicious overuse by per-account quotas. Every limit is:

- **tenant-aware** - counted for the authenticated account taken from the signed session, never for a client-supplied identifier;
- **observable** - visible to the account holder, to administrators, in logs and in the database;
- **configurable by entitlement** - limits come from the account's entitlement, can be tuned per deployment and can be overridden per account;
- **actionable** - a refusal says which limit applied and when to try again;
- **audited** - administrator overrides, entitlement changes and abuse-review decisions are recorded in an append-only log.

Code: `backend/quotas.py` (engine), `backend/quota_admin.py` (audited administrator actions), `backend/webapp/quota_api.py` (HTTP adapter). Tables are created by `init_db`.

## Quotas and where they are enforced

| Quota | Protects | Enforced at |
| --- | --- | --- |
| `login` | session minting and Google token-exchange calls | `GET /login/google` and `GET /auth` per validated client address (the callback is throttled *before* any token exchange); then per account, before a session is issued |
| `ai_request` | the AI providers and the shared server key | `GET /api/rag_search`, `GET /api/xai_reasoning/{id}` (only when a provider call is needed) and `GET /api/project_peak`, after provider resolution so an unconfigured request is not charged |
| `import` | the Hevy API | every Hevy source fetch: `GET /api/programmes` and `POST /api/programmes/preview`, `/activate`, `/select` |
| `export` | writes to the user's Hevy account | the daily agent's routine push (`main._sync_hevy_routines`) |
| `sync` | the full Hevy history rebuild | `POST /api/settings/sync-history` |
| `notification` | push services and the notification table | `POST /api/notifications/test-coach` and the daily agent's Web Push delivery (`main._deliver`) |
| `report` | CPU and database cost of aggregate reports | `GET /api/stats` and `GET /api/progress` |

A request that cannot reach the protected resource is not charged: a missing Hevy key, a missing AI key, an invalid explanation id and a cached explanation consume nothing.

## Default limits

Each cell is one or more rules; a request is allowed only if **every** rule has room (sliding window).

| Quota | `restricted` | `standard` | `extended` |
| --- | --- | --- | --- |
| `login` | 5/hour | 20/hour | 60/hour |
| `ai_request` | 3/min, 20/day | 15/min, 300/day | 30/min, 1000/day |
| `import` | 2/min, 10/hour | 10/min, 60/hour | 30/min, 200/hour |
| `export` | 1/hour, 2/day | 3/hour, 10/day | 10/hour, 30/day |
| `sync` | 1/5 min, 2/day | 2/5 min, 10/day | 6/5 min, 30/day |
| `notification` | 2/hour, 5/day | 10/hour, 50/day | 30/hour, 150/day |
| `report` | 6/min, 100/day | 30/min, 1000/day | 90/min, 5000/day |

Sign-in requests also share a per-address allowance before any account is known: 60 per 5 minutes and 300 per hour. IPv6 addresses are counted per /64 and IPv4-mapped IPv6 addresses as IPv4. The address is the proxy-validated client address (`request.state.validated_client_ip`, see `docs/REVERSE_PROXY_SECURITY.md`); a raw `X-Forwarded-For` header is never trusted. Behind a reverse proxy, set `WEB_TRUSTED_PROXY_CIDRS` so each client is counted on its own; without it every client shares the proxy's address allowance.

An account without a profile row is `standard`.

## How the limit for a request is chosen

1. The account's **entitlement** (`account_quota_profiles`) selects the rule set. An unrecognised stored value fails closed to `restricted`.
2. `QUOTA_POLICY_OVERRIDES` (below) may replace the rules of one entitlement/quota pair for the whole deployment.
3. A per-account **override** (`quota_overrides`) scales every rule of one quota by `limit_percent` (minimum limit 1) until it expires. `0` switches the quota off.

## What a refusal looks like

| Situation | Status | `Retry-After` | Body |
| --- | --- | --- | --- |
| Limit reached | `429` | seconds until one more request fits | `{"detail": "AI requests are limited to 15 per minute. Try again in 45 seconds.", "error": "quota_exceeded", "quota": {"kind": "ai_request", "limit": 15, "window_seconds": 60, "retry_after_seconds": 45}}` |
| Quota switched off for a bounded time | `429` | seconds until it ends | as above with `"error": "quota_disabled"` |
| Quota switched off indefinitely | `403` | none | `"error": "quota_disabled"`, `detail` says to contact the administrator |
| Limits cannot be checked | `503` | `5` | `"error": "quota_unavailable"` |

When several rules deny, the longest wait is reported. Browser navigations (`Accept: text/html` outside `/api/`, such as the OAuth callback) receive a small HTML page with the same message instead of JSON.

The Coach chat and the progress and stats pages show the server's `detail` when it is a quota message.

## Failing closed

If the quota tables cannot be read or written, the request is refused with `503` and the protected call (AI provider, Hevy, push service) is not made. The scheduled agent skips the push or routine export instead of sending unmetered. An invalid `QUOTA_POLICY_OVERRIDES` stops the web service from starting.

## Observability

- `GET /api/quotas` returns the signed-in account's entitlement, each quota's rules with `used`, `remaining` and `resets_in_seconds`, and any active override. It never includes another account's data.
- `GET /api/admin/quotas/overview` (administrators) returns allowed and refused volume per quota for the last 24 hours and the abuse-review queue.
- Refusals are logged at `WARNING` by `quotas` (at most one line per second per account or address and quota): kind, code, limit, window, retry time and a non-reversible reference (no email, id or address).
- A refused request is answered from a read-only snapshot (it never takes the database write lock) and is tallied best-effort in `quota_denials`: at most one tally per account and quota per second, one row per hour. A flood of refusals therefore cannot become write contention for other users, and the abuse queue counts tallied refusals.

## Administration and audit

An administrator is a signed-in account whose email is listed in `QUOTA_ADMIN_EMAILS` (case-insensitive). With the variable empty nobody is an administrator, and a deployment running in anonymous mode has none. The acting account always comes from the signed session; the affected account is a path identifier validated against `users`. Request bodies, queries and headers that name an owner (`account_id`, `user_id`, ...) are still rejected by the mutation guard, and unknown body fields are refused.

| Route | Body | Audit `action` |
| --- | --- | --- |
| `GET /api/admin/quotas/overview` | - | - |
| `GET /api/admin/quotas/audit?account=<id>&limit=<1-200>` | - | - |
| `POST /api/admin/quotas/accounts/{account_id}/entitlement` | `{"entitlement": "restricted\|standard\|extended", "reason": "..."}` | `entitlement_set` |
| `POST /api/admin/quotas/accounts/{account_id}/override` | `{"kind": "...", "limit_percent": 0-1000 or null, "expires_in_seconds": 60-7776000, "reason": "..."}`; a null `limit_percent` clears the override | `override_set` / `override_clear` |
| `POST /api/admin/quotas/accounts/{account_id}/abuse-review` | `{"decision": "dismiss\|restrict\|restore", "reason": "..."}` | `abuse_review` |

Production wraps these `POST` routes with CSRF protection; send the `X-CSRF-Token` obtained from `/api/csrf-token` as the web client does.

Every change and its audit record commit in **one transaction**, so a change cannot exist without its audit entry, and `quota_audit_log` rejects `UPDATE`. A reason of 3 to 500 printable characters is required and is normalised to a single line. Each entry stores the acting account, the affected account, the action, the reason, a UTC timestamp and a small JSON `detail` (`previous`/`current` state; for reviews also the denial evidence).

### Abuse review

1. `GET /api/admin/quotas/overview`: accounts with at least 20 tallied refusals (so at least 20 separate seconds of refused requests) in 24 hours are queued, with their refusals per quota.
2. Decide: `dismiss` keeps the entitlement, `restrict` applies `restricted`, `restore` returns to `standard`.
3. `POST .../abuse-review`. The audit entry records the evidence, then the account's refusal tallies are cleared, so it returns to the queue only after fresh refusals accumulate.
4. For temporary relief or a time-boxed block use `.../override` (for example `limit_percent: 0` with `expires_in_seconds` to pause exports).

## Configuration

| Variable | Purpose |
| --- | --- |
| `QUOTA_ADMIN_EMAILS` | Comma-separated administrator emails. Empty: no administrators. |
| `QUOTA_POLICY_OVERRIDES` | Optional JSON: `{"entitlements": {"<entitlement>": {"<quota>": [[limit, window_seconds], ...]}}, "login_by_address": [[limit, window_seconds], ...]}`. Limits 1-1,000,000, windows 1-604,800 seconds, 1-4 rules per quota with distinct windows. Replaces the rules of each named pair. |
| `WEB_AUTH_SECRET` | Also keys the hash of the client address stored for sign-in throttling. |

## Stored data

`account_quota_profiles`, `quota_overrides`, `quota_usage`, `quota_denials` and `quota_audit_log` carry an immutable `user_id` (foreign key to `users`, user-first index; updating `user_id` is rejected). `login_attempts` holds only a keyed hash of the client address and a timestamp. Usage rows are pruned after their longest window (at least 24 hours), refusal tallies after 30 days and sign-in rows after their longest window. Only allowed requests write usage rows, so the tables stay bounded by the limits themselves. No request payloads, prompts or provider data are stored. Usage and sign-in tables are operational and safe to truncate to reset counters; do not delete audit rows outside a documented retention process.

## Scope notes

- The only production `export` today is the agent's push of routines to Hevy. The account data export (#771) and any future download or provider-push route must call `quotas.consume(user_id, QuotaKind.EXPORT, db_path)` before producing data.
- Credential and settings mutations (`/api/settings/key`, `/preferences`, `/push-subscribe`, `/verify-hevy`, Google Health connect/disconnect) keep the earlier in-process per-address limiter; moving them under account quotas is a separate change.
- Server-scheduled AI jobs (daily insight, weekly correlations) are bounded by their schedule and are not charged to the interactive allowance.
- Quota state lives in the shared SQLite file (single writer). A different database backend must keep the atomic check-and-record contract.

## Verification

```bash
.venv/bin/python -m pytest -q backend/tests/test_quotas.py backend/tests/test_quota_admin.py backend/tests/test_webapp_quotas.py backend/tests/test_main.py
cd frontend && npm exec -- ng test --watch=false --include='src/app/shared/api-errors.spec.ts'
```
