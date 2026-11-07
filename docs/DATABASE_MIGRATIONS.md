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
- Every user-owned table has a non-null `user_id` with a foreign key to
  `users(id)`, an index whose first column is `user_id`, and a trigger that
  rejects any update changing `user_id`. A composite primary key or unique
  constraint satisfies the index requirement when it matches the query shape;
  frequently queried history tables also use explicit `idx_<table>_user_...`
  indexes. `ownership.USER_SCOPED_TABLES` is the registry, and `init_db()` audits
  it at the end of every start so drift fails closed. See
  `docs/TENANT_OWNERSHIP.md` for the full contract.
- Foreign keys are enforced on every repository connection
  (`PRAGMA foreign_keys = ON` in `_connect()`).
- Tables created before the contract whose owner column was nullable or
  unconstrained are rebuilt in the same transaction. Rows without an owner move to
  `legacy@local`; a row owned by a missing user, or an unknown column, aborts the
  rebuild and leaves the database untouched.
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

`backend/tests/test_ownership.py` covers the ownership contract itself:
`test_fresh_database_satisfies_the_whole_contract`,
`test_upgrade_enforces_the_contract_without_losing_or_reassigning_rows`,
`test_upgrade_refuses_to_reassign_rows_owned_by_a_missing_user`,
`test_upgrade_refuses_to_drop_columns_the_contract_does_not_know`,
`test_restart_repairs_dropped_indexes_and_triggers` and
`test_startup_fails_closed_when_drift_cannot_be_repaired`.

Run the focused migration gate from the repository root:

```bash
.venv/bin/python -m pytest -q backend/tests/test_database.py backend/tests/test_ownership.py
```

Before running the migration against production data, follow
`docs/BACKUP_RESTORE_RUNBOOK.md` and validate a current backup in an isolated
restore drill.

