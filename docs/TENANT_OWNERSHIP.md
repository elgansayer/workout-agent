# Tenant ownership and IDOR contract

Application-level `WHERE user_id = ?` filters are necessary but not sufficient.
A single forgotten filter, a caller that passes the wrong identifier, or a row
written with no owner silently breaks tenant isolation. Ownership is therefore
enforced at three independent layers, and an IDOR matrix proves each one against
a second, hostile tenant.

## 1. Authenticated owner context

`backend/ownership.py::AuthenticatedOwner` is the only tenant context the
repository and service layers accept.

- It cannot be built from a plain identifier. Only the factories in
  `ownership.py` mint one:
  - `owner_from_session(session)` - a complete signed session (HTTP requests).
  - `owner_for_user(db_path, user_id)` / `owner_for_email(db_path, email)` -
    background jobs and CLIs; the identifier must resolve to a stored `users`
    row, otherwise `UnknownOwnerError` is raised.
  - `owner_for_legacy_tenant(db_path)` - the stable `legacy@local` tenant used
    by single-user deployments and the deliberate anonymous mode.
- It is immutable, cannot be subclassed, cannot be copied onto another identity
  and cannot be pickled, so a verified context cannot be parked in a shared
  cache and revived for another request.
- Repositories turn it into a SQL parameter only through `owner_id(owner)`,
  which re-checks the runtime type. A raw string, `None`, a mock or any
  look-alike object raises `InvalidOwnerContextError` even when static typing was
  bypassed.

Every repository function in `backend/database.py` takes
`owner: AuthenticatedOwner` (keyword-only or first positional, never optional).
The only ownerless functions are `init_db`, `get_or_create_user`,
`get_all_users` (tenant enumeration for the job dispatcher), `get_legacy_user_id`
and the empty `get_programme_templates` shim.

`backend/tests/test_tenant_boundaries.py` keeps this from eroding. It parses the
production source and fails when a function accepts a bare tenant identifier, SQL
runs outside the reviewed modules, an owner context is forged outside
`ownership.py`, a provider is resolved without an owner, a module keeps a
process-wide cache, a tenant route stops deriving its owner from the session, or a
route declares a client-settable owner parameter. Every exception is listed with
its reason, so adding one is a reviewed decision.

## 2. Database-level constraints

`ownership.USER_SCOPED_TABLES` is the single source of truth for every table
that stores user-owned records. For each registered table the database itself
guarantees:

| Guarantee | Mechanism |
| --- | --- |
| A non-null owner | `user_id ... NOT NULL` |
| A real owner | foreign key to `users(id)`; `PRAGMA foreign_keys = ON` is set on every repository connection |
| Deletion cannot erase a tenant by accident | `ON DELETE NO ACTION` (records must be removed by a deliberate workflow); ephemeral `csrf_tokens` use `ON DELETE CASCADE` |
| An indexed access path | an index whose first column is `user_id` (a primary key or unique constraint counts) |
| An immutable owner | `trg_<table>_user_id_immutable` aborts any `UPDATE` that changes `user_id`, including through an upsert |

`init_db()` brings an existing database up to this contract and then audits the
whole schema with `enforce_ownership_constraints`. A table that has drifted and
cannot be repaired stops startup with `OwnershipConstraintError` instead of
serving data without the guarantee.

Upgrade behaviour for databases created before the contract:

- Tables whose owner column was nullable or unconstrained are rebuilt in the same
  transaction (`database._rebuild_owned_table`), preserving every row and the
  original indexes.
- Rows without an owner are assigned to the stable `legacy@local` tenant, the same
  backfill the earlier tenant migrations use. A client-supplied identifier is
  never used.
- The rebuild fails closed and leaves the database untouched when a row belongs to
  a user that does not exist, or when the table has a column the canonical schema
  does not know. Restore the missing user or remove the orphaned rows, then
  restart.
- Orphaned single-use CSRF nonces are purged rather than copied; the browser simply
  requests a fresh token.
- Dropped indexes and triggers are recreated on every start, so `init_db()` stays
  idempotent.

New user-owned tables must be added to `USER_SCOPED_TABLES`; the test suite
(`test_a_table_with_an_owner_column_must_be_registered`) fails when a table with a
`user_id` column is missing from the registry.

### Staged health platform exception

The canonical health platform (`health_connections`, `health_metrics`,
`health_sync_runs`) still uses an integer tenant identity that is not yet unified
with the text `users.id`, so the foreign key cannot be declared yet. The registry
records this exception explicitly (`require_foreign_key=False`) and
`test_registered_health_tables_are_the_only_ones_without_an_owner_foreign_key`
confines it to those three tables. The remaining guarantees are enforced now: a
non-null owner, `CHECK (user_id > 0)`, owner-leading indexes and the immutable-owner
trigger. Its writers validate an authenticated identity
(`authenticated_user_id`) instead of trusting the record. The foreign key is added
by the health-platform identity migration, which must flip
`require_foreign_key` back to `True` for these tables.

## 3. Request boundary

- `webapp.app._check_api_auth` derives the owner from the signed session and
  re-verifies it against `users`. A validly signed cookie for an account that no
  longer exists (for example after a database restore) receives `401`, never an
  empty tenant or a foreign key failure.
- Anonymous access resolves to the legacy tenant only when Google sign-in is not
  configured. Mutations still require a signed identity.
- `MutationSecurityMiddleware` rejects a mutation whose body, query string or
  `X-User-Id`/`X-Owner-Id`/`X-Account-Id` header names a different owner than the
  session with `404`.
- Authenticated responses are `Cache-Control: private, no-store` with
  `Vary: Cookie`, so no shared cache can serve one tenant's response to another.

## 4. IDOR matrix

| Operation | Service layer (`test_idor_matrix.py`) | HTTP, real sessions (`test_idor_http_matrix.py`) |
| --- | --- | --- |
| Read | `test_idor_read_isolates_every_listing_surface` | `test_http_read_never_returns_another_tenants_records` (every tenant GET route), `test_http_read_ignores_client_supplied_owner_claims` |
| Write | `test_idor_write_cannot_upsert_over_another_tenant`, `test_idor_active_programme_is_per_tenant` | `test_http_writes_only_touch_the_session_owner`, `test_http_client_supplied_owner_claims_cannot_redirect_a_write` |
| Delete | `test_idor_delete_refuses_rows_owned_by_another_tenant`, `test_idor_delete_cannot_remove_another_tenants_api_key` | `test_http_bulk_deletes_only_clear_the_session_owner`, `test_http_api_key_delete_cannot_remove_another_tenants_key`, `test_http_history_rebuild_never_touches_another_tenants_history` |
| Export | `test_idor_export_refuses_another_tenants_provider` | not reachable over HTTP |
| Background jobs | `test_idor_background_job_cannot_mint_an_arbitrary_owner`, `test_active_scheduler_runs_coaching_for_each_user`, `test_active_scheduler_scopes_each_insight_job_to_user` (`test_scheduler.py`) | not reachable over HTTP |
| Caches | `test_idor_cached_owner_cannot_be_revived_for_another_tenant`; `test_no_process_wide_cache_can_hold_tenant_data_unreviewed` | `test_http_responses_are_never_shareable_between_tenants` |
| Indirect object references | `test_idor_indirect_reference_does_not_cross_tenants` | `test_http_notification_ids_are_not_global_handles`, `test_http_reasoning_context_ids_are_owner_scoped` |
| Forged or stale identity | `test_owner_from_session_fails_closed_without_a_complete_identity` | `test_http_requests_without_a_session_are_rejected`, `test_http_forged_and_stale_sessions_cannot_mutate`, `test_http_stale_session_is_unauthorised_not_an_empty_tenant` |

Database-level enforcement is exercised in `backend/tests/test_ownership.py`:
the audit rejects each kind of drift, the trigger blocks reassignment (including
through an upsert), legacy databases are upgraded without losing or reassigning
rows, and a fresh database satisfies the whole contract.

Run the focused ownership gate from the repository root:

```bash
.venv/bin/python -m pytest -q \
  backend/tests/test_ownership.py \
  backend/tests/test_idor_matrix.py \
  backend/tests/test_idor_http_matrix.py \
  backend/tests/test_tenant_boundaries.py \
  backend/tests/test_database.py
```

## 5. Adding user-owned data

1. Add the table to `database.init_db` with `user_id TEXT NOT NULL REFERENCES
   users(id)` and an index (or primary key) that starts with `user_id`.
2. Register it in `ownership.USER_SCOPED_TABLES`; the startup audit then installs the
   immutable-owner trigger and verifies the rest.
3. Write repository functions that take `owner: AuthenticatedOwner` and bind
   `owner_id(owner)`; never accept a user identifier from a request.
4. Extend the IDOR matrix with a read, write and delete case for the new data.

## 6. Operations and rollback

Take and validate a backup (`docs/BACKUP_RESTORE_RUNBOOK.md`) before the first start
of a release containing this contract, because legacy tables are rebuilt in place.
The rebuilt tables keep their column names and order, so the previous release
continues to read and write them. The previous release always supplied an owner
and never reassigned one, and it does not enable foreign key enforcement, so the
new constraints only reject statements that would have broken tenant isolation.
To roll back, redeploy the previous image. If a downgrade must also remove the
triggers, drop each `trg_<table>_user_id_immutable` trigger manually; no data
migration is needed.
