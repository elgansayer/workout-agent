from datetime import datetime, timezone

import pytest

from health_sync import HealthSyncRun, SyncState


NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def test_sync_run_is_tenant_and_connection_scoped():
    run = HealthSyncRun(1, "oura", "connection-1", NOW, state=SyncState.RUNNING)
    assert run.user_id == 1
    assert run.connection_id == "connection-1"


def test_sync_run_rejects_invalid_identity_and_counters():
    with pytest.raises(ValueError):
        HealthSyncRun(0, "oura", "connection-1", NOW)
    with pytest.raises(ValueError):
        HealthSyncRun(1, "oura", "connection-1", NOW, fetched=-1)
