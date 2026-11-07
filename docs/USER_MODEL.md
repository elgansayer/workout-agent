# Production user model

Workout Agent is multi-user. Every record belongs to exactly one **account**, every
process acts for an explicit account, and production has **no shared fallback
user**. This document is the contract for the account schema, sign-in resolution,
tenant resolution and the one-time assignment of data written before accounts
existed.

All examples use reserved example domains and synthetic identifiers.

## Accounts

An account is a row in `users`:

| Column | Meaning |
| --- | --- |
| `id` | Random UUID. **Immutable** (`trg_users_id_immutable`). Every owned row references it. |
| `email` | Mutable attribute, never a key. Unique case-insensitively (`idx_users_email_lower`). |
| `created_at` | **Immutable** (`trg_users_created_at_immutable`). |
| `status` | Lifecycle state, constrained to the values below. |
| `status_changed_at`, `updated_at` | Timestamps maintained on every status change / login. |
| `last_login_at` | Set by a successful verified sign-in. |

### Status lifecycle

| Status | Authenticates | Scheduled | Notes |
| --- | --- | --- | --- |
| `active` | yes | yes | The default for a new account. |
| `suspended` | no | no | Operator-controlled; reversible. Takes effect on the very next request. |
| `unclaimed` | no | no | The `legacy@local` placeholder while it holds pre-account data (production). |
| `retired` | no | no | The placeholder after its data was assigned. **Terminal.** |
| `deleted` | no | no | Reserved for the account-deletion workflow. **Terminal.** |

Operators can only move an account between `active` and `suspended`
(`accounts.set_account_status`, `manage_accounts.py set-status`). `retired` and
`deleted` can never change again (`trg_users_status_terminal`). The vocabulary is
defined once in `database.ACCOUNT_STATUSES`; `accounts.AccountStatus` and the
`CHECK` constraint are asserted to agree by `test_user_model.py`.

### Canonical identities

`user_identities` binds an external identity to exactly one account:

- `UNIQUE (provider, subject)`: one external identity maps to one account.
- `UNIQUE (user_id, provider)`: an account has at most one identity per provider.
- `provider`, `subject`, `user_id` and `created_at` are **immutable**
  (`trg_user_identities_immutable`), so an identity can never be re-pointed at
  another account.
- `user_id` must reference an existing account (`trg_user_identities_user_exists`)
  and the placeholder can never hold an identity
  (`trg_user_identities_not_for_holding`).

The provider's stable `subject` selects the account. An email address is only used
to adopt a pre-identity account **once**, and only when the provider verified it.

### Sign-in resolution

`accounts.resolve_oauth_user` runs in one `BEGIN IMMEDIATE` transaction:

1. `(provider, subject)` already bound: that account, if it is `active`.
2. Otherwise an `active` account holding the verified email and no identity for
   this provider adopts the identity (the upgrade path for accounts created
   before identities existed).
3. Otherwise a new `active` account and identity are created.

It refuses (and creates nothing) when the subject or email is missing, the email
is not provider-verified, the email is the reserved placeholder address, the email
matches more than one account, the account is not `active`, or the email belongs
to an account already bound to a *different* subject for the provider (the address
may have been reassigned at the provider). Refusal messages never contain the email
or the subject, so they are safe to log.

The `/auth` callback maps these refusals to a generic `403`. Sign-in remains gated
first by the optional `ALLOWED_EMAILS` allow-list.

### Authenticated requests

A signed session is not enough. `webapp.app._check_api_auth` resolves the session's
`user_id` through `accounts.require_active_user` on **every** request, so
suspending or removing an account revokes access immediately. The `user_id` used
for every read and write comes from that check, never from a request parameter.

## Tenant resolution

`backend/tenancy.py` is the single definition of when an operation may omit the
user:

- Production, an unset `APP_ENV` and any unrecognised `APP_ENV` **never** allow it.
- The explicit **local single-user mode** needs both a non-production `APP_ENV`
  (`dev`, `development`, `local`, `test`, `testing`) and a truthy
  `ALLOW_ANONYMOUS_WEB` (`1`, `true`, `yes`, `on`). Only then does an omitted
  `user_id` resolve to the local tenant.

`database.require_tenant` applies the rule to every user-scoped persistence
function: an explicit non-empty `user_id` is always used as given; an omitted one
either resolves to the local tenant in that mode or raises
`tenancy.TenantRequiredError`. It never widens a query to all users. The agent,
scheduler, insight job and `sync_history` follow the same rule:

- the scheduler and insight job iterate **active** accounts only (`get_all_users`);
- `main.py` needs `--user-id` or `SCHEDULER_USER_ID` (outside local mode) and
  verifies the selected account is `active`;
- deployment-wide connector credentials (`HEVY_API_KEY`, the Google Health
  environment) belong to the **deployment owner** only: the verified person the
  legacy data was assigned to, or the local tenant in local mode. Every other
  account uses its own encrypted credentials.

### Persistence backstop

SQLite does not enforce `REFERENCES users(id)` unless `PRAGMA foreign_keys` is on,
and never rejects `NULL`. `init_db` therefore installs `trg_<table>_owner_required_*`
triggers on every table in `database.USER_OWNED_TABLES`: an `INSERT`, or an
`UPDATE` of `user_id`, that would leave a row without an owner is aborted whichever
code path issues it. That a specific owner is the *authenticated* user is decided at
the boundary above; the triggers guarantee "every record has an owner".

## Upgrading a single-user deployment

Schema changes are additive and idempotent (`database._migrate_user_model`): new
columns are added behind `PRAGMA table_info` guards, existing accounts keep their
`created_at` as their honest status/update timestamps, and tables, indexes and
triggers use `IF NOT EXISTS`.

Rows written before accounts existed have no owner. On the first start of the new
release:

- In **production** a single placeholder account (`legacy@local`, display name
  `Legacy Data`) is created **only if such rows exist**, in `unclaimed` status. It
  holds the data inertly: it cannot sign in, cannot be scheduled and cannot be
  managed by `set-status`. Until the data is assigned, startup logs a warning that
  names the remedy (no identifiers in the message). A placeholder left `active` by
  an older release is demoted to `unclaimed`.
- A brand-new production database has **no accounts and no seed rows**.
- In explicit **local mode** the placeholder is the active local tenant and the
  seed rows exist, as before.

### Assigning the legacy data to a verified owner

Assignment is a two-step, operator-driven process in `backend/legacy_ownership.py`,
run with `backend/scripts/manage_accounts.py` inside the application container
(`APP_ENV` and `DATABASE_PATH` must match production).

**Prerequisites**

1. Take and verify a backup (`docs/BACKUP_RESTORE_RUNBOOK.md`), ideally with a
   restore drill. Run the assignment in a quiet window.
2. The intended owner has **signed in once** since accounts were introduced, so
   their account has a provider-verified identity.

**1. Plan (read-only)**

```bash
python scripts/manage_accounts.py list
python scripts/manage_accounts.py plan-legacy-assignment --owner-email owner@example.com
# add --json for a machine-readable plan
```

The plan lists, per table, how many rows would move and how many would be left
behind, plus a **digest** of exactly what it found. It exits non-zero and prints no
digest while any blocker remains:

| Blocker | Meaning |
| --- | --- |
| `owner_not_found` | No account has that email; the owner must sign in once first. |
| `owner_not_active` | The owner's account is not `active`. |
| `owner_has_no_verified_identity` | The owner has not signed in through a verified identity. |
| `owner_is_legacy_placeholder` | The placeholder cannot own its own data. |
| `already_assigned_to_different_owner` | The data was already assigned to someone else. |
| `unmapped_user_table:<name>` | A live table holds `user_id` rows the assignment does not cover; a code change is required before assigning (nothing is stranded silently). |

**2. Apply**

```bash
python scripts/manage_accounts.py assign-legacy-data \
  --owner-email owner@example.com \
  --confirm-plan <digest from the plan> \
  --backup-verified \
  --operator <your name>
```

`--backup-verified` is the operator's attestation that a verified backup exists;
`--operator` defaults to the OS user and is recorded in the audit trail. The
command re-derives the plan inside one `BEGIN IMMEDIATE` transaction and refuses
(changing nothing) if the attestation or digest is missing, the digest no longer
matches (stale or mistyped), or the owner is ineligible. Otherwise it moves every
row, retires the placeholder and appends an audit record
(`legacy_ownership_migrations`, append-only: plan digest, from/to account, owner
email, operator, timestamp and per-table counts, never row identifiers). A failure
part-way rolls everything back.

Nothing is deleted or overwritten. Collisions are resolved in the owner's favour
and the losing legacy rows stay on the retired placeholder, reported in the plan:

| Tables | Rule |
| --- | --- |
| `workout_history`, `exercise_progress`, `body_metrics`, `daily_log`, `check_ins`, `chat_messages`, `notifications` | Append-only history: all legacy rows move. |
| `programme_state`, `dashboard_insights`, `deep_correlations`, `user_preferences` | One row per tenant: moves only if the owner has none. |
| `hevy_meta`, `hevy_routines`, `reasoning_logs`, `user_api_keys`, `push_subscriptions` | Moves unless the owner already has a row with the same key (`key`, `routine_key`, `context_id`, `provider`, `endpoint`). |
| `programmes` | Moves unless the owner has the same `template_key`, or the legacy row is active and the owner already has an active programme. |

`user_identities` and `csrf_tokens` carry `user_id` but are deliberately not
transferred (identities belong to the person who signed in; CSRF tokens expire).

Re-running the command is a no-op, and the data can never be redirected to a
different owner afterwards.

**3. Verify**

```bash
python scripts/manage_accounts.py list   # placeholder is `retired`, owner is `active`
python scripts/manage_accounts.py plan-legacy-assignment --owner-email owner@example.com
```

The second command reports the data as already assigned with nothing left to move.
Then sign in as the owner and confirm the dashboard shows the historical data.

**Rollback**

The assignment is the only non-additive step. Restore the pre-assignment backup
(`docs/BACKUP_RESTORE_RUNBOOK.md`); data written by users after the assignment is
not in that backup. The audit record intentionally stores no row identifiers.

## Operator reference

```bash
python scripts/manage_accounts.py [--database PATH] list
python scripts/manage_accounts.py [--database PATH] set-status --email EMAIL --status active|suspended
python scripts/manage_accounts.py [--database PATH] plan-legacy-assignment --owner-email EMAIL [--json]
python scripts/manage_accounts.py [--database PATH] assign-legacy-data --owner-email EMAIL \
  --confirm-plan DIGEST --backup-verified [--operator NAME]
```

`--database` defaults to `$DATABASE_PATH`. Every command first runs `init_db`, so
the schema is current. Refusals exit with status 1 and a message on stderr.
`list` prints emails for the operator; identity subjects are never printed.

## Verification

```bash
.venv/bin/python -m pytest -q \
  backend/tests/test_user_model.py backend/tests/test_accounts.py \
  backend/tests/test_legacy_ownership.py backend/tests/test_tenancy.py \
  backend/tests/test_manage_accounts.py backend/tests/test_webapp_accounts.py \
  backend/tests/test_database.py
```

| Test file | Covers |
| --- | --- |
| `test_user_model.py` | Schema contract: immutable ids/timestamps, status vocabulary and terminal states, identity constraints, case-insensitive email uniqueness, upgrade of pre-account databases, owner guards, registry drift, production vs local seeding. |
| `test_accounts.py` | Sign-in resolution, adoption, takeover refusals, concurrency, inactive accounts, operator status changes. |
| `test_legacy_ownership.py` | Plan/apply, digest and backup attestation, blockers, rollback on failure, conflict rules, audit trail, deployment-owner credentials. |
| `test_tenancy.py` | The production/local tenant policy. |
| `test_manage_accounts.py` | The operator CLI end to end, including refusals and exit codes. |
| `test_webapp_accounts.py` | `/auth` binding, per-request account checks, no anonymous fallback in production. |

## Boundaries of this change

This delivers the account schema, identity binding, tenant resolution and the
verified legacy-data assignment. It does not add registration policy, session
rotation/expiry, roles, invitations, recovery flows or account deletion; those
build on the `active`/`suspended`/`deleted` lifecycle and `user_identities` defined
here. The diagnostics endpoint validates the session shape but, unlike
`_check_api_auth`, does not yet re-check account status.
