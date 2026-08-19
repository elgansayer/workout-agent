"""Versioned per-metric source precedence policy."""

from __future__ import annotations

from dataclasses import dataclass, field

from health_models import MetricType


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    version: int = 1
    overrides: dict[MetricType, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("source policy version must be positive")
        for providers in self.overrides.values():
            if len(providers) != len(set(providers)):
                raise ValueError("source policy cannot contain duplicate providers")
