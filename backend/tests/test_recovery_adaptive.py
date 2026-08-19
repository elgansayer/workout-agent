from adaptive_training import TrainingDecision, recommend_adjustment
from recovery import RecoveryObservation, compute_recovery_features


def history(days=14):
    return [RecoveryObservation(480, 60, 50, 100) for _ in range(days)]


def test_cold_start_requests_confirmation():
    features = compute_recovery_features(RecoveryObservation(sleep_minutes=420), [])
    adjustment = recommend_adjustment(features)
    assert adjustment.decision == TrainingDecision.CONFIRM


def test_good_recovery_proceeds():
    features = compute_recovery_features(RecoveryObservation(480, 60, 50, 100), history())
    adjustment = recommend_adjustment(features)
    assert adjustment.decision == TrainingDecision.PROCEED
    assert adjustment.volume_multiplier == 1.0


def test_poor_recovery_is_bounded():
    features = compute_recovery_features(RecoveryObservation(360, 70, 35, 150), history())
    adjustment = recommend_adjustment(features)
    assert adjustment.decision == TrainingDecision.RECOVERY_SESSION
    assert adjustment.volume_multiplier >= 0.6
    assert adjustment.intensity_multiplier >= 0.85
    assert len(adjustment.reasons) >= 3


def test_missing_hrv_does_not_invent_value():
    features = compute_recovery_features(RecoveryObservation(480, 60, None, 100), history())
    assert features.hrv_delta_pct is None
    assert recommend_adjustment(features).decision == TrainingDecision.PROCEED


def test_stale_data_requires_confirmation():
    features = compute_recovery_features(RecoveryObservation(480, 60, 50, 100), history())
    assert recommend_adjustment(features, stale=True).decision == TrainingDecision.CONFIRM
