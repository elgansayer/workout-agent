from health_readiness import ReadinessBand, derive_readiness
from recovery import RecoveryFeatures


def features(**overrides):
    values = dict(sleep_delta_minutes=0, resting_hr_delta_bpm=0, hrv_delta_pct=0, training_load_delta_pct=0, history_days=14, confidence=1.0)
    values.update(overrides)
    return RecoveryFeatures(**values)


def test_readiness_is_unknown_without_baseline_confidence():
    assert derive_readiness(features(confidence=0.1)).band == ReadinessBand.UNKNOWN


def test_readiness_low_requires_explainable_multiple_signals():
    state = derive_readiness(features(sleep_delta_minutes=-120, resting_hr_delta_bpm=9))
    assert state.band == ReadinessBand.LOW
    assert len(state.reasons) == 2
