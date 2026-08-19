"""Explainable, bounded adaptive-training decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from recovery import RecoveryFeatures


class TrainingDecision(StrEnum):
    PROCEED = "proceed"
    REDUCE_VOLUME = "reduce_volume"
    REDUCE_INTENSITY = "reduce_intensity"
    RECOVERY_SESSION = "recovery_session"
    CONFIRM = "confirm"


@dataclass(frozen=True, slots=True)
class TrainingAdjustment:
    decision: TrainingDecision
    volume_multiplier: float
    intensity_multiplier: float
    confidence: float
    reasons: tuple[str, ...]
    rule_version: int = 1


def recommend_adjustment(features: RecoveryFeatures, *, stale: bool = False) -> TrainingAdjustment:
    reasons: list[str] = []
    risk = 0

    if stale or features.confidence < 0.2:
        return TrainingAdjustment(TrainingDecision.CONFIRM, 1.0, 1.0, features.confidence, ("insufficient fresh recovery data",))

    if features.sleep_delta_minutes is not None and features.sleep_delta_minutes <= -90:
        risk += 2
        reasons.append("sleep is at least 90 minutes below personal baseline")
    elif features.sleep_delta_minutes is not None and features.sleep_delta_minutes <= -45:
        risk += 1
        reasons.append("sleep is below personal baseline")

    if features.resting_hr_delta_bpm is not None and features.resting_hr_delta_bpm >= 8:
        risk += 2
        reasons.append("resting heart rate is elevated versus personal baseline")
    elif features.resting_hr_delta_bpm is not None and features.resting_hr_delta_bpm >= 5:
        risk += 1
        reasons.append("resting heart rate is moderately elevated")

    if features.hrv_delta_pct is not None and features.hrv_delta_pct <= -20:
        risk += 1
        reasons.append("HRV is materially below personal baseline")

    if features.training_load_delta_pct is not None and features.training_load_delta_pct >= 30:
        risk += 1
        reasons.append("recent training load is materially above baseline")

    # Bounded outputs: this layer cannot reduce volume by more than 40% or
    # intensity by more than 15%. Programme rewriting belongs elsewhere.
    if risk >= 4:
        return TrainingAdjustment(TrainingDecision.RECOVERY_SESSION, 0.6, 0.85, features.confidence, tuple(reasons))
    if risk >= 2:
        return TrainingAdjustment(TrainingDecision.REDUCE_VOLUME, 0.75, 0.95, features.confidence, tuple(reasons))
    if risk == 1:
        return TrainingAdjustment(TrainingDecision.REDUCE_INTENSITY, 0.9, 0.95, features.confidence, tuple(reasons))
    return TrainingAdjustment(TrainingDecision.PROCEED, 1.0, 1.0, features.confidence, ("recovery features are within current bounds",))
