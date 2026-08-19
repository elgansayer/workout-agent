from health_capability_state import HealthCapabilityState
from health_status_summary import setup_summary


def test_setup_summary_never_claims_health_data_without_fresh_source():
    assert not setup_summary(HealthCapabilityState(False, False, frozenset())).ready
    assert "fresh" in setup_summary(HealthCapabilityState(True, False, frozenset({"sleep"}))).message.lower()
    assert setup_summary(HealthCapabilityState(True, True, frozenset({"sleep"}))).ready
