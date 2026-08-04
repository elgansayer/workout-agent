# Weekly Database Migration Review

## Objective
Ensure `database.py`'s schema migrations stay idempotent, indexed, and safe
to run against real production data as the multi-tenant migration
progresses.

## Instructions
1. Review every `ALTER TABLE`/`CREATE TABLE IF NOT EXISTS`/backfill
   statement added to `init_db()` in the last 7 days.
2. Confirm each one is idempotent: running `init_db()` twice in a row
   against the same DB file must not error or duplicate data. Write a quick
   throwaway test if `tests/test_database.py` doesn't already cover
   double-init for the tables touched this week.
3. Confirm each new `user_id`-scoped table/column has a matching composite
   index (`CREATE INDEX IF NOT EXISTS idx_<table>_user_...`), per the
   `multi-tenant-migration` skill — a scoped table without an index on
   `user_id` will silently get slow as user count grows, long before it
   becomes an obvious bug.
4. Confirm the legacy-data backfill path (assigning existing unscoped rows
   to a synthesised legacy user) still works correctly against a DB file
   that predates all of this week's migrations — simulate with a copy of an
   old schema if one is available, or reason through the ALTER sequence
   manually.
