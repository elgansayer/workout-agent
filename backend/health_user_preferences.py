"""Per-user source preference model for supported metric families."""

from __future__ import annotations

from dataclasses import dataclass

from health_models import MetricType


@dataclass(frozen=True, slots=True)
class HealthSourcePreference:
    user_id: int
    metric_type: MetricType
    providers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.providers:
            raise ValueError("source preference requires tenant and providers")
        if len(self.providers) != len(set(self.providers)):
            raise ValueError("source preference providers must be unique")
