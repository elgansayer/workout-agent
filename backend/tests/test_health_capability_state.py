from health_capability_state import derive_capability_state
from health_connection import HealthConnection, HealthConnectionState


def test_capability_state_does_not_claim_data_for_disconnected_provider():
    state = derive_capability_state([HealthConnection("c1", 1, "oura", HealthConnectionState.DISCONNECTED, frozenset({"sleep"}))], fresh_connection_ids={"c1"})
    assert not state.has_connected_source
    assert not state.has_fresh_data
    assert state.capabilities == frozenset()


def test_connected_fresh_capabilities_are_derived_from_server_state():
    state = derive_capability_state([HealthConnection("c1", 1, "oura", HealthConnectionState.CONNECTED, frozenset({"sleep", "hrv"}))], fresh_connection_ids={"c1"})
    assert state.has_connected_source and state.has_fresh_data
    assert state.capabilities == frozenset({"sleep", "hrv"})
