## 2024-06-25 - Prevent In-Memory Slicing Over Time-Series Data

**Learning:** Fetching historical data like exercise logs and filtering via python array slicing (`list[-limit:]`) becomes a massive memory and CPU bottleneck as the database grows because it loads the full dataset. Using standard SQL limits doesn't work when we need exactly N latest records per group.

**Action:** Use SQL Window functions, specifically `ROW_NUMBER() OVER (PARTITION BY group_col ORDER BY sort_col DESC)`, inside a subquery to constrain the dataset entirely in the database engine, returning only exactly what is needed for immediate rendering.
