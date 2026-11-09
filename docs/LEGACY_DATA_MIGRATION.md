# Legacy single-user data adoption

Before the multi-user launch the production database holds records that belong to no real account: rows with a null `user_id`, and rows owned by the synthetic `legacy@local` tenant that `database.init_db()` creates and backfills (`docs/DATABASE_MIGRATIONS.md`). Several fallbacks still resolve to that tenant when no account is named: the web app when Google sign-in is not configured (`_check_api_auth`), the coaching run's default user (`_resolve_run_user`), the insight jobs' default user (`_resolve_insight_user`) and the `user_id or get_legacy_user_id()` defaults in `database.py`. Leaving the data there keeps it reachable by those fallbacks.

`tools/legacy_data_migration.py` re-owns that data to **one explicitly named production account**, once and reversibly. It is an operator tool, not application code: no web route or job calls it.

## Guarantees

- **Explicit target.** `--target-user-id` is required. The account must already exist (it must have signed in once) and must not be the legacy tenant. Nothing is inferred, including "the first account".
- **Refuses ambiguity.** If ownership is unclear the tool exits non-zero before it writes a backup or changes a row (see [Refusals](#refusals)).
- **Re-owns, never deletes.** Only `user_id` changes. Every table keeps its exact row count and the check runs before the commit. The one exception: if the target already has an active programme, adopted legacy programmes are stored inactive so the account keeps exactly one active programme.
- **Session state is left alone.** `CSRFMiddleware` creates `csrf_tokens`, which also has a `user_id` column, in the production database. Its single-use rows live for minutes and are bound to one signed session, so they are not user records: the tool never re-owns, counts or deletes them (`EPHEMERAL_TABLES`). A `user_id` table that is in neither `USER_OWNED_TABLES` nor `EPHEMERAL_TABLES` refuses the run.
- **Backed up first.** A verified encrypted `.wab` backup (`tools/backup_restore.py`) is mandatory. It is written while the tool holds SQLite's only write lock and before the first row changes. The backup path must be new; an existing backup is never overwritten.
- **All or nothing.** The adoption is one transaction. A failed check rolls it back to the untouched database.
- **Evidence.** Per-table before/after counts for the legacy, target and other owners, `integrity_check` and `foreign_key_check` before and after, and the backup digest are printed and, with `--evidence`, written as JSON (mode `0600`, never overwritten, no email addresses, no secrets).
- **Idempotent.** When nothing is left to adopt the run changes nothing, writes no backup and reports `noop`.

## Preconditions

1. The current release has started once against the database so `init_db()` has added `user_id` to every tenant table.
2. The target account has signed in. Run the migration **before** that account connects providers or logs data: adopted rows join, but are not deduplicated with, records the target already owns (reported as `merged_tables`). Keep the scheduler and worker stopped from the target's first sign-in until the migration has run: they process every account, so a cycle in between can create rows for the target (for example a daily insight) that share a unique key with the legacy copy and make the tool refuse.
3. Every writer is stopped (web, worker and scheduler). The tool waits up to 30 seconds for the write lock and then refuses; it cannot stop a writer that starts after it finishes. Run it as the user that owns the database file and its directory so SQLite's WAL side files are not left owned by someone else.
4. A backup keyring is configured exactly as in `docs/BACKUP_RESTORE_RUNBOOK.md` (`BACKUP_ENCRYPTION_KEYS`; never pass keys on the command line). Without it the tool exits `2` and changes nothing.
5. A current backup has been restore-drilled, as `docs/DATABASE_MIGRATIONS.md` requires before any production migration.

## Procedure

Find the target id with a read-only query:

```bash
sqlite3 -readonly /data/workout_agent.db "SELECT id, email, created_at FROM users ORDER BY created_at"
```

Run the adoption from the repository root:

```bash
python tools/legacy_data_migration.py migrate \
  --database /data/workout_agent.db \
  --target-user-id ACCOUNT_ID \
  --backup-output /srv/workout-agent-backups/pre-legacy-adoption-$(date -u +%Y%m%dT%H%M%SZ).wab \
  --evidence /srv/workout-agent-evidence/legacy-adoption-$(date -u +%Y%m%dT%H%M%SZ).json
```

Exit status: `0` migrated or `noop`; `1` refused or failed (the database is unchanged); `2` missing keyring or arguments.

Review the printed table: every row must show `legacy` falling to `0` and `target` rising by the same amount, with `Integrity: ok -> ok`. Copy the backup off-host as the backup runbook describes, keep the evidence with the change record, then restart the services.

### Evidence fields

| Field | Meaning |
| --- | --- |
| `status` | `migrated` or `noop` |
| `before` / `after` | per table: `total`, `legacy` (null or legacy-owned), `target`, `other` |
| `rows_reassigned` | rows whose owner changed |
| `programmes_deactivated` | legacy programmes stored inactive because the target already had an active one |
| `merged_tables` | tables where adopted rows joined records the target already owned |
| `retained_default_rows` | re-seeded defaults left with the legacy tenant (below) |
| `integrity_before` / `integrity_after` | `integrity_check` result and `foreign_key_check` violation count |
| `backup` | path, creation time, key id and database digest, or `null` for a `noop` |

## Refusals

Every refusal exits `1` with the database unchanged.

| Condition | Meaning and next step |
| --- | --- |
| Target missing, empty or the legacy tenant | Supply the real account id. |
| A tenant table lacks `user_id`, or has a `user_id` this tool does not adopt | Start the current release once; if a new table with a `user_id` column was added, classify it in the same change: `USER_OWNED_TABLES` for user records, `EPHEMERAL_TABLES` for transient session state (tests enforce this). |
| `integrity_check` or `foreign_key_check` fails | Includes rows owned by an account that no longer exists. Repair under the data-loss runbook in `docs/OPERATIONS_RUNBOOKS.md`. |
| Rows owned by an account other than the legacy tenant and the target | Another account already owns data, so the legacy data cannot be attributed to the first production account. Do not override; review with the privacy/security owner. |
| A legacy row shares a unique key with a row the target owns | For example a saved API key for the same provider, or the same programme template. Resolve in the product while only the target holds it, or escalate. Do not hand-edit production rows. |
| Backup cannot be written or verified, its row counts differ from the database, or the path exists | Fix storage or keyring, choose a new path. An unverified backup is deleted. |
| The write lock is unavailable | A writer is still running. Stop it and retry. |

## Idempotency and re-seeded defaults

`init_db()` re-creates two default rows for the legacy tenant on every start: its `programme_state` row and its `hevy_meta` `programme_start_date`. They therefore reappear after the application restarts. When the target already owns the same key, the legacy copy is not data to adopt: it stays with the legacy tenant (never merged or deleted) and is listed as `retained_default_rows`, so re-running after a restart is a `noop`. Any other key collision is a refusal. If `retained_default_rows` is non-empty on a **first** run, the target had already started a programme; compare its day and start date with the legacy values in the backup before relying on the account.

## Rollback

Reversibility follows `docs/BACKUP_RESTORE_RUNBOOK.md`; nothing is merged by hand.

1. Stop every writer.
2. `python tools/backup_restore.py verify --backup PRE_MIGRATION.wab`
3. `python tools/backup_restore.py restore --backup PRE_MIGRATION.wab --output /data/workout_agent.restored.db`
4. Cut over the restored file atomically while writers are stopped, as in the production recovery procedure.

Records written after the migration are not in that backup; the evidence timestamp bounds the loss window.

## After the migration

- Sign in as the target: history, dashboard and Settings show the adopted data. Sign in as a different account: it shows none of it.
- The legacy tenant row remains. It holds no adopted records, only the two defaults that `init_db()` re-seeds at each start, so the legacy fallbacks above see no user data.
- Connector credentials taken from the environment (`HEVY_API_KEY`, the Google Health variables, the Health Connect file) are applied only to the legacy tenant by `_scope_config_to_user`. Rows in `user_api_keys` move with the adoption and are re-sealed for the target (ciphertext is bound to the owner id), so `ENCRYPTION_KEY` must be set when running the tool; a credential that cannot be opened for its current owner refuses the migration. The tool lifts the `user_api_keys` owner-immutability trigger only inside its own transaction and recreates it before commit. Authentication and audit tables (`user_sessions`, `user_identities`, `credential_audit_events`, `csrf_tokens`) are never touched. Any credential that exists only in the environment must be saved in the target account's Settings.
- The scheduler runs a cycle for every account, including the legacy tenant, so the legacy tenant can accumulate new rows from environment credentials. A repeat run adopts them under the same checks.
- Known limitation, outside this migration: the older Google Health helpers (`google_health_client.py`, `google_health_auth.py`) still call `get_meta`/`set_meta` without a `user_id`. `get_meta` without one reads the first matching row of any account, so that path is not tenant-safe until it is scoped.

## Tests

```bash
.venv/bin/python -m pytest -q tools/test_legacy_data_migration.py \
  backend/tests/test_legacy_data_adoption_schema.py \
  backend/tests/test_legacy_data_adoption_http.py \
  backend/tests/test_database.py
```

- `tools/test_legacy_data_migration.py` builds the real tenant schema with `init_db()` and fills every user-owned table. It proves, through the production read API, that the target sees the adopted records while a new account and the legacy tenant see none; that restoring the backup reproduces the exact pre-migration contents; that a second run and a run after a database restart change nothing; that each refusal leaves the database and backup path untouched; that a failed check rolls back; and that the command-line entry point works, fails closed without keys and never prints them.
- `backend/tests/test_legacy_data_adoption_schema.py` builds the schema production really has (`init_db()` plus the `csrf_tokens` table `CSRFMiddleware` adds), proves every `user_id` table is classified, that adoption succeeds and leaves the session tokens untouched, and fails when a new module starts creating tables so its tables get classified.
- `backend/tests/test_legacy_data_adoption_http.py` proves over the session-authenticated HTTP API that only the target reads the adopted records, and that a second account, a later account, the legacy tenant and the anonymous fallback read none of them.
- `backend/tests/test_database.py::test_complete_legacy_schema_is_adopted_by_the_first_account_only` runs the whole path on a database upgraded from the complete pre-tenant schema, the shape the production database has.
