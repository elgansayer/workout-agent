"""Provider-neutral health synchronization run state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SyncState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class HealthSyncRun:
    user_id: int
    provider: str
    connection_id: str
    started_at: datetime
    state: SyncState = SyncState.RUNNING
    finished_at: datetime | None = None
    cursor_before: str | None = None
    cursor_after: str | None = None
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("sync run requires a positive user_id")
        if not self.provider.strip() or not self.connection_id.strip():
            raise ValueError("sync run requires provider and connection")
        if min(self.fetched, self.written, self.skipped) < 0:
            raise ValueError("sync counters cannot be negative")
