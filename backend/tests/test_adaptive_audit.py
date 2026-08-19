from datetime import datetime, timezone

from adaptive_audit import AdaptiveDecisionRecord
from adaptive_training import TrainingAdjustment, TrainingDecision


def test_adaptive_decision_records_rule_and_reasons():
    adjustment = TrainingAdjustment(TrainingDecision.REDUCE_VOLUME, 0.75, 0.95, 0.8, ("poor sleep",))
    record = AdaptiveDecisionRecord(1, datetime(2026, 8, 19, tzinfo=timezone.utc), adjustment, "summary-1")
    assert record.adjustment.rule_version == 1
    assert record.adjustment.reasons == ("poor sleep",)
