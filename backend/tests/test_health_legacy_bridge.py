from datetime import datetime, timezone

from health_legacy_bridge import legacy_body_metrics
from health_models import MetricType


def test_legacy_body_metrics_become_canonical_with_explicit_provenance():
    records = legacy_body_metrics(user_id=1, observed_at=datetime(2026, 8, 19, tzinfo=timezone.utc), connection_id="legacy", weight_kg=80, body_fat_pct=15)
    assert {record.metric_type for record in records} == {MetricType.WEIGHT_KG, MetricType.BODY_FAT_PCT}
    assert all(record.provenance.provider == "legacy_body_metrics" for record in records)
