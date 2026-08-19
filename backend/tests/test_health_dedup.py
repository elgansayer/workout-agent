from datetime import datetime, timedelta, timezone

import pytest

from health_dedup import deduplicate_metrics, metric_fingerprint, select_preferred_metric
from health_models import HealthMetric, MetricType, SourceProvenance


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def metric(user, provider, connection, upstream, value=60.0, when=NOW):
    return HealthMetric(
        user_id=user,
        metric_type=MetricType.RESTING_HR_BPM,
        value=value,
        unit="bpm",
        observed_at=when,
        provenance=SourceProvenance(provider, connection, upstream_id=upstream),
    )


def test_upstream_identity_is_stable_and_connection_scoped():
    first = metric(1, "garmin", "a", "record-1")
    same = metric(1, "garmin", "a", "record-1", value=61)
    other_connection = metric(1, "garmin", "b", "record-1")
    assert metric_fingerprint(first) == metric_fingerprint(same)
    assert metric_fingerprint(first) != metric_fingerprint(other_connection)


def test_deduplication_never_crosses_users():
    first = metric(1, "garmin", "a", "record-1")
    duplicate = metric(1, "garmin", "a", "record-1")
    other_user = metric(2, "garmin", "a", "record-1")
    assert deduplicate_metrics([first, duplicate, other_user]) == [first, other_user]


def test_default_precedence_prefers_direct_garmin_over_health_connect():
    aggregated = metric(1, "health_connect", "phone", "hc-1", when=NOW + timedelta(minutes=1))
    direct = metric(1, "garmin", "watch", "g-1")
    assert select_preferred_metric([aggregated, direct]) is direct


def test_source_selection_rejects_cross_tenant_candidates():
    with pytest.raises(ValueError, match="tenant"):
        select_preferred_metric([metric(1, "garmin", "a", "1"), metric(2, "oura", "b", "2")])
