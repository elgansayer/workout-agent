"""Pure analytics helpers for the dashboard.

Everything here is dependency-free and unit-testable: strength scoring (DOTS),
estimated 1RM, simple linear projection, and mapping exercises to muscle groups
so training volume can be broken down by body part.
"""

from __future__ import annotations

from typing import Iterable, Sequence

# DOTS coefficients for men (Open Powerlifting). DOTS expresses strength
# relative to bodyweight on a single scale, so progress shows even as weight
# changes.
_DOTS_MEN = (-307.75076, 24.0900756, -0.1918759221, 0.0007391293, -0.000001093)


def epley_1rm(weight: float | None, reps: int | None) -> float | None:
    """Estimated one-rep max via the Epley formula."""
    if not weight or not reps:
        return None
    return round(weight * (1 + reps / 30), 1)


def dots_score(bodyweight_kg: float | None, total_kg: float | None) -> float | None:
    """Return a DOTS strength score for a lift/total at a bodyweight.

    ``total_kg`` is any kg figure to score (here, typically an estimated 1RM).
    """
    if not bodyweight_kg or not total_kg or bodyweight_kg <= 0 or total_kg <= 0:
        return None
    bw = max(40.0, min(210.0, float(bodyweight_kg)))  # clamp to the valid domain
    a, b, c, d, e = _DOTS_MEN
    denom = a + b * bw + c * bw**2 + d * bw**3 + e * bw**4
    if denom == 0:
        return None
    return round(total_kg * 500 / denom, 1)


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float] | None:
    """Least-squares fit. Returns (slope, intercept), or None if undefined."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov / var_x
    intercept = mean_y - slope * mean_x
    return slope, intercept


def project(points: Sequence[tuple[float, float]], x_target: float) -> float | None:
    """Project the value at ``x_target`` from (x, y) observations via a fit."""
    if len(points) < 2:
        return None
    fit = linear_fit([p[0] for p in points], [p[1] for p in points])
    if fit is None:
        return None
    slope, intercept = fit
    return round(slope * x_target + intercept, 1)


# Ordered keyword rules. The first group whose keyword appears in the (lower
# case) exercise name wins, so the order resolves overlaps such as "leg curl"
# (Legs) versus "bicep curl" (Arms) and "incline curl" (Arms) versus "incline
# press" (Chest).
_GROUP_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Core", ("crunch", "hanging leg", "leg raise", "knee raise", "plank", "sit-up", "sit up", "oblique", "ab wheel")),
    ("Legs", ("leg curl", "leg extension", "leg press", "calf", "squat", "lunge", "hamstring", "quad", "glute", "hip thrust", "rdl")),
    ("Shoulders", ("lateral", "rear delt", "reverse pec", "delt", "shoulder", "overhead press", "military", "upright")),
    ("Back", ("deadlift", "pull-up", "pullup", "pull up", "chin-up", "chin up", "chinup", "row", "pulldown", "lat ", "face pull")),
    ("Arms", ("curl", "tricep", "bicep", "pushdown", "skull", "preacher", "brachial", "cable extension", "overhead cable")),
    ("Chest", ("bench", "chest", "fly", "flye", "pec", "dip", "push-up", "push up", "incline", "press")),
]


def muscle_group_for(name: str) -> str:
    """Classify an exercise name into a broad muscle group."""
    lowered = " ".join(name.lower().split())
    for group, keywords in _GROUP_RULES:
        if any(keyword in lowered for keyword in keywords):
            return group
    return "Other"


def group_volumes(exercise_volumes: Iterable[dict]) -> dict[str, float]:
    """Sum per-exercise volume into muscle groups.

    ``exercise_volumes`` is an iterable of ``{"exercise": str, "volume": float}``.
    """
    totals: dict[str, float] = {}
    for row in exercise_volumes:
        group = muscle_group_for(row["exercise"])
        totals[group] = totals.get(group, 0.0) + float(row.get("volume") or 0.0)
    return {g: v for g, v in totals.items() if v > 0}
