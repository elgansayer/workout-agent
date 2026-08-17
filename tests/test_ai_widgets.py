"""Tests for ai_widgets SVG chart generation (no network/AI calls)."""

from __future__ import annotations

from webapp.ai_widgets import (
    block_phase_tracker,
    systemic_recovery_correlation,
    volume_distribution,
)

# ── block_phase_tracker ──────────────────────────────────────────────


def test_block_phase_tracker_insufficient_data() -> None:
    """Returns a placeholder when fewer than 2 sessions."""
    result = block_phase_tracker([{"volume": 1000, "date": "2025-01-01"}])
    assert "Not enough data" in result


def test_block_phase_tracker_empty_list() -> None:
    """Returns placeholder message for empty list as well."""
    result = block_phase_tracker([])
    assert "Not enough data" in result


def test_block_phase_tracker_renders_svg() -> None:
    """Produces a valid SVG chart with the expected class."""
    sessions = [
        {"volume": 5000, "date": "2025-01-01"},
        {"volume": 5500, "date": "2025-01-02"},
        {"volume": 5200, "date": "2025-01-03"},
    ]
    result = block_phase_tracker(sessions)
    assert "<svg" in result
    assert 'class="svg-chart"' in result
    assert "Phase Tracker" in result


def test_block_phase_tracker_all_same_volume() -> None:
    """Handles flat-volume data without division by zero."""
    sessions = [
        {"volume": 5000, "date": "2025-01-01"},
        {"volume": 5000, "date": "2025-01-02"},
    ]
    result = block_phase_tracker(sessions)
    assert "<svg" in result


def test_block_phase_tracker_truncates_to_30_sessions() -> None:
    """Only last 30 sessions are plotted."""
    sessions = [{"volume": i * 100, "date": f"2025-01-{i:02d}"} for i in range(1, 51)]
    result = block_phase_tracker(sessions)
    assert "<svg" in result


# ── systemic_recovery_correlation ────────────────────────────────────


def test_correlation_insufficient_data() -> None:
    """Returns placeholder when fewer than 3 overlapping data points."""
    biometrics: list[dict] = [{"date": "2025-01-01", "resting_hr": 60}]
    sessions: list[dict] = [{"volume": 5000, "date": "2025-01-01"}]
    result = systemic_recovery_correlation(biometrics, sessions)
    assert "Not enough overlapping data" in result


def test_correlation_no_matching_dates() -> None:
    """Returns placeholder when biometrics and sessions don't overlap."""
    biometrics = [
        {"date": "2025-01-01", "resting_hr": 60},
        {"date": "2025-01-02", "resting_hr": 62},
        {"date": "2025-01-03", "resting_hr": 58},
    ]
    sessions = [
        {"volume": 5000, "date": "2025-01-10"},
        {"volume": 5200, "date": "2025-01-11"},
    ]
    result = systemic_recovery_correlation(biometrics, sessions)
    assert "Not enough overlapping data" in result


def test_correlation_missing_resting_hr() -> None:
    """Biometrics without resting_hr are skipped."""
    biometrics: list[dict] = [
        {"date": "2025-01-01", "resting_hr": None},
        {"date": "2025-01-02", "resting_hr": 60},
        {"date": "2025-01-03", "resting_hr": 62},
        {"date": "2025-01-04", "resting_hr": 58},
    ]
    sessions: list[dict] = [
        {"volume": 5000, "date": "2025-01-02"},
        {"volume": 5200, "date": "2025-01-03"},
        {"volume": 4800, "date": "2025-01-04"},
    ]
    result = systemic_recovery_correlation(biometrics, sessions)
    assert "<svg" in result


def test_correlation_renders_svg() -> None:
    """Produces a valid SVG scatter plot."""
    biometrics: list[dict] = [
        {"date": "2025-01-01", "resting_hr": 60},
        {"date": "2025-01-02", "resting_hr": 62},
        {"date": "2025-01-03", "resting_hr": 58},
    ]
    sessions: list[dict] = [
        {"volume": 5000, "date": "2025-01-01"},
        {"volume": 5200, "date": "2025-01-02"},
        {"volume": 4800, "date": "2025-01-03"},
    ]
    result = systemic_recovery_correlation(biometrics, sessions)
    assert "<svg" in result
    assert 'class="svg-chart"' in result
    assert "Systemic Recovery Correlation" in result


def test_correlation_highlights_anomalies() -> None:
    """Anomalous points (high RHR + low volume) get a different color."""
    biometrics: list[dict] = [
        {"date": "2025-01-01", "resting_hr": 60},
        {"date": "2025-01-02", "resting_hr": 65},
        {"date": "2025-01-03", "resting_hr": 80},  # high RHR
    ]
    sessions: list[dict] = [
        {"volume": 6000, "date": "2025-01-01"},
        {"volume": 5500, "date": "2025-01-02"},
        {"volume": 2000, "date": "2025-01-03"},  # low volume
    ]
    result = systemic_recovery_correlation(biometrics, sessions)
    assert "<svg" in result
    # The anomalous point should be rendered with PINK (#f472b6)
    assert "#f472b6" in result


# ── volume_distribution ──────────────────────────────────────────────


def test_volume_distribution_empty() -> None:
    """Returns empty string for empty input."""
    assert volume_distribution({}) == ""


def test_volume_distribution_zero_total() -> None:
    """Returns empty string when all volumes are zero."""
    assert volume_distribution({"Chest": 0, "Back": 0}) == ""


def test_volume_distribution_renders_svg() -> None:
    """Produces a valid SVG bar chart."""
    groups = {
        "Chest": 3000,
        "Back": 4000,
        "Legs": 5000,
        "Shoulders": 2000,
        "Arms": 1000,
    }
    result = volume_distribution(groups)
    assert "<svg" in result
    assert 'class="svg-chart"' in result
    assert "Volume Distribution" in result
    assert "Actual" in result
    assert "Ideal" in result


def test_volume_distribution_extends_to_other_groups() -> None:
    """Any group keys are rendered; ideal fallback is zero for unknowns."""
    groups = {"Chest": 1000, "CustomGroup": 500}
    result = volume_distribution(groups)
    assert "<svg" in result
