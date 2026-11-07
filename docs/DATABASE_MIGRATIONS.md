# Database migration safety

`backend/database.py::init_db()` is the compatibility migration path for the
SQLite deployment. It must remain safe for a brand-new database, a database
created before tenant scoping, and a database where an earlier startup stopped
part-way through schema initialization.

## Invariants

- All schema changes run inside the transaction managed by `_connect()`.
- An `ALTER TABLE ... ADD COLUMN` is guarded by `PRAGMA table_info`.
- Existing unscoped rows are assigned to the single stable
  `legacy@local` tenant. Backfills update only rows whose `user_id` is null,
  so retrying initialization does not duplicate or reassign data.
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

`backend/tests/test_database.py` contains three complementary checks:

- `test_complete_legacy_schema_migration_is_idempotent_and_indexed` builds the
  complete synthetic pre-tenant schema, runs initialization twice, and verifies
  row preservation, one legacy tenant, and user-first indexes.
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

## One-time legacy adoption

`init_db()` keeps pre-tenant rows in the `legacy@local` tenant; it never
chooses a production owner. Moving that data into the first production account
is a separate, explicit operator step:
`tools/legacy_data_migration.py`, documented in
`docs/LEGACY_DATA_MIGRATION.md`. It requires a target account id, refuses
ambiguous ownership, writes a verified backup under the write lock before the
first change, re-owns rows in one transaction with before/after counts, and is
idempotent. `tools/test_legacy_data_migration.py` and
`backend/tests/test_legacy_data_adoption_schema.py` fail when a new tenant table
is not classified by the tool (`USER_OWNED_TABLES` or `EPHEMERAL_TABLES`), and
`backend/tests/test_database.py` runs the adoption on a database upgraded from
the complete pre-tenant schema.

