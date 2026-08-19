import pytest

from connectors.oauth import OAuthState, sign_state, verify_state


SECRET = b"synthetic-test-secret"


def test_oauth_state_round_trip_is_user_and_provider_bound():
    state = OAuthState(7, "oura", "nonce")
    signed = sign_state(state, SECRET)
    assert verify_state(signed, SECRET, expected_user_id=7, expected_provider="oura") == state
    with pytest.raises(ValueError, match="mismatch"):
        verify_state(signed, SECRET, expected_user_id=8, expected_provider="oura")


def test_oauth_state_rejects_tampering():
    signed = sign_state(OAuthState(7, "oura", "nonce"), SECRET)
    with pytest.raises(ValueError, match="signature"):
        verify_state(signed[:-1] + "0", SECRET, expected_user_id=7, expected_provider="oura")
