"""SQLite implementation for canonical health metric persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Sequence

from health_dedup import metric_fingerprint
from health_models import HealthMetric, SleepSession, WearableActivity


class SQLiteHealthRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def save_metrics(self, user_id: int, records: Sequence[HealthMetric]) -> int:
        written = 0
        for record in records:
            if record.user_id != user_id:
                raise ValueError("cannot persist another user's health metric")
            provenance = record.provenance
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO health_metrics
                (user_id, metric_type, value, unit, observed_at, provider, connection_id,
                 upstream_id, source_device, source_app, data_origin, normalization_version,
                 fingerprint, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    record.metric_type.value,
                    record.value,
                    record.unit,
                    record.observed_at.isoformat(),
                    provenance.provider,
                    provenance.connection_id,
                    provenance.upstream_id,
                    provenance.source_device,
                    provenance.source_app,
                    provenance.data_origin,
                    provenance.normalization_version,
                    metric_fingerprint(record),
                    json.dumps(dict(record.metadata), sort_keys=True),
                ),
            )
            written += max(cursor.rowcount, 0)
        self.connection.commit()
        return written

    def save_sleep(self, user_id: int, records: Sequence[SleepSession]) -> int:
        if any(record.user_id != user_id for record in records):
            raise ValueError("cannot persist another user's sleep session")
        return 0

    def save_activities(self, user_id: int, records: Sequence[WearableActivity]) -> int:
        if any(record.user_id != user_id for record in records):
            raise ValueError("cannot persist another user's wearable activity")
        return 0
