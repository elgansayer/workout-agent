"""Conservative outlier detection for recovery inputs."""

from __future__ import annotations

from statistics import median


def is_relative_outlier(value: float, history: list[float], *, threshold: float = 0.5) -> bool:
    if len(history) < 7:
        return False
    baseline = median(history)
    if baseline == 0:
        return False
    return abs(value - baseline) / abs(baseline) >= threshold
