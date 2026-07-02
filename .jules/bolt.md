## 2024-XX-XX - [O(n) memory to O(1) in get_progress_history]
**Learning:** The database query in `get_progress_history` was pulling the entire history of workouts into memory to extract only the last `limit_per_exercise` records. Over time, this unbounded growth would cause performance bottlenecks.
**Action:** Added an SQLite window function (`ROW_NUMBER() OVER PARTITION BY`) to limit results at the database level. Always look for "fetch everything then slice in Python" patterns as the database grows.
