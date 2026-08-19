"""Data-quality flags for normalized health inputs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class QualityFlag(StrEnum):
    STALE = "stale"
    OUTLIER = "outlier"
    DUPLICATE = "duplicate"
    PARTIAL = "partial"
    UNKNOWN_SOURCE = "unknown_source"


@dataclass(frozen=True, slots=True)
class DataQuality:
    flags: frozenset[QualityFlag] = frozenset()

    @property
    def usable_for_adaptation(self) -> bool:
        return not bool(self.flags & {QualityFlag.STALE, QualityFlag.OUTLIER, QualityFlag.UNKNOWN_SOURCE})
