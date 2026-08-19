"""Compose normalized health inputs into explainable daily training guidance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from adaptive_training import TrainingAdjustment, recommend_adjustment
from health_models import HealthMetric
from health_readiness import ReadinessState, derive_readiness
from health_summary import build_daily_summary
from recovery import RecoveryFeatures, RecoveryObservation, compute_recovery_features


@dataclass(frozen=True, slots=True)
class DailyHealthDecision:
    readiness: ReadinessState
    adjustment: TrainingAdjustment
    recovery_features: RecoveryFeatures


def build_daily_health_decision(
    *,
    user_id: int,
    day: date,
    metrics: Iterable[HealthMetric],
    current_recovery: RecoveryObservation,
    recovery_history: Iterable[RecoveryObservation],
    stale: bool = False,
) -> DailyHealthDecision:
    # Building the summary first enforces tenant/source invariants even when the
    # caller has already derived recovery observations from those metrics.
    build_daily_summary(user_id=user_id, day=day, metrics=metrics)
    features = compute_recovery_features(current_recovery, recovery_history)
    readiness = derive_readiness(features)
    adjustment = recommend_adjustment(features, stale=stale)
    return DailyHealthDecision(readiness, adjustment, features)
