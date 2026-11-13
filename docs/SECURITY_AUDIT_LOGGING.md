# Security audit logging and anomaly alerts

Sensitive account and integration changes are written to an append-only,
hash-chained audit trail (`security_audit_events`), and deterministic alert rules
watch that trail for abuse. This page is the contract for what is recorded, what
is never recorded, how tampering is detected, and how operators use it.

Code: `backend/security_audit.py` (store, chain, sanitiser), `backend/security_alerts.py`
(rules), `backend/webapp/audit_events.py` (request glue),
`backend/webapp/request_correlation.py` (request ids),
`backend/scripts/verify_security_audit.py` (operator verifier).

## What is recorded

Every row has an **actor**, a **subject**, a UTC **time**, a **request correlation
id**, an **outcome**, a **severity** and flat **safe metadata**.

| Column | Meaning |
| --- | --- |
| `actor_type` / `actor_id` | `user` (verified session), `anonymous` (no verified session), `system` (alerts) or `operator`. |
| `user_id` | Tenant that owns the affected account/data. `NULL` for unauthenticated or deployment-wide events. Always taken from the signed session, never from a body, query string or header. |
| `subject_type` / `subject_id` | What was acted on: `account`, `identity`, `session`, `credential`, `connector`, `data`, `permission`, `source`, `system`. |
| `occurred_at` | UTC ISO-8601 with microseconds. |
| `request_id` | The `X-Request-ID` the response carried, so a user or operator can quote it. Server-generated per request; an inbound `X-Request-ID` is ignored. |
| `outcome` | `success`, `failure`, `denied`, or `raised` (alert events only). |
| `severity` | `info`, `warning` or `critical`. |
| `source_ref` | Keyed pseudonym of the validated client address (never the address). |
| `metadata_json` | Flat, bounded, secret-scrubbed key/value pairs. |

### Coverage

| Required event | Event type | Recorded by |
| --- | --- | --- |
| Login | `auth.login` / `success` | `GET /auth` after the session is established |
| Failed login | `auth.login` / `failure`, `denied` | `GET /auth`: token-exchange failure, missing identity, e-mail outside `ALLOWED_EMAILS` (the attempted address is stored only as a keyed pseudonym) |
| Session revocation | `auth.session_revoked` | `GET /logout` for a verified session |
| Key changes | `credential.saved`, `credential.deleted` | `POST /api/settings/key`, `POST /api/settings/key/delete` |
| Connector changes | `connector.connected`, `connector.disconnected`, `connector.sync`, `connector.test` | Hevy key save/delete, `POST /api/settings/verify-hevy`, `POST /api/settings/sync-history`, Google Health `callback` and `disconnect` |
| Deletion | `data.deleted` | `POST /api/chat/clear`, the only user-data deletion the product exposes today (there is no account or health-data deletion flow yet) |
| Permission changes | `permission.changed` | `POST /api/settings/push-subscribe` (notification-delivery permission granted) and `POST /api/settings/preferences` when the selected AI provider, which decides who receives coaching context, actually changes |
| Recovery requested | `account.recovery_requested` | `POST /api/auth/recovery/request`. Metadata records only whether a token was issued; the address is never stored. |
| Recovery completed | `account.recovered` | `POST /api/auth/recovery/complete`: success, or denied for an invalid/expired/replayed token. |
| E-mail change requested | `account.email_change_requested` | `POST /api/account/email/change-request` after the verification and previous-address warning are queued. |
| E-mail changed | `account.email_changed` | `POST /api/account/email/confirm`: success, or denied for an invalid/expired/replayed token. |
| Export | `data.exported` | `GET /api/admin/accounts/{id}/export` after `data.export` is authorised. |
| Admin access | `admin.access` | Every authorised or denied `/api/admin/*` permission check. Role changes also emit `permission.changed`. |

Export and admin access now have production surfaces behind the central matrix in
`docs/ROLE_PERMISSIONS.md`. A denied permission check records `admin.access` /
`denied` with the closed-vocabulary permission name and opaque account ids, never
the target's address or data. `health_export.HealthExport` and
`training_export.ExportedWorkout` remain validation envelopes with no caller,
and `health_delete` is still unwired.

Requests rejected before a route runs (authentication boundary, CSRF, owner-claim
mismatch, rate limit) are not audited: they never reach an action, and recording
them would let unauthenticated traffic write rows. Repeated sign-in failures,
which can be triggered anonymously, are recorded but bounded (see below).

### Never recorded

Credentials and secrets (API keys, OAuth codes and tokens, session cookies, push
endpoints/keys, encryption or signing keys), raw provider error text, raw e-mail
addresses and IP addresses, health or workout payloads, and prompts.

Defence in depth in `sanitise_metadata`: keys are classified with
`data_classification` (credential, identifier, health, workout and prompt fields
are replaced by a `[REDACTED:...]` marker), nested or non-scalar values are
replaced, and string values are scrubbed of bearer tokens, JWTs, provider-style
keys, UUIDs, e-mail addresses, URL query strings, `key=value` secrets and long
opaque runs, then truncated. Failure `reason` values are fixed codes, and only
`type(exc).__name__` is kept for unexpected exceptions. Callers must still choose
metadata keys deliberately: a key such as `workouts_found` is classified as
workout data and would be redacted.

## Tamper evidence

* **Append-only:** triggers abort any `UPDATE` or `DELETE`, so the tenant column
  and every other field are immutable through the application connection.
* **Hash chain:** `event_hash` digests every column plus `prev_hash`, the sequence
  number and the key id. Edits, reordering and removals break verification from
  that row onward. `AUTOINCREMENT` sequence numbers are never reused, so removing
  tail rows leaves a permanent gap even after later events are appended.
* **Keyed digest:** with key material the digest is HMAC-SHA-256. A party that
  can only write to the database cannot recompute the chain. Without key material
  (local development only) rows still chain with SHA-256, which detects accidental
  damage but not a deliberate rewrite, and client-address pseudonyms are then
  brute-forceable. A keyed chain cannot be downgraded: an `unkeyed` row after any
  keyed row fails verification (`key_downgrade`).
* **External anchor:** each stored event logs
  `security_audit seq=… event_type=… outcome=… severity=… request_id=… event_hash=…`.
  Ship those lines off-host. An `event_hash` captured there before an incident
  proves the chain up to that row has not been rewritten, even by someone who
  holds the key, because the database copy must still reproduce that hash.

### Keys

| Source | Used when |
| --- | --- |
| `AUDIT_LOG_HMAC_KEY` (≥ 32 characters) | Set. A shorter value fails web startup. Recommended for production because it survives session-secret rotation. |
| `WEB_AUTH_SECRET` (≥ 32 characters) | `AUDIT_LOG_HMAC_KEY` is unset. A purpose-separated key is derived, so production (which requires this secret) is always keyed. |
| none | Local development only; rows are `unkeyed`. |

The `key_id` stored on each row is a one-way fingerprint of the derived key. After
a key change, older rows cannot have their digest recomputed. The verifier still
checks their sequence and links but, because a forged `key_id` would look the
same, it **fails by default** when any row cannot be authenticated. After a
deliberate rotation, run it once with each key and pass `--allow-unverifiable` so
every segment is checked by the key that signed it. The agent/scheduler process
shares the same database and must receive the same key if it ever writes events.

### Bounded growth

Unauthenticated endpoints must not be able to fill the database. Anonymous events
are limited to 30 per source and event type per 10 minutes (suppressed events are
logged as `security_audit_suppressed`), and alert events are de-duplicated per
rule, subject and severity for 30 minutes. Both limits sit far above the alert
thresholds, so detection is unaffected. Two limits remain: a flood spread across
many sources is bounded only by what the reverse proxy lets through, so rate-limit
`/auth` at the edge; and behind a proxy whose address is not listed in
`WEB_TRUSTED_PROXY_CIDRS` every client shares the proxy's address, so the
per-source rules and budget then aggregate all clients. There is deliberately no
automatic pruning; any retention process must be a reviewed operator procedure
that records a signed checkpoint, because the chain cannot be shortened without
detection.

### Availability

Recording never raises into a request: a storage failure is logged at `ERROR`
(`security_audit_write_failed event_type=…`) and the user action proceeds. Alert
evaluation failures are logged the same way. A malformed `AUDIT_LOG_HMAC_KEY`
fails startup instead of silently disabling the chain.

## Alert rules

Rules run after each stored event, count events in a fixed window ending at the
triggering event, and append an `alert.*` event (outcome `raised`) plus a
`security_alert alert_type=… severity=… subject_type=… seq=… request_id=…` log
line. Alert rows are part of the chain.

| Alert type | Rule | Window / threshold | Severity |
| --- | --- | --- | --- |
| `alert.repeated_auth_failures` | Failed or denied `auth.login` from one pseudonymised source | 5 in 10 min | warning |
| `alert.repeated_auth_failures` | Failed or denied `auth.login` against one pseudonymised identity (credential stuffing from many sources) | 5 in 10 min | warning |
| `alert.production_auth_disabled` | `APP_ENV` is production but authentication is incomplete or anonymous access is enabled. Checked at startup and whenever an anonymous request would be served. Production already refuses to start in this state, so this is a tripwire for a bypassed or regressed guard. | immediate | critical |
| `alert.unusual_export_activity` | Successful `data.exported` by one account | 3 in 60 min | warning |
| `alert.unusual_export_activity` | Any denied `data.exported` (for example a cross-tenant export attempt) | 1 in 60 min | critical |
| `alert.connector_abuse` | Failed `connector.connected` / `connector.sync` / `connector.test` for one account | 5 in 10 min | warning |
| `alert.connector_abuse` | Successful connect/disconnect churn for one account | 8 in 10 min | warning |

Thresholds are module constants (`DEFAULT_RULES`) with unit tests; change them
in code review, not at runtime. Alerts reach operators through the chain, the
`security_alert` log marker (route it to your alerting stack: `critical` alerts
log at `CRITICAL`, `warning` alerts at `WARNING`) and the verifier's recent-alert
listing. They are not shown to end users in the UI.

## Operating the audit trail

Verify the chain (for example nightly) with the same environment as the web
service:

```bash
python backend/scripts/verify_security_audit.py --db /data/workout_agent.db --require-keyed
```

Exit status `0` means every row was verified, `1` means tampering or damage (the
output names the first failing sequence number and reason: `sequence_gap`,
`broken_link`, `hash_mismatch`, `tail_truncated` or `key_downgrade`) or a row that
cannot be authenticated, and `2` means the database or key configuration is
unusable. `--require-keyed` additionally fails when no key material is configured
or any row is unkeyed; use it for deployments whose chain has always been keyed
(a database that began unkeyed in development will contain unkeyed rows).
`--allow-unverifiable` accepts rows signed with a key that is not configured, for
use after a deliberate rotation. `--json` emits the same result for automation.
Treat a failure as a security incident: preserve the database and the shipped
`event_hash` log lines, then follow `docs/OPERATIONS_RUNBOOKS.md`.

The audit table is part of the database backup. Restoring a backup restores its
chain; events written after the backup are lost from the table, and the shipped
log lines then show which events occurred and their hashes (not their content).

## Privacy and tenancy

`security_audit_events` is operational security data. It stores pseudonymous
references rather than addresses and attempted e-mail addresses, and no
credentials, health data or prompts. It is append-only and is not purged by the
application; see `docs/PRIVACY.md` for the retention statement. The tenant column
`user_id` has a tenant-first index, is never taken from client input, and the
pre-authentication events that have no tenant carry `NULL`. The account-scoped
rules (export and connector) count only that account's events, so one account's
activity cannot trigger or mask another's alert; the sign-in rules are scoped to a
pseudonymised source or identity instead. There is intentionally no HTTP read
endpoint yet; the operator verifier reads across tenants because it requires
direct database access.

## Verification

```bash
.venv/bin/python -m pytest -q backend/tests/test_security_audit.py \
  backend/tests/test_security_alerts.py backend/tests/test_request_correlation.py \
  backend/tests/test_security_audit_routes.py backend/tests/test_verify_security_audit_script.py
```
