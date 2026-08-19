"""Status model for non-destructive legacy health data migration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LegacyMigrationStatus:
    user_id: int
    legacy_rows: int
    canonical_rows: int
    complete: bool

    def __post_init__(self) -> None:
        if self.user_id <= 0 or self.legacy_rows < 0 or self.canonical_rows < 0:
            raise ValueError("invalid legacy health migration status")
