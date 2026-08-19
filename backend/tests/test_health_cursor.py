import pytest

from health_cursor import SyncCursor


def test_sync_cursor_is_connection_and_tenant_bound():
    cursor = SyncCursor(1, "oura", "c1", "next-page")
    assert cursor.connection_id == "c1"
    with pytest.raises(ValueError):
        SyncCursor(0, "oura", "c1", "next-page")
