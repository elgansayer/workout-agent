from health_observability import ConnectorMetric


def test_observability_labels_contain_no_tenant_identity():
    metric = ConnectorMetric("garmin", "sync", "success", 120, records=20)
    assert metric.labels() == {"provider": "garmin", "operation": "sync", "outcome": "success"}
    assert "user_id" not in metric.labels()
