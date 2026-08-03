## 2024-05-14 - SQLite Temporary B-Tree Sort Overhead
**Learning:** When queries in `database.py` group and order by `exercise_name` or `date` on the `exercise_progress` table without covering indexes, SQLite resorts to creating temporary B-Trees (`USE TEMP B-TREE FOR ORDER BY` / `GROUP BY`). This incurs a measurable overhead, especially as the size of the progress history grows.
**Action:** Always verify query plans for commonly accessed tables and add precise, matching compound indexes in `init_db()` (e.g., `(exercise_name, id ASC)`) to eliminate implicit temporary sorting during reads.

## 2024-XX-XX - [O(n) memory to O(1) in get_progress_history]
**Learning:** The database query in `get_progress_history` was pulling the entire history of workouts into memory to extract only the last `limit_per_exercise` records. Over time, this unbounded growth would cause performance bottlenecks.
**Action:** Added an SQLite window function (`ROW_NUMBER() OVER PARTITION BY`) to limit results at the database level. Always look for "fetch everything then slice in Python" patterns as the database grows.
