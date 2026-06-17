"""Tests for the self-improving coaching analysis (insights.py)."""

from __future__ import annotations

import insights


def _sets(*pairs: tuple[float | None, int | None]) -> list[dict]:
    """Build a chronological list of top-set rows from (weight, reps) pairs."""
    rows = []
    for i, (weight, reps) in enumerate(pairs):
        rows.append(
            {
                "top_weight_kg": weight,
                "top_reps": reps,
                "sets": 3,
                "date": f"2026-01-{i + 1:02d}",
            }
        )
    return rows


def test_progressing_lift_is_detected():
    entries = _sets((100, 8), (102.5, 8), (105, 8), (107.5, 8))
    lift = insights.analyse_lift("Deadlift", entries, "good")
    assert lift.trend == "progressing"
    assert lift.change_pct is not None and lift.change_pct > 0
    assert lift.intervention is None
    assert lift.metric == "kg"


def test_stalling_lift_is_flagged_with_intervention():
    entries = _sets((100, 8), (100, 8), (100, 8), (100, 8))
    lift = insights.analyse_lift("Bench", entries, "good")
    assert lift.trend == "stalling"
    assert lift.intervention is not None


def test_regressing_lift_with_poor_recovery_suggests_deload():
    entries = _sets((110, 8), (107.5, 8), (105, 8), (100, 8))
    lift = insights.analyse_lift("Squat", entries, "poor")
    assert lift.trend == "regressing"
    assert "deload" in lift.intervention.lower()


def test_new_lift_has_no_trend():
    lift = insights.analyse_lift("Curl", _sets((20, 12), (20, 12)), "good")
    # Only two sessions: below the minimum for a trend judgement.
    assert lift.trend == "new"
    assert lift.intervention is None


def test_bodyweight_lift_uses_reps_metric():
    entries = _sets((None, 8), (None, 9), (None, 10), (None, 11))
    lift = insights.analyse_lift("Pull-Up", entries, "good")
    assert lift.metric == "reps"
    assert lift.trend == "progressing"


def test_sessions_since_best_counts_from_peak():
    entries = _sets((100, 8), (110, 8), (105, 8), (105, 8))
    lift = insights.analyse_lift("Row", entries, "good")
    assert lift.sessions_since_best == 2


def test_recovery_poor_on_short_sleep():
    rec = insights.analyse_recovery([], {"sleep_hours": 5.0})
    assert rec.status == "poor"
    assert "trim" in rec.directive.lower()


def test_recovery_good_on_solid_sleep_and_steady_hr():
    metrics = [
        {"date": "2026-01-01", "resting_hr": 58, "weight_kg": 82.0},
        {"date": "2026-01-02", "resting_hr": 58, "weight_kg": 81.9},
        {"date": "2026-01-03", "resting_hr": 58, "weight_kg": 81.8},
    ]
    rec = insights.analyse_recovery(metrics, {"sleep_hours": 8.0})
    assert rec.status == "good"
    assert rec.weight_trend == "falling"


def test_recovery_rising_hr_is_poor():
    metrics = [
        {"date": "2026-01-01", "resting_hr": 55},
        {"date": "2026-01-02", "resting_hr": 60},
        {"date": "2026-01-03", "resting_hr": 66},
    ]
    rec = insights.analyse_recovery(metrics, {"sleep_hours": 8.0})
    assert rec.resting_hr_trend == "rising"
    assert rec.status == "poor"


def test_recovery_unknown_without_data():
    rec = insights.analyse_recovery(None, None)
    assert rec.status == "unknown"


def test_build_insights_orders_problems_first():
    history = {
        "Bench": _sets((100, 8), (100, 8), (100, 8), (100, 8)),       # stalling
        "Deadlift": _sets((100, 8), (105, 8), (110, 8), (115, 8)),    # progressing
        "Squat": _sets((110, 8), (107, 8), (104, 8), (100, 8)),       # regressing
    }
    review = insights.build_insights(history, [], {"sleep_hours": 7.5})
    trends = [lift.trend for lift in review.lifts]
    # Regressing and stalling must come before progressing.
    assert trends.index("regressing") < trends.index("progressing")
    assert trends.index("stalling") < trends.index("progressing")
    assert "regressing" in review.headline or "1 regressing" in review.headline
    assert review.as_text().startswith("Self-review:")


def test_priorities_returns_only_flagged_lifts():
    history = {
        "Bench": _sets((100, 8), (100, 8), (100, 8), (100, 8)),
        "Deadlift": _sets((100, 8), (105, 8), (110, 8), (115, 8)),
    }
    review = insights.build_insights(history, [], None)
    names = {lift.name for lift in review.priorities()}
    assert "Bench" in names
    assert "Deadlift" not in names


def test_as_message_is_plain_text_with_sections():
    history = {
        "Bench": _sets((100, 8), (100, 8), (100, 8), (100, 8)),       # stalling
        "Deadlift": _sets((100, 8), (105, 8), (110, 8), (115, 8)),    # progressing
    }
    review = insights.build_insights(history, [], {"sleep_hours": 7.5})
    message = review.as_message(week=4, block_name="Hypertrophy")
    assert message.startswith("Weekly self-review - Week 4 (Hypertrophy)")
    assert "Progressing: Deadlift" in message
    assert "Needs attention:" in message
    assert "Bench" in message
    assert "Recovery status:" in message
    # Phone-friendly: no markdown and no em dash.
    assert "*" not in message
    assert "\u2014" not in message
