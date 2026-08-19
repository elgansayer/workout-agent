"""Privacy-safe connector observability labels."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConnectorMetric:
    provider: str
    operation: str
    outcome: str
    duration_ms: int
    records: int = 0

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.operation.strip() or not self.outcome.strip():
            raise ValueError("connector metric dimensions are required")
        if self.duration_ms < 0 or self.records < 0:
            raise ValueError("connector metric values cannot be negative")

    def labels(self) -> dict[str, str]:
        # Deliberately excludes user_id, connection_id and health values.
        return {"provider": self.provider, "operation": self.operation, "outcome": self.outcome}
