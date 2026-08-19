from datetime import date

from adaptive_training import TrainingDecision
from health_daily_pipeline import build_daily_health_decision
from health_readiness import ReadinessBand
from recovery import RecoveryObservation


def test_daily_pipeline_proceeds_for_baseline_recovery_without_metrics():
    history = [RecoveryObservation(480, 60, 50, 100) for _ in range(14)]
    result = build_daily_health_decision(
        user_id=1,
        day=date(2026, 8, 19),
        metrics=[],
        current_recovery=RecoveryObservation(480, 60, 50, 100),
        recovery_history=history,
    )
    assert result.readiness.band == ReadinessBand.NORMAL
    assert result.adjustment.decision == TrainingDecision.PROCEED
