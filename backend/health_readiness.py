"""Explainable provider-neutral readiness state derived from recovery features."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from recovery import RecoveryFeatures


class ReadinessBand(StrEnum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    CAUTION = "caution"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class ReadinessState:
    band: ReadinessBand
    confidence: float
    reasons: tuple[str, ...]
    algorithm_version: int = 1


def derive_readiness(features: RecoveryFeatures) -> ReadinessState:
    if features.confidence < 0.2:
        return ReadinessState(ReadinessBand.UNKNOWN, features.confidence, ("insufficient baseline coverage",))
    reasons: list[str] = []
    severity = 0
    if features.sleep_delta_minutes is not None and features.sleep_delta_minutes <= -90:
        severity += 2
        reasons.append("sleep materially below baseline")
    if features.resting_hr_delta_bpm is not None and features.resting_hr_delta_bpm >= 8:
        severity += 2
        reasons.append("resting heart rate materially above baseline")
    if features.hrv_delta_pct is not None and features.hrv_delta_pct <= -20:
        severity += 1
        reasons.append("HRV materially below baseline")
    if severity >= 3:
        return ReadinessState(ReadinessBand.LOW, features.confidence, tuple(reasons))
    if severity:
        return ReadinessState(ReadinessBand.CAUTION, features.confidence, tuple(reasons))
    return ReadinessState(ReadinessBand.NORMAL, features.confidence, ("available recovery features are within current bounds",))
