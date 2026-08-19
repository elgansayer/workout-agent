"""SQL DDL for the canonical health platform.

The migration runner can execute these statements incrementally after existing
multi-user database ownership is verified.
"""

HEALTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS health_connections (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    state TEXT NOT NULL,
    granted_capabilities TEXT NOT NULL DEFAULT '[]',
    cursor TEXT,
    last_sync_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, provider, id)
);

CREATE TABLE IF NOT EXISTS health_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    metric_type TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    upstream_id TEXT,
    source_device TEXT,
    source_app TEXT,
    data_origin TEXT,
    normalization_version INTEGER NOT NULL DEFAULT 1,
    fingerprint TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(user_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_health_metrics_user_type_time
ON health_metrics(user_id, metric_type, observed_at);

CREATE TABLE IF NOT EXISTS health_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    cursor_before TEXT,
    cursor_after TEXT,
    fetched INTEGER NOT NULL DEFAULT 0,
    written INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    error_code TEXT
);
"""
