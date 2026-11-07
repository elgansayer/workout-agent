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

`user_sessions` (the browser-session registry, see [Session security](SESSION_SECURITY.md)) follows the same rules: it is created by `ensure_user_sessions_schema`, which `init_db()` calls and `SessionStore` calls again on connect so a request that arrives before initialisation still finds the table. It has a `user_id` foreign key, the user-first index `idx_user_sessions_user`, and stores only a SHA-256 digest of each session identifier. It is additive, so rolling the code back simply leaves an unused table.

Never use a client-supplied owner identifier to choose the backfill tenant.
Runtime requests must continue to derive tenant identity from the authenticated
session.

## Account access policy schema

The account access policy (issue #753) adds two things to the schema, both
additive and idempotent, and both covered by
`backend/tests/test_database.py`:

- `users.status` (`active` | `suspended` | `deleted`, `NOT NULL` default
  `'active'`), `users.status_changed_at` and `users.session_epoch`
  (`NOT NULL` default `0`). Each column is added only when `PRAGMA table_info`
  does not already list it, so a database created before the policy keeps every
  existing account as `active` with epoch `0`, and re-running initialization
  never resets a revocation.
- `account_invitations`. This table is **operator-owned access-control data, not
  tenant data**: an invitation exists before the invited person has an account,
  so there is no owning `user_id` to point at. It therefore has no `user_id`
  column and no `user_id`-first index. It does have a single foreign key,
  `accepted_user_id -> users(id)`, which links the one account the invitation
  created, and it is indexed by `email` with a partial unique index that keeps
  at most one pending invitation per address.

See `docs/ACCOUNT_ACCESS_POLICY.md` for the behaviour these columns and tables
drive.

## Regression coverage

`backend/tests/test_database.py` contains three complementary checks:

- `test_complete_legacy_schema_migration_is_idempotent_and_indexed` builds the
  complete synthetic pre-tenant schema, runs initialization twice, and verifies
  row preservation, one legacy tenant, and user-first indexes.
- `test_init_db_completes_partial_user_id_backfills` exercises a partially
  applied migration and verifies that null ownership is repaired safely.
- `test_init_db_migration_idempotent` covers repeated initialization of the
  current schema.
- `test_account_policy_schema_is_added_idempotently_to_a_pre_existing_users_table`
  and `test_account_policy_epoch_survives_repeated_initialisation` cover the
  account access policy schema described above.

Run the focused migration gate from the repository root:

```bash
.venv/bin/python -m pytest -q backend/tests/test_database.py
```

Before running the migration against production data, follow
`docs/BACKUP_RESTORE_RUNBOOK.md` and validate a current backup in an isolated
restore drill.

