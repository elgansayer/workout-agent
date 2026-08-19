from adaptive_training import TrainingAdjustment, TrainingDecision
from health_coaching_context import coaching_health_context
from health_readiness import ReadinessBand, ReadinessState


def test_coaching_context_contains_derived_explainable_signals_only():
    readiness = ReadinessState(ReadinessBand.CAUTION, 0.8, ("sleep below baseline",))
    adjustment = TrainingAdjustment(TrainingDecision.REDUCE_VOLUME, 0.75, 0.95, 0.8, ("sleep below baseline",))
    context = coaching_health_context(readiness, adjustment)
    assert context["readiness"]["band"] == "caution"
    assert "raw_payload" not in context
    assert "access_token" not in str(context)
