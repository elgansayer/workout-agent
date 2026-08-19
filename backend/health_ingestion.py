"""Provider-neutral normalization and in-memory ingestion primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from health_dedup import deduplicate_metrics
from health_models import HealthMetric, SleepSession, WearableActivity

NormalizedRecord = HealthMetric | SleepSession | WearableActivity


@dataclass(slots=True)
class HealthIngestionBatch:
    user_id: int
    records: list[NormalizedRecord] = field(default_factory=list)

    def add(self, record: NormalizedRecord) -> None:
        if record.user_id != self.user_id:
            raise ValueError("cannot ingest a health record for another user")
        self.records.append(record)

    def extend(self, records: Iterable[NormalizedRecord]) -> None:
        for record in records:
            self.add(record)

    def normalized(self) -> list[NormalizedRecord]:
        metrics = [item for item in self.records if isinstance(item, HealthMetric)]
        non_metrics = [item for item in self.records if not isinstance(item, HealthMetric)]
        return [*deduplicate_metrics(metrics), *non_metrics]
