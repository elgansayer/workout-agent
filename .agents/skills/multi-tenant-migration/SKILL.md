---
name: multi-tenant-migration
description: 'Add user_id scoping to tables and queries (PostgreSQL / SQLite via SQLAlchemy 2.0 or Alembic) to guarantee strict multi-tenant data isolation per user.'
---

# Multi-Tenant Migration & Isolation

## Why This Exists

Every domain table in the workout agent must be strictly isolated per user (`user_id`). This ensures that multi-user authentication, cloud deployment, and background ingestion tasks operate safely without data leakage across accounts.

## Core Mandates

1. **Foreign Key Scoping:**
   - Every domain table (`workout_history`, `exercise_progress`, `body_metrics`, `daily_log`, `programme_state`, `hevy_routines`, `hevy_meta`, `user_preferences`, `user_api_keys`) must have a `user_id` column referencing `users(id)`.

2. **Alembic & Async Migrations:**
   - Use declarative Alembic migrations or idempotent schema initialization.
   - For PostgreSQL deployments, leverage Row-Level Security (RLS) and composite indexing `(user_id, date)`.

3. **Query Filtering:**
   - All ORM queries and SQL statements must explicitly filter on `user_id == current_user_id`.

4. **Owner Context:**
   - Repository and service functions take `owner: AuthenticatedOwner` (`backend/ownership.py`) and bind it with `owner_id(owner)`. Never accept a bare user identifier; mint contexts only through `owner_from_session`, `owner_for_user`, `owner_for_email` or `owner_for_legacy_tenant`.

5. **Database Constraints:**
   - Register every new user-owned table in `ownership.USER_SCOPED_TABLES`. It must have `user_id TEXT NOT NULL REFERENCES users(id)`, an index that starts with `user_id`, and the immutable-owner trigger that `init_db()` installs and audits. See `docs/TENANT_OWNERSHIP.md`.

6. **Testing:**
   - Verify in `backend/tests/test_database.py` that two separate users querying the same tables receive only their own records.
   - Extend `backend/tests/test_idor_matrix.py` and `backend/tests/test_idor_http_matrix.py` with a read, write and delete case for the new data.

