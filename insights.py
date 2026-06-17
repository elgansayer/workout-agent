"""Self-improving coaching analysis: turn raw history into actionable insight.

This is the agent's memory and judgement layer. Where the rest of the system
records what happened, this module reasons about whether the coaching is
*working*: which lifts are progressing, which have stalled, how recovery is
trending, and what the coach should change next. The output is fed into the
Gemini prompt so each day's plan is driven by trends, not just the last session.

Everything here is pure and dependency-light (only ``analytics``), so it is
fully unit-testable without a database, network, or model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from analytics import epley_1rm, linear_fit

# A lift needs at least this many logged sessions before a trend is meaningful.
MIN_SESSIONS_FOR_TREND = 3
# Relative change (latest vs first in window) that counts as real movement.
PROGRESS_THRESHOLD = 0.02   # +2 percent or better is progressing
REGRESS_THRESHOLD = -0.02   # -2 percent or worse is regressing
# Sessions without a new best before a lift is treated as stalled.
STALL_SESSIONS = 3
# Recovery thresholds.
LOW_SLEEP_HOURS = 6.0
GOOD_SLEEP_HOURS = 7.0
# Minimum readings before a body-metric trend is reported.
MIN_READINGS_FOR_TREND = 3


@dataclass(frozen=True)
class LiftInsight:
    """Trend judgement for a single exercise over its recent sessions."""

    name: str
    metric: str                 # "kg" (estimated 1RM) or "reps" (bodyweight)
    sessions: int
    latest: float | None        # latest score in the chosen metric
    best: float | None          # best score ever in the window
    change_pct: float | None    # latest vs first, as a fraction
    slope_per_session: float | None
    trend: str                  # progressing | stalling | regressing | new
    sessions_since_best: int | None
    intervention: str | None    # what to change when not progressing

    def as_line(self) -> str:
        unit = "kg e1RM" if self.metric == "kg" else "reps"
        bits = [f"{self.name}: {self.trend}"]
        if self.latest is not None:
            bits.append(f"latest {self.latest:g} {unit}")
        if self.change_pct is not None:
            bits.append(f"{self.change_pct * 100:+.0f}% over {self.sessions} sessions")
        if self.sessions_since_best:
            bits.append(f"{self.sessions_since_best} since best")
        line = ", ".join(bits)
        if self.intervention:
            line += f" -> {self.intervention}"
        return line


@dataclass(frozen=True)
class RecoveryInsight:
    """Recovery picture from the latest reading plus recent trends."""

    sleep_hours: float | None
    resting_hr: int | None
    resting_hr_trend: str | None    # rising | falling | steady
    weight_kg: float | None
    weight_trend: str | None
    body_fat_pct: float | None
    body_fat_trend: str | None
    status: str                     # good | fair | poor | unknown
    directive: str                  # what the coach should do about volume today

    def as_text(self) -> str:
        bits = [f"Recovery status: {self.status}."]
        if self.sleep_hours is not None:
            bits.append(f"Sleep {self.sleep_hours:g} h.")
        if self.resting_hr is not None:
            trend = f" ({self.resting_hr_trend})" if self.resting_hr_trend else ""
            bits.append(f"Resting HR {self.resting_hr}{trend}.")
        if self.weight_kg is not None:
            trend = f" ({self.weight_trend})" if self.weight_trend else ""
            bits.append(f"Bodyweight {self.weight_kg:g} kg{trend}.")
        if self.body_fat_pct is not None:
            trend = f" ({self.body_fat_trend})" if self.body_fat_trend else ""
            bits.append(f"Body fat {self.body_fat_pct:g}%{trend}.")
        bits.append(f"Directive: {self.directive}")
        return " ".join(bits)


@dataclass(frozen=True)
class TrainingInsights:
    """The full self-review handed to the coach each day."""

    lifts: list[LiftInsight]
    recovery: RecoveryInsight
    headline: str

    def priorities(self) -> list[LiftInsight]:
        """Lifts most in need of intervention, worst first."""
        order = {"regressing": 0, "stalling": 1, "progressing": 2, "new": 3}
        flagged = [lift for lift in self.lifts if lift.trend in ("regressing", "stalling")]
        return sorted(flagged, key=lambda lift: order.get(lift.trend, 9))

    def as_text(self) -> str:
        lines = [self.headline, "", self.recovery.as_text(), ""]
        if self.lifts:
            lines.append("Per-lift trend analysis:")
            lines.extend(f"- {lift.as_line()}" for lift in self.lifts)
        else:
            lines.append("Per-lift trend analysis: not enough history yet.")
        return "\n".join(lines)

    def as_message(self, week: int | None = None, block_name: str | None = None) -> str:
        """A phone-friendly weekly self-review for Telegram (plain text)."""
        header = "Weekly self-review"
        if week is not None:
            header += f" - Week {week}"
            if block_name:
                header += f" ({block_name})"
        lines = [header, self.headline, ""]

        progressing = [lift for lift in self.lifts if lift.trend == "progressing"]
        flagged = self.priorities()

        if progressing:
            names = ", ".join(
                f"{lift.name} ({lift.change_pct * 100:+.0f}%)"
                if lift.change_pct is not None
                else lift.name
                for lift in progressing
            )
            lines.append(f"Progressing: {names}.")
        if flagged:
            lines.append("Needs attention:")
            for lift in flagged:
                action = f" {lift.intervention}" if lift.intervention else ""
                lines.append(f"- {lift.name}: {lift.trend}.{action}")
        if not progressing and not flagged:
            lines.append("Not enough logged history yet to spot trends. Keep logging.")

        lines.append("")
        lines.append(self.recovery.as_text())
        return "\n".join(lines).strip()


def _series_scores(entries: Sequence[dict[str, Any]]) -> tuple[list[float], str]:
    """Reduce an exercise's logged top sets to one comparable score series.

    Weighted lifts use the Epley estimated 1RM; bodyweight lifts fall back to
    reps. The two are never mixed, so the series stays internally comparable.
    """
    weighted: list[float] = []
    for entry in entries:
        weight = entry.get("top_weight_kg")
        reps = entry.get("top_reps")
        if weight and reps:
            value = epley_1rm(weight, reps)
            if value is not None:
                weighted.append(value)
    if len(weighted) >= 2:
        return weighted, "kg"

    rep_only = [float(e["top_reps"]) for e in entries if e.get("top_reps")]
    if len(rep_only) >= 2:
        return rep_only, "reps"
    return (weighted or rep_only), ("kg" if weighted else "reps")


def _classify(scores: Sequence[float]) -> tuple[str, float | None, float | None]:
    """Return (trend, change_pct, slope_per_session) for a score series."""
    n = len(scores)
    if n < MIN_SESSIONS_FOR_TREND:
        return "new", None, None
    first, last = scores[0], scores[-1]
    change_pct = (last - first) / first if first else None
    fit = linear_fit(list(range(n)), list(scores))
    slope = fit[0] if fit else None
    if change_pct is None:
        return "new", None, slope
    if change_pct >= PROGRESS_THRESHOLD:
        return "progressing", change_pct, slope
    if change_pct <= REGRESS_THRESHOLD:
        return "regressing", change_pct, slope
    return "stalling", change_pct, slope


def _sessions_since_best(scores: Sequence[float]) -> int | None:
    """How many sessions have passed since the best score in the window."""
    if not scores:
        return None
    best_idx = max(range(len(scores)), key=lambda i: scores[i])
    return len(scores) - 1 - best_idx


def _intervention(trend: str, since_best: int | None, recovery_status: str) -> str | None:
    """Suggest a concrete change for a lift that is not progressing."""
    if trend == "progressing" or trend == "new":
        return None
    if recovery_status == "poor":
        return "deload ~10% and rebuild with strict form while recovery is low"
    if trend == "regressing":
        return "deload ~10% then micro-load back up; check form and rest times"
    # Stalling with adequate recovery: change the stimulus.
    if since_best is not None and since_best >= STALL_SESSIONS:
        return "break the plateau: drop to a fresh rep range or swap for a variation"
    return "add a rep or a tiny load increase to nudge past the plateau"


def analyse_lift(name: str, entries: Sequence[dict[str, Any]], recovery_status: str = "unknown") -> LiftInsight:
    """Build a trend judgement for one exercise from its logged top sets."""
    scores, metric = _series_scores(entries)
    sessions = len(scores)
    trend, change_pct, slope = _classify(scores)
    since_best = _sessions_since_best(scores)
    latest = round(scores[-1], 1) if scores else None
    best = round(max(scores), 1) if scores else None
    intervention = _intervention(trend, since_best, recovery_status)
    return LiftInsight(
        name=name,
        metric=metric,
        sessions=sessions,
        latest=latest,
        best=best,
        change_pct=change_pct,
        slope_per_session=round(slope, 2) if slope is not None else None,
        trend=trend,
        sessions_since_best=since_best,
        intervention=intervention,
    )


def _trend_of(values: Sequence[float], rising_is: str, falling_is: str, tol: float) -> str | None:
    """Classify the direction of a short numeric series."""
    clean = [v for v in values if v is not None]
    if len(clean) < MIN_READINGS_FOR_TREND:
        return None
    fit = linear_fit(list(range(len(clean))), list(clean))
    if fit is None:
        return None
    slope = fit[0]
    if slope > tol:
        return rising_is
    if slope < -tol:
        return falling_is
    return "steady"


def analyse_recovery(
    body_metrics: Sequence[dict[str, Any]] | None,
    recovery: dict[str, Any] | None,
) -> RecoveryInsight:
    """Summarise recovery from the latest reading and recent body-metric trends."""
    readings = list(body_metrics or [])
    latest = readings[-1] if readings else {}

    sleep_hours = (recovery or {}).get("sleep_hours")
    resting_hr = latest.get("resting_hr") or (recovery or {}).get("resting_hr")
    weight_kg = latest.get("weight_kg") or (recovery or {}).get("weight_kg")
    body_fat_pct = latest.get("body_fat_pct") or (recovery or {}).get("body_fat_pct")

    recent = readings[-7:]
    rhr_trend = _trend_of([r.get("resting_hr") for r in recent], "rising", "falling", 0.3)
    weight_trend = _trend_of([r.get("weight_kg") for r in recent], "rising", "falling", 0.05)
    bf_trend = _trend_of([r.get("body_fat_pct") for r in recent], "rising", "falling", 0.05)

    # Decide an overall status and a volume directive for today.
    poor = (sleep_hours is not None and sleep_hours < LOW_SLEEP_HOURS) or rhr_trend == "rising"
    good = (
        sleep_hours is not None
        and sleep_hours >= GOOD_SLEEP_HOURS
        and rhr_trend != "rising"
    )
    if poor:
        status = "poor"
        directive = "trim volume and intensity today; keep form strict, no grinding reps"
    elif good:
        status = "good"
        directive = "recovery is solid; push the prescribed progressions confidently"
    elif sleep_hours is not None or resting_hr is not None:
        status = "fair"
        directive = "hold the plan; progress only lifts that clearly earned it"
    else:
        status = "unknown"
        directive = "no recovery data; progress conservatively on logged evidence alone"

    return RecoveryInsight(
        sleep_hours=sleep_hours,
        resting_hr=int(resting_hr) if resting_hr is not None else None,
        resting_hr_trend=rhr_trend,
        weight_kg=round(float(weight_kg), 1) if weight_kg is not None else None,
        weight_trend=weight_trend,
        body_fat_pct=round(float(body_fat_pct), 1) if body_fat_pct is not None else None,
        body_fat_trend=bf_trend,
        status=status,
        directive=directive,
    )


def _headline(lifts: Sequence[LiftInsight], recovery: RecoveryInsight) -> str:
    """A one-line summary of where training stands."""
    counts = {"progressing": 0, "stalling": 0, "regressing": 0, "new": 0}
    for lift in lifts:
        counts[lift.trend] = counts.get(lift.trend, 0) + 1
    parts = [
        f"{counts['progressing']} progressing",
        f"{counts['stalling']} stalling",
        f"{counts['regressing']} regressing",
    ]
    return f"Self-review: {', '.join(parts)}; recovery {recovery.status}."


def build_insights(
    progress_history: dict[str, list[dict[str, Any]]] | None,
    body_metrics: Sequence[dict[str, Any]] | None = None,
    recovery: dict[str, Any] | None = None,
) -> TrainingInsights:
    """Assemble the full self-review from stored history and recovery data.

    ``progress_history`` is the shape returned by
    ``database.get_progress_history`` (name -> chronological list of top sets).
    """
    recovery_insight = analyse_recovery(body_metrics, recovery)
    lifts = [
        analyse_lift(name, entries, recovery_insight.status)
        for name, entries in sorted((progress_history or {}).items())
        if entries
    ]
    # Surface the lifts that need attention first, then the rest by name.
    order = {"regressing": 0, "stalling": 1, "progressing": 2, "new": 3}
    lifts.sort(key=lambda lift: (order.get(lift.trend, 9), lift.name))
    headline = _headline(lifts, recovery_insight)
    return TrainingInsights(lifts=lifts, recovery=recovery_insight, headline=headline)
