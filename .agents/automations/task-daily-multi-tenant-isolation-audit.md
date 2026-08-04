# Daily Multi-Tenant Isolation Audit

## Objective
Track progress on the multi-tenancy migration (`AGENTS.md` §2/§7) and catch
any new code that creates a fresh unscoped-data bug.

## Instructions
1. List every table in `database.py`'s `init_db()` and note which ones still
   lack a `user_id` column. Compare against yesterday's state (check recent
   commit history for `multi-tenant-migration`-tagged work) to track
   progress — don't just repeat the same finding every day without noting
   delta.
2. For every function added or changed in `database.py` in the last 24
   hours, confirm it takes a `user_id` parameter if it touches a table that
   has one, and filters every `SELECT`/`UPDATE`/`DELETE` by it.
3. For every new route in `webapp/app.py`, confirm it passes the
   session's `user_id` through to any `database.py` call it makes, rather
   than relying on a table that happens to be unscoped (which would silently
   "work" today but is exactly the bug this audit exists to prevent from
   compounding).
4. If you find a table or function that's still unscoped and nothing else is
   actively migrating it, pick the single highest-traffic one (start with
   whatever `main.py`'s daily run touches most, e.g. `workout_history` or
   `daily_log`) and do one migration pass yourself following the
   `multi-tenant-migration` skill, rather than only auditing.
