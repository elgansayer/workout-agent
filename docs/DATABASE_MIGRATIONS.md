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
- Runtime connections open with `PRAGMA foreign_keys` off, so a declared
  `REFERENCES users(id)` is documentation, not protection. `init_db()` therefore
  also installs (idempotently, last, after the final table rebuild) triggers that
  enforce the same guarantees unconditionally for every user-scoped table - the
  registry in `database.USER_OWNED_TABLES` plus `database.ACCOUNT_BOUND_TABLES`
  (`user_sessions`, `credential_audit_events`, `record_provenance`):
  - `trg_<table>_owner_exists_{insert,update}` - the owner must be a row of
    `users`. A write for a closed or never-created account fails instead of
    leaving data nobody can reach or erase.
  - `trg_<table>_user_immutable` - once a row has an owner it cannot be given to
    another account (taking ownership of an unowned `NULL`/empty row, as the
    pre-account backfill does, is still allowed). The audited legacy adoption
    flows (`legacy_ownership.apply_legacy_ownership` and
    `tools/legacy_data_migration.py`) lift this guard together with the
    revision `*_no_update` guards for their own transaction and recreate it
    before commit. The append-only `credential_audit_events` and
    `record_provenance` tables get only the insert-time existence guard because
    their own trigger already aborts every UPDATE.
  - `trg_<child>_<column>_same_owner_{insert,update}` - a reference from one
    owner-scoped row to another (a revision's programme, a routine's revision,
    a match's programme, ...) must point at a row with the **same** `user_id`.
    The reference list is read from the live foreign keys by
    `database.owned_references()` plus the few undeclared integer references in
    `database._UNDECLARED_OWNED_REFERENCES`, so a new relationship between owned
    tables is covered without editing a list. A nullable reference may stay
    unset. The guard fires on INSERT and when the reference column changes; a
    legacy adoption that only changes `user_id` does not trigger it.
  The deletion-ledger tables (`account_deletion_requests`, `account_tombstones`)
  and `security_audit_events` are deliberately outside this set: they must
  outlive, or describe attempts without, an account. `user_identities` has its own
  equivalent triggers.
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

- `record_provenance` (the provenance and lineage ledger) is created by `init_db()`
  with `CREATE ... IF NOT EXISTS` for the table, its index and an append-only
  trigger. It needs no backfill: records stored before it existed have no
  revision, read as "source not recorded", and capture a legacy baseline the
  first time they are replaced or corrected. Its unique index leads with
  `user_id`. See `docs/PROVENANCE.md`.

`user_sessions` (the browser-session registry, see [Session security](SESSION_SECURITY.md)) follows the same rules: it is created by `ensure_user_sessions_schema`, which `init_db()` calls and `SessionStore` calls again on connect so a request that arrives before initialisation still finds the table. It has a `user_id` foreign key, the user-first index `idx_user_sessions_user`, and stores only a SHA-256 digest of each session identifier. It is additive, so rolling the code back simply leaves an unused table.

Per-user settings add columns without a new table: `user_settings.ensure_settings_schema`
(called from `init_db` right after `users` is created) adds `users.locale`,
`users.motion`, `users.contrast`, `users.text_size` and
`exercise_progress.performed_at`, each guarded by `PRAGMA table_info`. When the
`users` columns first appear on an existing database, accounts still holding the
untouched `UTC` default inherit the deployment `TZ` once, so the upgrade keeps
each daily run where it was. See `docs/USER_SETTINGS.md`.

Never use a client-supplied owner identifier to choose the backfill tenant.
Runtime requests must continue to derive tenant identity from the authenticated
session.

## Credential vault schema and migration

`user_api_keys` keeps its original columns and gains additive lifecycle columns
(`status`, `key_hint`, `key_version`, `rotated_at`, `revoked_at`,
`last_tested_at`, `last_test_status`). `credential_audit_events` is append-only
and tenant scoped. A trigger rejects any attempt to change a credential's
`user_id`.

`secret_vault.migrate_legacy_credentials()` runs after `init_db()` at web and
agent startup. It re-seals pre-vault plaintext and Fernet rows under per-user
AES-256-GCM and moves legacy `hevy_meta.google_health_refresh_token` values into
the vault before blanking the legacy value. When `ENCRYPTION_KEY` is missing or
invalid, migration changes nothing and reports the pending count; it never
writes plaintext. The operation is idempotent and compare-and-swap protected so
concurrent web and agent startups cannot corrupt a row.

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

## Push-subscription ownership

A web-push endpoint identifies one browser profile, so it belongs to one account at a time; otherwise a notification for the account that left a shared device would still be delivered to whoever signs in there next. `init_db()` removes any repeated endpoint, keeping the newest registration (the account that most recently took the device), and then creates the unique index `idx_push_subscriptions_endpoint`. Both steps run only while the index is absent, so the migration is idempotent and later starts do not touch the table. `save_push_subscription` hands the device to the registering account by deleting every registration of that endpoint in the same transaction before inserting its own.

Rollback: the index is additive. Dropping it restores the previous behaviour, but the duplicate rows removed on upgrade are not recoverable; each was a registration for a browser another account had since taken. `backend/tests/test_push_subscription_ownership.py` covers the hand-over, the database constraint and the upgrade of a database that already holds repeated endpoints.

## Regression coverage

The user-model migration, tenant policy and ownership assignment are covered by
`backend/tests/test_user_model.py`, `test_accounts.py`, `test_legacy_ownership.py`,
`test_tenancy.py`, `test_manage_accounts.py` and `test_webapp_accounts.py`
(see `docs/USER_MODEL.md`).

`backend/tests/test_owner_integrity.py` proves, for every table in the two
registries and with the connection opened exactly as the runtime opens it, that
each has a non-null owner, a user-first index and the three integrity guards
above, that they hold on every write path, and that a new `user_id` table must be
classified. `backend/tests/owned_rows.py` builds valid parent/child rows for it.

`backend/tests/test_database.py` contains three complementary checks:

- `test_complete_legacy_schema_migration_is_idempotent_and_indexed` builds the
  complete synthetic pre-tenant schema, runs initialization twice, and verifies
  row preservation, one legacy holding, and user-first indexes.
- `test_init_db_completes_partial_user_id_backfills` exercises a partially
  applied migration and verifies that null ownership is repaired safely.
- `test_init_db_migration_idempotent` covers repeated initialization of the
  current schema.
- `test_account_policy_schema_is_added_idempotently_to_a_pre_existing_users_table`
  and `test_account_policy_epoch_survives_repeated_initialisation` cover the
  account access policy schema described above.

`backend/tests/test_programme_domain.py` additionally covers the additive
Hevy-first programme domain migration, legacy static archive preservation,
partial domain-schema replay, and immutable revision triggers.

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

