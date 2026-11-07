# Database migration safety

`backend/database.py::init_db()` is the compatibility migration path for the
SQLite deployment. It must remain safe for a brand-new database, a database
created before tenant scoping, and a database where an earlier startup stopped
part-way through schema initialization.

## Invariants

- All schema changes run inside the transaction managed by `_connect()`.
- An `ALTER TABLE ... ADD COLUMN` is guarded by `PRAGMA table_info`.
- Existing unscoped rows are held by the single reserved `legacy@local`
  holding account, which is created only when such rows exist. In
  production it is `unclaimed`: it cannot sign in or be scheduled, so the data
  is inaccessible until an operator assigns it to a verified owner through the
  explicit plan/apply flow in `docs/USER_MODEL.md`. Only the explicit
  non-production local mode keeps it `active`. Backfills update only rows whose
  `user_id` is null, so retrying initialization does not duplicate or reassign
  data.
- The user-model upgrade is additive: `users` gains `status`,
  `status_changed_at`, `updated_at` and `last_login_at` behind
  `PRAGMA table_info` guards; `user_identities` and the append-only
  `legacy_ownership_migrations` audit table are created with
  `IF NOT EXISTS`. `users.id` and `users.created_at` are immutable,
  `retired`/`deleted` accounts can never change status, and identity columns
  are immutable, all enforced by triggers that every start reinstalls.
- Every table in `database.USER_OWNED_TABLES` has insert/update triggers that
  reject a row without an owner, so an ownerless write fails whichever code
  path issues it. A fresh production database holds no accounts and no seed
  rows.
- The only non-additive step is assigning legacy rows to the verified owner. It
  is a single transaction guarded by a plan digest and a backup attestation;
  rollback is a restore of the pre-assignment backup.
- A table rebuild uses a `*_new` table, parameterized copies, and a
  drop/rename in the same transaction.
- Every user-owned table has a foreign key to `users(id)` and an index whose
  first column is `user_id`. A composite primary key or unique constraint
  satisfies this requirement when it matches the query shape; frequently
  queried history tables also use explicit `idx_<table>_user_...` indexes.
- Seed rows use `INSERT OR IGNORE` or a conflict-safe upsert. Re-running
  `init_db()` must preserve current programme state and existing metadata.

Never use a client-supplied owner identifier to choose the backfill tenant.
Runtime requests must continue to derive tenant identity from the authenticated
session.

## Regression coverage

The user-model migration, tenant policy and ownership assignment are covered by
`backend/tests/test_user_model.py`, `test_accounts.py`, `test_legacy_ownership.py`,
`test_tenancy.py`, `test_manage_accounts.py` and `test_webapp_accounts.py`
(see `docs/USER_MODEL.md`).

`backend/tests/test_database.py` contains three complementary checks:

- `test_complete_legacy_schema_migration_is_idempotent_and_indexed` builds the
  complete synthetic pre-tenant schema, runs initialization twice, and verifies
  row preservation, one legacy holding, and user-first indexes.
- `test_init_db_completes_partial_user_id_backfills` exercises a partially
  applied migration and verifies that null ownership is repaired safely.
- `test_init_db_migration_idempotent` covers repeated initialization of the
  current schema.

Run the focused migration gate from the repository root:

```bash
.venv/bin/python -m pytest -q backend/tests/test_database.py
```

Before running the migration against production data, follow
`docs/BACKUP_RESTORE_RUNBOOK.md` and validate a current backup in an isolated
restore drill.

