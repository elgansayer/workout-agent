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

4. **Testing:**
   - Verify in `backend/tests/test_database.py` that two separate users querying the same tables receive only their own records.

