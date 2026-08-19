"""Provider-neutral recovery feature calculation.

The engine produces explainable features only. It does not diagnose illness or
make medical decisions and does not require an AI provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    sleep_minutes: float | None = None
    resting_hr_bpm: float | None = None
    hrv_rmssd_ms: float | None = None
    recent_training_load: float | None = None


@dataclass(frozen=True, slots=True)
class RecoveryFeatures:
    sleep_delta_minutes: float | None
    resting_hr_delta_bpm: float | None
    hrv_delta_pct: float | None
    training_load_delta_pct: float | None
    history_days: int
    confidence: float


def _values(history: Iterable[RecoveryObservation], attribute: str) -> list[float]:
    return [value for item in history if (value := getattr(item, attribute)) is not None]


def _delta_pct(current: float | None, baseline_values: list[float]) -> float | None:
    if current is None or not baseline_values:
        return None
    baseline = median(baseline_values)
    if baseline == 0:
        return None
    return ((current - baseline) / baseline) * 100.0


def compute_recovery_features(
    current: RecoveryObservation,
    history: Iterable[RecoveryObservation],
) -> RecoveryFeatures:
    history = list(history)
    sleep = _values(history, "sleep_minutes")
    resting_hr = _values(history, "resting_hr_bpm")
    hrv = _values(history, "hrv_rmssd_ms")
    load = _values(history, "recent_training_load")

    sleep_delta = None
    if current.sleep_minutes is not None and sleep:
        sleep_delta = current.sleep_minutes - median(sleep)

    available_current = sum(
        value is not None
        for value in (current.sleep_minutes, current.resting_hr_bpm, current.hrv_rmssd_ms, current.recent_training_load)
    )
    coverage = available_current / 4.0
    history_factor = min(len(history) / 14.0, 1.0)

    return RecoveryFeatures(
        sleep_delta_minutes=sleep_delta,
        resting_hr_delta_bpm=(
            current.resting_hr_bpm - median(resting_hr)
            if current.resting_hr_bpm is not None and resting_hr
            else None
        ),
        hrv_delta_pct=_delta_pct(current.hrv_rmssd_ms, hrv),
        training_load_delta_pct=_delta_pct(current.recent_training_load, load),
        history_days=len(history),
        confidence=round(coverage * history_factor, 3),
    )
