from health_sync_progress import SyncProgress


def test_sync_progress_contains_no_user_health_values():
    progress = SyncProgress("oura", "backfill", 120, complete=False)
    assert progress.provider == "oura"
    assert progress.processed == 120
    assert not hasattr(progress, "user_id")
