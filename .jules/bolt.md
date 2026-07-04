## 2024-07-04 - Missing Index on `exercise_progress`
**Learning:** Frequent queries grouped by `exercise_name` on the `exercise_progress` table lead to O(n) table scans, significantly degrading performance as log entries grow. Functions like `get_recent_bests` (using a subquery grouping by `exercise_name` to find max ids) and `get_progress_history` are significantly delayed.
**Action:** Adding a composite index `(exercise_name, id)` drastically cuts down query times (e.g. from ~70ms down to ~13ms for 100k rows) and scales much better for group-by max aggregate queries common in the DB layer.
