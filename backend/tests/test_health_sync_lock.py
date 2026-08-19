from health_sync_lock import sync_lock_key


def test_sync_lock_is_user_provider_and_connection_scoped():
    assert sync_lock_key(7, "oura", "c1") == "health-sync:7:oura:c1"
    assert sync_lock_key(8, "oura", "c1") != sync_lock_key(7, "oura", "c1")
