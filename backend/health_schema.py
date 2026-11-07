"""SQL DDL for the canonical health platform.

The migration runner can execute these statements incrementally after existing
multi-user database ownership is verified.

Every health table carries a non-null owner column with a database-level
integrity check, an indexed access path that leads with that owner and a
trigger that makes the owner immutable.  The canonical health platform still
uses its own integer tenant identity, so the foreign key to ``users(id)`` is
deferred to the health-platform identity migration; the registry in
:mod:`ownership` records that exception explicitly so the contract is audited
rather than assumed.
"""

from __future__ import annotations

from ownership import owner_immutability_trigger_sql, registered_table

_HEALTH_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS health_connections (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL CHECK (user_id > 0),
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
    user_id INTEGER NOT NULL CHECK (user_id > 0),
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
    user_id INTEGER NOT NULL CHECK (user_id > 0),
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

CREATE INDEX IF NOT EXISTS idx_health_connections_user_provider
ON health_connections(user_id, provider);

CREATE INDEX IF NOT EXISTS idx_health_sync_runs_user_started
ON health_sync_runs(user_id, started_at DESC, id DESC);
"""

_HEALTH_TABLE_NAMES = ("health_connections", "health_metrics", "health_sync_runs")

# A record can never be reassigned to a different owner.  The triggers come
# from the same builder the ownership audit verifies, so the schema and the
# contract cannot drift.
_HEALTH_OWNER_TRIGGERS = "\n".join(
    owner_immutability_trigger_sql(registered_table(name)) + ";"
    for name in _HEALTH_TABLE_NAMES
)

HEALTH_SCHEMA_SQL = _HEALTH_TABLE_DDL + "\n" + _HEALTH_OWNER_TRIGGERS + "\n"
