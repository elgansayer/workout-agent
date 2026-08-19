from health_schema import HEALTH_SCHEMA_SQL


def test_health_schema_contains_tenant_and_provenance_constraints():
    sql = HEALTH_SCHEMA_SQL.lower()
    assert "create table if not exists health_connections" in sql
    assert "create table if not exists health_metrics" in sql
    assert "create table if not exists health_sync_runs" in sql
    assert "user_id integer not null" in sql
    assert "normalization_version integer not null" in sql
    assert "unique(user_id, fingerprint)" in sql
