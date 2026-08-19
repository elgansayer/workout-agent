from datetime import datetime, timedelta, timezone

import pytest

from connectors.health_connect_ingest import HealthConnectUpload, replay_key


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def upload(**overrides):
    values = dict(user_id=1, device_id="pixel", batch_id="batch-1", generated_at=NOW, record_count=10)
    values.update(overrides)
    return HealthConnectUpload(**values)


def test_upload_is_tenant_bound_and_replay_key_is_stable():
    item = upload()
    item.validate(authenticated_user_id=1, now=NOW)
    assert replay_key(item) == "1:pixel:batch-1"
    with pytest.raises(ValueError, match="tenant"):
        item.validate(authenticated_user_id=2, now=NOW)


def test_upload_rejects_unbounded_or_future_batches():
    with pytest.raises(ValueError, match="bounds"):
        upload(record_count=5001).validate(authenticated_user_id=1, now=NOW)
    with pytest.raises(ValueError, match="future"):
        upload(generated_at=NOW + timedelta(hours=1)).validate(authenticated_user_id=1, now=NOW)
