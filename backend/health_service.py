"""Application service that enforces tenant-safe canonical health writes."""

from __future__ import annotations

from health_ingestion import HealthIngestionBatch
from health_models import HealthMetric, SleepSession, WearableActivity
from health_repository import HealthRepository


class HealthIngestionService:
    def __init__(self, repository: HealthRepository):
        self.repository = repository

    def persist(self, batch: HealthIngestionBatch) -> int:
        records = batch.normalized()
        metrics = [item for item in records if isinstance(item, HealthMetric)]
        sleep = [item for item in records if isinstance(item, SleepSession)]
        activities = [item for item in records if isinstance(item, WearableActivity)]
        return (
            self.repository.save_metrics(batch.user_id, metrics)
            + self.repository.save_sleep(batch.user_id, sleep)
            + self.repository.save_activities(batch.user_id, activities)
        )
