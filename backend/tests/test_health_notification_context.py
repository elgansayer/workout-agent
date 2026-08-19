from adaptive_training import TrainingAdjustment, TrainingDecision
from health_notification_context import notification_context
from health_readiness import ReadinessBand, ReadinessState


def test_notification_context_uses_derived_state_not_raw_metrics():
    context = notification_context(ReadinessState(ReadinessBand.CAUTION, 0.8, ("reason",)), TrainingAdjustment(TrainingDecision.REDUCE_VOLUME, 0.75, 0.95, 0.8, ("reason",)))
    assert context.readiness_band == "caution"
    assert not hasattr(context, "heart_rate")
    assert not hasattr(context, "weight")
