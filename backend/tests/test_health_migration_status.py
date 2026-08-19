from health_migration_status import LegacyMigrationStatus


def test_legacy_migration_status_is_per_user():
    status = LegacyMigrationStatus(1, legacy_rows=10, canonical_rows=10, complete=True)
    assert status.complete
    assert status.user_id == 1
