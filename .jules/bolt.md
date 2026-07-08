## 2024-07-08 - SQLite Compound Indexing for Pagination

**Learning:** When adding or fetching ordered/grouped queries in SQLite (e.g., `ORDER BY date DESC, id DESC LIMIT X`), if a compound index matching the exact columns and directions is missing, SQLite falls back to creating a temporary B-Tree sort on every read. This was severely bottlenecking the `history`, `daily_log`, and `body_metrics` endpoints, particularly as the tables grew.

**Action:** Always ensure compound indexes match the specific query clauses exactly (e.g., `CREATE INDEX ON table (date DESC, id DESC)`) to enable instant limit/offset pagination and avoid slow in-memory sorts.
