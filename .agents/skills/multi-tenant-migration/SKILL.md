---
name: multi-tenant-migration
description: 'Add user_id scoping to a domain table in database.py (and every function that reads/writes it) so it isolates data per logged-in user instead of being shared globally. Use whenever touching workout_history, programme_state, exercise_progress, body_metrics, daily_log, check_ins, chat_messages, dashboard_insights, deep_correlations, or any new table that stores per-user data.'
---

# Multi-Tenant Migration

## Why This Exists

Per `AGENTS.md` §2/§7, every domain table in `database.py` predates the
`users` table and has no `user_id` column — one shared programme, history,
weight log, and chat for every Google account that logs in. This skill is
the standard procedure for fixing one table (and its call sites) at a time
without breaking existing single-tenant deployments mid-migration.

## When to Use

- You're adding a `user_id` column to an existing table.
- You're adding a brand-new table that stores anything per-user (it must be
  scoped from creation — don't create a new unscoped table, ever).
- You're modifying a function in `database.py`, `main.py`, or
  `webapp/app.py` that reads/writes a table still missing `user_id`.

## Procedure

1. **Schema change** — add the column inside `init_db()` using the existing
   idempotent-migration pattern (see the `hrv` column migration in
   `database.py` for the template):

   ```python
   cursor.execute("PRAGMA table_info(workout_history)")
   columns = {row[1] for row in cursor.fetchall()}
   if "user_id" not in columns:
       cursor.execute(
           "ALTER TABLE workout_history ADD COLUMN user_id TEXT REFERENCES users(id)"
       )
   ```

   Leave it nullable at the ALTER step (SQLite can't add a `NOT NULL` column
   without a default to an existing table with rows) — see step 2 for
   backfill, then enforce `NOT NULL` only in new-row insert paths going
   forward, not via a schema constraint on old rows.

2. **Backfill existing rows** — every pre-migration deployment has exactly
   one implicit tenant. Synthesise a stable "legacy" user (e.g.
   `get_or_create_user(email="legacy@local", display_name="Legacy Data")`
   once at migration time) and backfill: `UPDATE workout_history SET user_id
   = ? WHERE user_id IS NULL`. Do this inside the same `init_db()` migration
   block so it's idempotent and runs automatically on upgrade, not as a
   separate manual script.

3. **Thread `user_id` through every function touching the table.** Change
   the function signature, not just the SQL — e.g.
   `save_workout(date, payload)` → `save_workout(user_id, date, payload)`,
   and every `WHERE` clause gets `AND user_id = ?`. Do this for *every*
   caller in the same change; a function with some scoped call sites and
   some not is worse than leaving it unscoped, because it hides the bug
   instead of making it loud.

4. **Update callers** — `main.py` (the scheduled agent run) needs to resolve
   which user it's running for (today it's implicitly the one legacy/admin
   user; once multiple users have connected their own Hevy/Telegram, this
   becomes a loop over `users` — that's a separate `scheduler-job` task, not
   part of a single table's migration). `webapp/app.py` route handlers
   already have `user_id` available from the session (see the auth
   middleware) — pass it through, don't reach for a global.

5. **Composite indexes** — add `CREATE INDEX IF NOT EXISTS idx_<table>_user_date
   ON <table>(user_id, date)` (or the relevant ordering column) alongside the
   migration; every scoped table is queried by `user_id` first.

6. **Tests** — add/extend `tests/test_database.py` covering: two different
   `user_id`s writing to the same table don't see each other's rows, and the
   legacy-backfill path produces a row scoped to the synthesised legacy user.

## Verification

Run the `verification-gate` skill's steps. Additionally, manually check: does
`init_db()` still succeed when run twice in a row against the same DB file
(idempotency), and does it succeed against a *pre-migration* DB file with
existing unscoped rows (backfill path)? Both are easy to break silently.

## Gotchas

- `programme_state` and `dashboard_insights`/`deep_correlations` are
  currently singleton rows (`id=1`) — migrating these means dropping the
  singleton assumption entirely (primary key becomes `user_id`, not
  `id=1`), not just adding a column. Check every `WHERE id = 1` in
  `database.py` when you touch these.
- Don't migrate every table in one giant change. Pick one table (and its
  full call-site chain) per task — this matches how `task_add()`/the swarm
  queue is meant to be used, and makes a bad migration easy to isolate and
  revert.
