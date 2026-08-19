from datetime import datetime, timezone

from health_ingestion import HealthIngestionBatch
from health_models import HealthMetric, MetricType, SourceProvenance
from health_service import HealthIngestionService


class FakeRepository:
    def __init__(self):
        self.calls = []

    def save_metrics(self, user_id, records):
        self.calls.append(("metrics", user_id, list(records)))
        return len(records)

    def save_sleep(self, user_id, records):
        self.calls.append(("sleep", user_id, list(records)))
        return len(records)

    def save_activities(self, user_id, records):
        self.calls.append(("activities", user_id, list(records)))
        return len(records)


def test_ingestion_service_routes_canonical_records_by_tenant():
    repo = FakeRepository()
    batch = HealthIngestionBatch(7)
    batch.add(HealthMetric(7, MetricType.STEPS, 1000, "count", datetime(2026, 8, 19, tzinfo=timezone.utc), SourceProvenance("garmin", "c1", "r1")))
    assert HealthIngestionService(repo).persist(batch) == 1
    assert all(call[1] == 7 for call in repo.calls)
