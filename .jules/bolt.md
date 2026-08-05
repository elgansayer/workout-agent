## 2024-05-14 - SQLite Temporary B-Tree Sort Overhead
**Learning:** When queries in `database.py` group and order by `exercise_name` or `date` on the `exercise_progress` table without covering indexes, SQLite resorts to creating temporary B-Trees (`USE TEMP B-TREE FOR ORDER BY` / `GROUP BY`). This incurs a measurable overhead, especially as the size of the progress history grows.
**Action:** Always verify query plans for commonly accessed tables and add precise, matching compound indexes in `init_db()` (e.g., `(exercise_name, id ASC)`) to eliminate implicit temporary sorting during reads.

## 2024-XX-XX - [O(n) memory to O(1) in get_progress_history]
**Learning:** The database query in `get_progress_history` was pulling the entire history of workouts into memory to extract only the last `limit_per_exercise` records. Over time, this unbounded growth would cause performance bottlenecks.
**Action:** Added an SQLite window function (`ROW_NUMBER() OVER PARTITION BY`) to limit results at the database level. Always look for "fetch everything then slice in Python" patterns as the database grows.

## 2024-05-14 - SQLite 1RM Calculation Offloading
**Learning:** `get_personal_records()` previously loaded the entire workout history into Python memory to find the highest Epley 1RM per exercise, operating in O(N) memory scale. SQLite natively supports retrieving correctly associated bare columns with aggregate functions like `MAX(weight * (1 + reps/30))` combined with `GROUP BY`.
**Action:** Shift row-level aggregation logic directly to SQLite queries (e.g., max calculations over history) to ensure unbounded memory growth does not break application logic, fetching O(1) records per exercise instead of O(N).

## 2024-05-15 - Compound Indexes for Multi-Tenant Queries
**Learning:** Adding single-column `user_id` indexes during multi-tenancy migrations solves basic filtering, but forces SQLite to use slow temporary B-Trees for queries with `ORDER BY` or `GROUP BY` (e.g. `ORDER BY date DESC, id DESC`). This affects `body_metrics` and `exercise_progress` tables.
**Action:** When migrating single-user tables to multi-tenant, single-column `user_id` indexes are insufficient if the queries have ordering/grouping. Always create compound indexes like `(user_id, date DESC, id DESC)` or `(user_id, exercise_name, id DESC)` to fully support the query shape and prevent temporary sorts.
