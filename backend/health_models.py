"""Canonical provider-neutral health domain models.

These models are deliberately independent from provider payloads and database
implementation details. Persistence migrations can adopt them incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Mapping


class MetricType(StrEnum):
    WEIGHT_KG = "weight_kg"
    BODY_FAT_PCT = "body_fat_pct"
    RESTING_HR_BPM = "resting_hr_bpm"
    HRV_RMSSD_MS = "hrv_rmssd_ms"
    STEPS = "steps"
    SLEEP_DURATION_MIN = "sleep_duration_min"
    SPO2_PCT = "spo2_pct"
    RESPIRATION_BPM = "respiration_bpm"
    STRESS_SCORE = "stress_score"
    RECOVERY_SCORE = "recovery_score"


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    provider: str
    connection_id: str
    upstream_id: str | None = None
    source_device: str | None = None
    source_app: str | None = None
    data_origin: str | None = None
    normalization_version: int = 1

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.connection_id.strip():
            raise ValueError("provider and connection_id are required")
        if self.normalization_version < 1:
            raise ValueError("normalization_version must be positive")


@dataclass(frozen=True, slots=True)
class HealthMetric:
    user_id: int
    metric_type: MetricType
    value: float
    unit: str
    observed_at: datetime
    provenance: SourceProvenance
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("health metric requires a positive user_id")
        if not self.unit.strip():
            raise ValueError("metric unit is required")


@dataclass(frozen=True, slots=True)
class SleepSession:
    user_id: int
    started_at: datetime
    ended_at: datetime
    provenance: SourceProvenance
    stages_minutes: Mapping[str, int] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("sleep session requires a positive user_id")
        if self.ended_at <= self.started_at:
            raise ValueError("sleep session must end after it starts")


@dataclass(frozen=True, slots=True)
class WearableActivity:
    user_id: int
    activity_type: str
    started_at: datetime
    ended_at: datetime
    provenance: SourceProvenance
    distance_m: float | None = None
    energy_kcal: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("activity requires a positive user_id")
        if self.ended_at <= self.started_at:
            raise ValueError("activity must end after it starts")


@dataclass(frozen=True, slots=True)
class DailyHealthSummary:
    user_id: int
    day: date
    metrics: Mapping[MetricType, float]
    selected_sources: Mapping[MetricType, SourceProvenance]
    precedence_version: int = 1
    computed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("daily summary requires a positive user_id")
        if self.precedence_version < 1:
            raise ValueError("precedence_version must be positive")
        missing = set(self.metrics) - set(self.selected_sources)
        if missing:
            raise ValueError(f"missing provenance for metrics: {sorted(missing)}")
