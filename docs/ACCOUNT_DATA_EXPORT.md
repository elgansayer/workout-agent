# Account data export

A signed-in user can download a portable archive of the data Workout Agent holds about their account. The export is built in the background, readable only by its owner, and deleted automatically when it expires.

The implementation is split so the disclosure boundary can be reviewed on its own:

| Module | Responsibility |
|---|---|
| `backend/account_export_sections.py` | **What is exported.** Reviewed, owner-scoped collections with explicit columns, provenance and schema versions. |
| `backend/account_export.py` | **How it is packaged and served.** Streaming ZIP builder, job service, bounded worker pool, maintenance. |
| `backend/account_export_store.py` | **Job and audit persistence.** Tenant-scoped SQL and lifecycle transitions. |
| `backend/webapp/app.py` | Five `/api/account/exports` routes. |
| `backend/scheduler.py` | `run_export_maintenance()` sweep in the agent container. |
| `frontend/src/app/components/settings/` | Settings → **Your data**. |

## User experience

1. Open **Settings → Your data** and choose **Request data export**.
2. The request returns immediately. The page shows the export as *Queued*, then *Building*, and polls until it is ready.
3. **Download** saves `workout-agent-export-YYYY-MM-DD.zip`. The page shows the size, the SHA-256 checksum and when the file expires.
4. **Delete** removes the archive straight away; an export that is still building is cancelled.

Only one export per account is built at a time: requesting again while one is in flight returns the same export. An account may request at most 5 exports in 24 hours and keeps at most the 3 newest ready exports (older ones expire early). Exports are available for 24 hours by default (`ACCOUNT_EXPORT_TTL_HOURS`).

## Archive format

The archive is a ZIP file. `manifest.json` describes everything else; each collection is one UTF-8 JSON document.

```text
manifest.json
profile/account.json              profile/preferences.json
programmes/programmes.json        programmes/state.json
workouts/hevy_workouts.json       workouts/exercise_progress.json
metrics/body_metrics.json
check_ins/check_ins.json
connectors/connections.json       connectors/google_health.json
connectors/hevy_routines.json     connectors/sync_state.json
notifications/notifications.json  notifications/push_subscriptions.json
coaching/chat_messages.json       coaching/reasoning_logs.json
coaching/dashboard_insight.json   coaching/correlation_report.json
coaching/daily_plans.json
audit/events.json
```

Every collection file has the same envelope:

```json
{
  "schema_version": 1,
  "section": "workouts",
  "collection": "exercise_progress",
  "description": "Top set and set count per exercise per session.",
  "provenance": {
    "origin": "derived_computation",
    "source": "Computed when a workout is imported.",
    "generator": "hevy_parser.parse_workout + database.save_progress",
    "derived_from": ["workouts/hevy_workouts"]
  },
  "records": [
    {"record_id": 1, "date": "2026-10-01", "exercise_name": "Back squat", "top_weight_kg": 100.0, "top_reps": 5, "sets": 3}
  ],
  "record_count": 1
}
```

`manifest.json` records the export format and schema version, the export id, the generation time, the account id, one entry per collection (path, description, schema version, record count, SHA-256 of the file, provenance) grouped by section, and the exclusions below. All collections are read from one read-only database snapshot, so related records are consistent with each other.

### Sections

| Section | Collections | Source of the records |
|---|---|---|
| Profile | `account`, `preferences` | `users`, `user_preferences` |
| Programmes | `programmes`, `state` | `programmes`, `programme_state` |
| Workouts | `hevy_workouts`, `exercise_progress` | `workout_history`, `exercise_progress` |
| Metrics | `body_metrics` | `body_metrics` |
| Check-ins | `check_ins` | `check_ins` |
| Connector metadata | `connections`, `google_health`, `hevy_routines`, `sync_state` | `user_api_keys` (metadata only), `hevy_meta` (allow-listed keys), `hevy_routines` |
| Notifications | `notifications`, `push_subscriptions` | `notifications`, `push_subscriptions` (host only) |
| Coaching history | `chat_messages`, `reasoning_logs`, `dashboard_insight`, `correlation_report`, `daily_plans` | `chat_messages`, `reasoning_logs`, `dashboard_insights`, `deep_correlations`, `daily_log` |
| Audit events | `events` | `account_audit_events` |

### Provenance and schema versions

Every collection declares a provenance `origin`:

| Origin | Meaning |
|---|---|
| `user_entered` | Typed or chosen by the account holder. |
| `connector_import` | Imported from an external provider (Hevy, Google Health, Health Connect). |
| `system_generated` | Written by the application (sync state, notifications, audit events). |
| `derived_computation` | Computed from other data (exercise progress, programme definitions). |
| `ai_generated` | Produced by the user's selected AI provider (insights, check-ins, plans, reasoning). |
| `mixed` | More than one of the above; the `source` text says which fields. |

`derived_computation` and `ai_generated` collections must also name their `generator` (the code that produced the records) and `derived_from` (the inputs, as collection paths or a description). The application does not store which AI provider or model produced a record, so those collections say so in `notes` rather than inventing a value. Programme records additionally carry the `engine_version` of the generator that built them.

Versioning policy: `schema_version` is per collection and `schema_version` in the manifest is for the archive layout. Adding an optional field keeps the version; renaming, removing or changing the meaning of a field increments it, and the change is recorded here.

## What is never exported

* **Credentials.** API keys, OAuth client secrets and refresh tokens, Google Health tokens and OAuth state, push-subscription keys, session secrets and encryption keys. A connector record reports only whether a credential is configured (`credential_configured`); the check is evaluated inside SQLite so the stored value never reaches Python.
* **Internal security data.** CSRF tokens, rate-limit records, session data, system prompts and operator diagnostics. Unclassified columns are internal by default (`docs/DATA_CLASSIFICATION.md`).
* **Other accounts' records.** Every collection query is bound to the authenticated account's id.

How this is enforced, in layers:

1. **Explicit queries.** A collection is a literal SQL string plus a record builder. There is no `SELECT *` and no generic table dump, so a new column is never exported by accident.
2. **Owner binding.** `Collection` refuses to be constructed unless its query filters on the owner column and its parameter count matches; the runner always passes the account id from the stored job, never a caller-chosen value.
3. **Allow-lists for shared tables.** `hevy_meta` also holds OAuth state and a Google Health refresh token, so only the keys in `SYNC_STATE_KEYS` are read. `user_api_keys.extra_json` is reduced to the model name. A push endpoint is reduced to its host because its path acts as a capability.
4. **Scrubbing of free-form documents.** Hevy payloads, programme definitions and insights are copied with credential-class keys (as classified by `data_classification.classify_field`) removed at any depth.
5. **Final guard.** Before a record is written, `find_credential_keys` refuses any credential-class key anywhere in it and fails the export with `policy_violation`.
6. **Read-only snapshot.** The builder opens the database with `mode=ro`, so it cannot modify data even if a query were wrong.

The derived analytics fields (`insight_json`, `insight_markdown`) are exported through reviewed DTOs (`dashboard_insight`, `correlation_report`) as `docs/DATA_CLASSIFICATION.md` requires; the global policy is not weakened.

## API

All routes require a verified, signed session. They never fall back to the anonymous legacy account that the dashboard's trusted-network mode uses for other routes.

| Method and path | Result |
|---|---|
| `POST /api/account/exports` | `202` with `{"export": {...}, "created": true}`. Returns the in-flight export with `created: false` if one exists. `429` with `Retry-After` when the 24-hour limit is reached; `503` when exports are disabled. |
| `GET /api/account/exports` | `{"exports": [...], "retention_hours": 24, "enabled": true}`, the 10 newest, newest first. |
| `GET /api/account/exports/{id}` | One export; poll this while it builds. |
| `GET /api/account/exports/{id}/download` | The ZIP as an attachment. `409` while building, `410` once expired, failed or unavailable, `503` when disabled. |
| `DELETE /api/account/exports/{id}` | Deletes the archive and cancels a build in progress. |

Unknown ids, ids that belong to another account, deleted exports and malformed ids all return the same `404 {"detail": "Export not found"}`, so an id cannot be probed. Responses are `private, no-store`. State-changing requests pass the existing CSRF and ownership guards, which also reject a body, query or header that claims a different owner.

An export in the API looks like this; it never includes the account id:

```json
{
  "id": "0f3c6d1e9a7b4c2d8e5f1a2b3c4d5e6f",
  "status": "ready",
  "schema_version": 1,
  "requested_at": "2026-11-10T12:00:00+00:00",
  "completed_at": "2026-11-10T12:00:03+00:00",
  "expires_at": "2026-11-11T12:00:03+00:00",
  "size_bytes": 11566,
  "checksum_sha256": "…",
  "record_counts": {"profile.account": 1, "workouts.hevy_workouts": 42},
  "error_code": null,
  "download_url": "/api/account/exports/0f3c6d1e9a7b4c2d8e5f1a2b3c4d5e6f/download"
}
```

`status` is one of `queued`, `running`, `ready`, `failed`, `expired`. A failed export carries a stable `error_code` (`build_failed`, `export_too_large`, `export_timeout`, `policy_violation`, `export_disabled`, `interrupted`) and never exception text.

## Lifecycle and reliability

```text
queued → running → ready → expired
   └────────┴──→ failed
any state → deleted
```

* **Asynchronous.** The request only inserts a `queued` row. A process-wide pool of two workers builds archives, so a burst of requests queues in the pool instead of occupying the web server's request threads. The archive is streamed to disk in 500-row batches, with a 256 MiB uncompressed limit and a 15-minute time limit.
* **One in flight per account** is a database invariant (a partial unique index), so two web replicas cannot both start a build for the same account.
* **Crash recovery.** A running job refreshes a heartbeat every few seconds. A job whose heartbeat is more than 2 minutes old (or a queued job that has waited more than 5 minutes) is failed as `interrupted` by request-time hygiene, the scheduled sweep or the web start-up sweep, which frees the account to request again.
* **Atomic publication.** The archive is written to `<id>.zip.partial` with mode `0600` and renamed only when complete. The size is checked again on download, so a truncated file is never served.
* **Expiry.** Expiry is enforced three ways: a download refuses an expired export even if its file still exists; every request cleans the caller's own expired exports; and the sweep deletes expired artifacts for every account, including owners who never return. The sweep also removes artifact-shaped files that no live job owns (after a 10-minute grace period) and prunes finished job rows after 30 days.

## Audit events

Export activity is recorded in `account_audit_events`, an append-only, tenant-scoped table (`user_id` is indexed first and cannot be rewritten): `account_export.requested`, `.completed`, `.failed`, `.downloaded`, `.deleted` and `.expired`. Events carry identifiers and outcomes only, never content or network details. They are exported to their owner under **Audit events**, so the export of an account lists the earlier exports of that account.

## Operations

| Variable | Default | Purpose |
|---|---|---|
| `ACCOUNT_EXPORT_ENABLED` | enabled | Kill switch. `0`, `false`, `no` or `off` stops creating and serving exports (a queued build fails with `export_disabled`; builds lost to the restart are failed as `interrupted`); listing and deleting keep working. Use it during a tenant-isolation incident (`docs/OPERATIONS_RUNBOOKS.md`). |
| `ACCOUNT_EXPORT_TTL_HOURS` | `24` | How long a ready export stays downloadable. Clamped to 1–168. |
| `ACCOUNT_EXPORT_DIR` | `account-exports/` beside `DATABASE_PATH` | Where archives are stored. The web and agent containers share one data volume, so the default needs no extra configuration. |

Operational notes:

* Archives contain health, workout and coaching data. Keep the directory on private storage, never behind a static file server, and **exclude it from filesystem-level backups**. Encrypted `.wab` backups (`docs/BACKUP_RESTORE_RUNBOOK.md`) cover the database only, so they never include archives. An archive is a secondary copy of user data and must not outlive its retention period.
* The directory is created with mode `0700`; archives are `0600` with random 128-bit names that are never derived from user input.
* The agent container's scheduler runs the sweep every 30 seconds, which is what removes the archive of an owner who never returns. Without an agent container (for example local development) the web process sweeps once at start-up and each export request cleans the caller's own expired exports, so an abandoned archive can outlive its retention until the next web restart. Run the agent container wherever exports are enabled.

## Adding or changing a collection

0. When a migration adds a table, `test_every_table_is_exported_or_explicitly_excluded` fails until the table is either read by a collection or listed in `NOT_EXPORTED_TABLES` with the reason. Decide deliberately; do not just silence the test.
1. Add a `Collection` to `COLLECTIONS` in `backend/account_export_sections.py` with a literal, owner-scoped query that lists its columns, a record builder with an explicit field list, a `schema_version` and a `Provenance`.
2. Never select a credential column. If a shared table mixes secrets and metadata, allow-list the rows (as `SYNC_STATE_KEYS` does) and keep secret values out of the `SELECT` list.
3. Run free-form JSON through `_decode_blob` so credential-class keys are scrubbed.
4. Classify any new column in `backend/data_classification.py::FIELD_CLASSES` (see `docs/DATA_CLASSIFICATION.md`).
5. Add synthetic seed data to `backend/tests/account_export_support.py` and extend `backend/tests/test_account_export_sections.py`; the cross-account and secret-exclusion tests then cover the new collection automatically.
6. Update this document and, if a field changed meaning, bump the collection `schema_version`.

## Known limits

* The canonical health-platform tables in `backend/health_schema.py` are a staged foundation that production schema initialisation does not create, so the export covers the `body_metrics` table the application actually writes. Add a collection when that platform is wired in.
* There is no account-deletion workflow yet. When one exists it must delete export artifacts and job rows with the rest of the account.
* Archives are protected by file permissions and the storage volume, like the database itself; they are not separately encrypted.

## Verification

```bash
.venv/bin/python -m pytest -q backend/tests/test_account_export_sections.py \
  backend/tests/test_account_export_store.py backend/tests/test_account_export.py \
  backend/tests/test_account_export_api.py
cd frontend && npm test -- --watch=false --include src/app/components/settings/settings.spec.ts
```

`test_account_export_api.py` is the end-to-end suite: it signs in two synthetic accounts, requests an export, polls the asynchronous job, downloads and inspects the real archive, and proves cross-account, anonymous, CSRF, expiry, deletion, failure and kill-switch behaviour against the live FastAPI application.
