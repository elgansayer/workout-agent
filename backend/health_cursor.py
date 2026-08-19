"""Tenant-bound incremental sync cursor identity."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SyncCursor:
    user_id: int
    provider: str
    connection_id: str
    value: str

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.provider.strip() or not self.connection_id.strip() or not self.value.strip():
            raise ValueError("sync cursor requires tenant, provider, connection and value")
