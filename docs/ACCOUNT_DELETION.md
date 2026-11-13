# Account and health-data deletion

A signed-in user can erase their **health data**, or **close their account** and erase everything, from **Settings → Account and data** (also linked from the Account page). Both actions are confirmed by typing an exact phrase, report progress, and leave verifiable audit evidence. The implementation is `backend/account_deletion.py`; the HTTP surface is in `backend/webapp/app.py`.

Nothing here replaces the operator's own legal duties. Read [Retention exceptions](#retention-exceptions) before telling users what is and is not erased.

## What the user can do

| Action | Erases | Confirmation | Timing |
| --- | --- | --- | --- |
| **Erase health data** (`health_data`) | Body metrics; canonical health tables; Google Health tokens and other health-source credentials; every AI-derived insight, correlation, reasoning log, Coach chat message and check-in; retained exports | Type `DELETE HEALTH DATA` | Immediate. The account, workouts, programmes and notifications stay. |
| **Close account** (`account`) | Everything in the table below, then the account itself | Type the account's email address | Scheduled `ACCOUNT_DELETION_GRACE_DAYS` ahead (default 7); cancellable until then |

Confirmation text is compared case-insensitively with whitespace collapsed. The server supplies the exact text (`GET /api/account/deletion`), and the buttons stay disabled until it matches. The shared single-user tenant (`legacy@local`, used when web authentication is off) can never be closed: it has no authenticated owner.

Health data is erased together with AI-derived content because that content quotes and summarises it; keeping it would keep the data.

## Lifecycle

```text
confirm ─▶ pending ─(window ends)─▶ in_progress ─▶ completed
              │                         │
              ├─▶ cancelled             └─▶ failed ─(retry after back-off)─▶ in_progress
```

| Request state | Account gate | Web app | Scheduled jobs | Cancel |
| --- | --- | --- | --- | --- |
| none, `cancelled`, or a finished `health_data` request | `open` | normal | normal | n/a |
| `pending` | `pending` | fully usable | **paused** | yes, until `execute_after` |
| `in_progress` | `locked` | `423` for everything except the deletion status endpoint and the read-only `GET /api/settings` (the Settings page hosts the progress view) | paused | no |
| `failed` | `locked` | `423` as above; stays fenced until an operator or a retry finishes it | paused | no |
| `completed` (`account`) | `closed` | `401` for every route; the session is cleared | the `users` row no longer exists | no |

* Only one open (`pending`, `in_progress` or `failed`) request may exist per account, enforced by a partial unique index.
* **Who runs it.** A request that is already due (health data, or a zero-day window) runs inside the `POST`. Otherwise the scheduler process executes it: `scheduler.run_account_deletions()` runs at the start of every `run_once` and on every 30-second loop iteration. **Deferred closures therefore need the scheduler running.** Web replicas never schedule anything.
* **Claiming.** `next_attempt_at` is the due time for `pending`, the lease expiry (15 minutes, extended after each step) for `in_progress`, and the retry time (5 minutes) for `failed`. A request is claimed with one atomic `UPDATE`, so two workers cannot both run it, and a crashed worker is resumed once its lease expires. After 5 attempts a request stays `failed` for an operator.
* **Idempotent steps.** Each step is its own short transaction. Re-running a step is safe and its row count accumulates.
* **Verified.** Each step re-counts what it should have removed in the same transaction and fails the request on any residue. The account is closed only by a final transaction that holds the write lock, re-verifies every step, deletes the `users` row, and writes the tombstone. A late write that slips in between sweeps triggers another pass (up to 3) instead of closing the account with data attached.
* **Stragglers.** An in-flight chat stream or coaching run that began before closure can still write once. For 15 minutes after closure every scheduler tick re-erases such rows (`sweep_recent_tombstones`). New work is blocked at the source: `get_all_users()` excludes closing accounts, and `main.run`, the daily insight and the weekly correlations return early for them.
* **Fail closed.** The signed session cookie is stateless, so `_check_api_auth` consults the ledger on every request, and the CSRF layer refuses to mint tokens for a closed account.

## What is erased

Every statement is `… WHERE user_id = ?` with the owner taken from the session or from the persisted request row, never from the client. The manifest is `USER_DATA_STORES` in `backend/account_deletion.py`; the tests below fail if a table with a `user_id` column is missing from it.

| Step | Stores | Health scope |
| --- | --- | --- |
| `queued_jobs` | There is no persistent job queue. Scheduled work is fenced (see above); the step verifies the account can no longer be scheduled. | – |
| `notifications` | `notifications`, `push_subscriptions` | – |
| `connector_tokens` | `user_api_keys` (Hevy, AI providers, health providers), `hevy_meta` (which also holds the Google Health refresh token and any legacy OAuth state, check-in counters, the routine folder id and the connector operation and last-sync status described in `READINESS.md`), `health_connections` | Only health-provider keys, `google_health_*` meta rows and `health_connections` |
| `derived_insights` | `dashboard_insights`, `deep_correlations`, `reasoning_logs`, `chat_messages`, `check_ins` | All |
| `primary_records` | `workout_history`, `exercise_progress`, `daily_log`, `programme_state`, `programmes`, `user_preferences`, `hevy_routines`, `body_metrics`, `health_metrics`, `health_sync_runs` | `body_metrics`, `health_metrics`, `health_sync_runs` |
| `caches` | `csrf_tokens` (per-user single-use CSRF nonce registry) | – |
| `search_indexes` | None exist. The step fails the request if an unregistered virtual (FTS) table appears. | – |
| `retained_exports` | The directory `<database directory>/exports/<user id>/` | Yes |
| `unregistered_stores` | Any other table with a `user_id` column that is not in the manifest is swept and reported, so a forgotten table cannot silently survive | – |
| `account_identity` | The `users` row, then the tombstone is written in the same transaction | – |

Browser-side state cannot be reached from the server, so the closing response carries `Clear-Site-Data: "cache", "cookies", "storage"` and the app unsubscribes the browser's push subscription. The in-process rate-limit counters are keyed by network address, not by user, and hold no account data.

### Retained exports contract

No export feature persists files today. Any feature that retains an export artefact **must** keep it under `<database directory>/exports/<user id>/`; deletion removes that directory (unlinking, never following, a symlink) and the verifier fails the request if it survives. Identifiers that are not a safe single path component are never mapped to a path.

### Append-only and immutable tables

A table guarded by a "no delete" or "append-only" trigger blocks erasure. The engine never drops or bypasses a guard: the step fails, the request is reported as `failed` with `database_error`, and the account stays fenced instead of claiming success. Keep the guard and give it a tenant-scoped exemption for accounts being deleted:

```sql
CREATE TRIGGER trg_<table>_no_delete BEFORE DELETE ON <table>
WHEN NOT EXISTS (SELECT 1 FROM account_deletion_requests
                 WHERE user_id = OLD.user_id AND state = 'in_progress')
 AND NOT EXISTS (SELECT 1 FROM account_tombstones WHERE user_id = OLD.user_id)
BEGIN SELECT RAISE(ABORT, '<table> is immutable'); END;
```

The `in_progress` request covers the live workflow; the tombstone covers `reconcile`, which writes the tombstone first in the same transaction. Other tenants' rows stay protected. `test_append_only_tables_are_erased_only_for_the_account_being_deleted`, `…_when_a_restore_is_reconciled` and `test_a_blocked_erasure_fails_the_request_instead_of_hiding_the_rows` pin this contract. If a security requirement obliges you to keep an append-only log (for example a credential audit trail), do not leave it in the manifest silently: record it here as a retention exception, keep it free of personal data, and bound its retention.

## Retention exceptions

These are the only things deliberately kept, and each is bounded:

1. **Deletion tombstone and receipt** (`account_tombstones`, `account_deletion_requests`, `account_deletion_steps`). Holds the opaque former account id, the request id (the receipt reference shown to the user), timestamps, the policy version, and per-step row counts. It holds **no** email, name, health, workout or credential data, and the account is not active: it cannot authenticate, receive scheduled work, or be resumed. *Purpose:* prove the erasure happened, fence stale sessions, and let a restore be reconciled. *Retention:* `ACCOUNT_TOMBSTONE_RETENTION_DAYS` (default 365, never below the 35-day backup baseline); `prune_expired_tombstones` then deletes the tombstone and its receipt. Earlier ledger rows of the closed account are dropped when it closes.
2. **Encrypted backups.** Erased data persists in backups until they expire under [`BACKUP_RESTORE_RUNBOOK.md`](BACKUP_RESTORE_RUNBOOK.md) (35-day baseline, never fewer than the 14 newest). Backups are not rewritten. A restore **must** be followed by the reconcile procedure below, or erased accounts reappear.
3. **Other systems that already hold a copy.** Hevy, the selected AI provider (Gemini, Claude, OpenAI, DeepSeek), Google, browser push services and any connected health provider keep copies under their own policies. Deletion removes this application's stored tokens but does **not** call those providers to revoke access or delete data; users should revoke Workout Agent in each provider's own settings. This matches the existing "disconnect" behaviour.
4. **Operational logs.** Audit lines carry the receipt id and scope only, never an account id or email. Application and proxy logs follow the deployment's log retention; they are not rewritten.
5. **Append-only audit trails** (`security_audit_events`, `credential_audit_events`). These hash-chained security and credential events record the tenant and action (never secret material, health data or prompts) and are deliberately not erased on account closure, so an account's security history remains attributable. The deployment operator sets and applies their retention as described in [`SECURITY_AUDIT_LOGGING.md`](SECURITY_AUDIT_LOGGING.md) and [`PRIVACY.md`](PRIVACY.md).

An account that signs in again with the same email after closure is a **new** account with a new id; nothing is carried over.

## HTTP API

All routes require a signed-in session (the owner is never taken from the request), send `Cache-Control: private, no-store`, and mutations carry the usual CSRF token. A body or header naming another account is rejected by the mutation guard before the route runs.

| Route | Purpose |
| --- | --- |
| `GET /api/account/deletion` | `eligible`, the required confirmation text and window per scope, and the latest request with its steps and `progress_percent`. Works while locked so progress stays visible. |
| `POST /api/account/deletion` `{"scope": "account" \| "health_data", "confirmation": "…"}` | Create the request. `200` if it already completed, otherwise `202`. For a completed closure the session is cleared and `Clear-Site-Data` is sent. |
| `POST /api/account/deletion/cancel` | Cancel the open `pending` request while its window is open. |

Errors use `{"detail": "<code>"}`: `400 confirmation_mismatch`, `403 not_eligible`, `404 not_found`, `409 already_requested` / `not_cancellable`, `422` (malformed body or unknown scope), `423` (locked), `401` (unauthenticated or closed), `429` (rate limit). A `failed` run is reported as `202` with `state: "failed"` and a non-sensitive `error_code` (`residual_data`, `database_error`, `filesystem_error`, `internal_error`, `not_eligible`), and is retried automatically.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ACCOUNT_DELETION_GRACE_DAYS` | `7` (0–30) | Cancellation window for account closure; `0` erases immediately. Health-data erasure is always immediate. |
| `ACCOUNT_TOMBSTONE_RETENTION_DAYS` | `365` (35–3650) | How long a tombstone and its receipt are kept. |

Out-of-range values are clamped and logged; non-numeric values fall back to the default.

## Operator runbook

**Find stuck or failed requests**

```sql
SELECT id, scope, state, attempts, error_code, next_attempt_at
FROM account_deletion_requests WHERE state IN ('in_progress', 'failed');
```

The receipt id is the `id`; ids are opaque and safe to share with the user. A `pending` request past `execute_after` means the scheduler is not running.

**Retry a request that exhausted its attempts** (after fixing the cause, e.g. disk space or permissions):

```sql
UPDATE account_deletion_requests
SET attempts = 0, next_attempt_at = '1970-01-01T00:00:00.000000+00:00'
WHERE id = '<request id>' AND state = 'failed';
```

The next scheduler tick resumes it. Never delete a `failed` row to "unstick" an account: that would lift the fence while data remains.

**After restoring a backup**, deletions made since the backup was taken are undone. Re-apply them:

```bash
# 1. Before restoring, from the current (or protected pre-restore) database:
python backend/account_deletion.py --database /path/to/current.db list-tombstones > tombstoned-ids.txt
# 2. Restore the backup (BACKUP_RESTORE_RUNBOOK.md).
# 3. Re-erase those accounts and re-create their tombstones in the restored database:
python backend/account_deletion.py --database /path/to/restored.db reconcile tombstoned-ids.txt
```

The file holds opaque account ids only. Run `list-tombstones` against a copy if the original must stay read-only evidence. Carry the list across successive restores until the tombstones' retention period has passed. `reconcile` is idempotent, refuses the shared `legacy@local` tenant, and fences ids that are absent from the backup so a stale session cannot recreate rows.

## Verification

* `backend/tests/test_account_deletion.py` – manifest coverage, erasure and tenant isolation (every table is found by schema, not by the manifest), health vs account scope, confirmation, cancellation window, fencing, retries and crash recovery, verification failure, late writers, exports and symlinks, tombstone contents and retention, the CSRF/job/scheduler consumers, and the restore reconcile.
* `backend/tests/test_account_deletion_api.py` – authentication, ownership claims, caching headers, every status code, lock-out while erasing, closed-session behaviour, and the legacy tenant.
* `frontend/src/app/components/settings/settings.spec.ts` – the Account and data section (confirmation gating, progress, cancel, failure and sign-out paths, accessibility hooks).

**Adding a user-owned table?** Add a `DataStore` to `USER_DATA_STORES` (set `health=True` if the health scope must erase it, and `owner=` if the table links to an account through a column other than `user_id`, such as `accepted_user_id`), seed it in `_seed()` in `test_account_deletion.py`, and update the table above. The runtime sweep and the coverage test only find tables that have a `user_id` column, so a table owned through any other column **must** be registered explicitly or it will survive deletion. `test_every_user_owned_table_is_registered_and_seeded` fails until you do, and at runtime the `unregistered_stores` sweep erases any table you forgot.

## Known limitations

* No upstream revocation or provider-side deletion (see exception 3).
* Closure is confirmed in the active session; there is no separate re-authentication or email notification step. The cancellation window is the safeguard against a hijacked session.
* The user is not told when a deferred closure completes: the receipt id shown when it is scheduled is the reference to quote to the operator.
