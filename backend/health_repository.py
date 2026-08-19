"""Persistence contract for canonical health records.

Concrete database wiring can implement this protocol while keeping provider
adapters independent of database.py internals.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from health_connection import HealthConnection
from health_models import HealthMetric, SleepSession, WearableActivity
from health_sync import HealthSyncRun


class HealthRepository(Protocol):
    def save_connection(self, connection: HealthConnection) -> None: ...

    def save_metrics(self, user_id: int, records: Sequence[HealthMetric]) -> int: ...

    def save_sleep(self, user_id: int, records: Sequence[SleepSession]) -> int: ...

    def save_activities(self, user_id: int, records: Sequence[WearableActivity]) -> int: ...

    def save_sync_run(self, run: HealthSyncRun) -> None: ...
