"""Safe sync progress projection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SyncProgress:
    provider: str
    phase: str
    processed: int
    complete: bool = False

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.phase.strip() or self.processed < 0:
            raise ValueError("invalid sync progress")
