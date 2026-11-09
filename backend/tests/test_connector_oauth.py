from __future__ import annotations

import base64
import json

import pytest
from connectors.oauth import (
    OAuthState,
    OAuthStateError,
    code_challenge_for,
    create_oauth_state,
    generate_pkce_pair,
    is_redirect_uri_allowed,
    require_redirect_uri,
    sign_state,
    verify_state,
)

SECRET = b"synthetic-test-secret"
NOW = 1_700_000_000
REDIRECT = "https://app.example.test/google-health/callback"
SESSION = "session-abc"


def _signed(**overrides: object) -> tuple[OAuthState, str]:
    values: dict[str, object] = {
        "user_id": "user-7",
        "provider": "oura",
        "session_id": SESSION,
        "redirect_uri": REDIRECT,
        "secret": SECRET,
        "now": NOW,
    }
    values.update(overrides)
    return create_oauth_state(**values)  # type: ignore[arg-type]


def test_oauth_state_round_trip_is_tenant_provider_session_and_redirect_bound():
    state, signed = _signed()
    verified = verify_state(
        signed,
        SECRET,
        expected_user_id="user-7",
        expected_provider="oura",
        expected_session_id=SESSION,
        expected_redirect_uri=REDIRECT,
        max_age_seconds=300,
        now=NOW,
    )
    assert verified.user_id == "user-7"
    assert verified.provider == "oura"
    assert verified.nonce == state.nonce
    assert verified.session_id == SESSION
    assert verified.redirect_uri == REDIRECT


def test_legacy_three_field_state_round_trips_without_optional_bindings():
    state = OAuthState(7, "oura", "nonce")
    signed = sign_state(state, SECRET)
    assert (
        verify_state(
            signed,
            SECRET,
            expected_user_id=7,
            expected_provider="oura",
        )
        == state
    )


def test_oauth_state_rejects_tampering():
    _, signed = _signed()
    with pytest.raises(OAuthStateError, match="signature"):
        verify_state(
            signed[:-1] + ("0" if signed[-1] != "0" else "1"),
            SECRET,
            expected_user_id="user-7",
            expected_provider="oura",
        )


def test_oauth_state_rejects_tenant_mix_up():
    _, signed = _signed()
    with pytest.raises(OAuthStateError, match="mismatch"):
        verify_state(
            signed,
            SECRET,
            expected_user_id="other-user",
            expected_provider="oura",
        )


def test_oauth_state_rejects_provider_mix_up():
    _, signed = _signed()
    with pytest.raises(OAuthStateError, match="mismatch"):
        verify_state(
            signed,
            SECRET,
            expected_user_id="user-7",
            expected_provider="google_health",
        )


def test_oauth_state_rejects_login_csrf_from_another_session():
    _, signed = _signed()
    with pytest.raises(OAuthStateError, match="session"):
        verify_state(
            signed,
            SECRET,
            expected_user_id="user-7",
            expected_provider="oura",
            expected_session_id="attacker-session",
        )


def test_oauth_state_rejects_open_redirect_mismatch():
    _, signed = _signed()
    with pytest.raises(OAuthStateError, match="redirect"):
        verify_state(
            signed,
            SECRET,
            expected_user_id="user-7",
            expected_provider="oura",
            expected_redirect_uri="https://evil.example/callback",
        )


def test_oauth_state_expires_quickly():
    _, signed = _signed()
    with pytest.raises(OAuthStateError, match="expired"):
        verify_state(
            signed,
            SECRET,
            expected_user_id="user-7",
            expected_provider="oura",
            max_age_seconds=300,
            now=NOW + 301,
        )
    assert verify_state(
        signed,
        SECRET,
        expected_user_id="user-7",
        expected_provider="oura",
        max_age_seconds=300,
        now=NOW + 299,
    )


def test_oauth_state_rejects_future_issue_time():
    _, signed = _signed(now=NOW + 120)
    with pytest.raises(OAuthStateError, match="issue time"):
        verify_state(
            signed,
            SECRET,
            expected_user_id="user-7",
            expected_provider="oura",
            max_age_seconds=300,
            now=NOW,
        )


def test_oauth_state_code_verifier_is_not_exposed_in_signed_state():
    state, signed = _signed()
    assert state.code_verifier is not None
    body = signed.split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert "cv" not in payload
    assert state.code_verifier not in signed


def test_pkce_s256_matches_rfc7636_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    generated, challenge = generate_pkce_pair()
    assert 43 <= len(generated) <= 128
    assert code_challenge_for(generated) == challenge


def test_redirect_uri_allowlist_is_exact_match_and_fails_closed():
    allowlist = (REDIRECT,)
    assert is_redirect_uri_allowed(REDIRECT, allowlist)
    for candidate in (
        "https://app.example.test/google-health/callback/",
        "https://app.example.test/google-health/callback?next=https://evil.example",
        "https://app.example.test.evil.example/google-health/callback",
        "https://app.example.test/google-health/../callback",
        "http://app.example.test/google-health/callback",
        "javascript:alert(1)",
    ):
        assert not is_redirect_uri_allowed(candidate, allowlist)
        with pytest.raises(OAuthStateError, match="allowlisted"):
            require_redirect_uri(candidate, allowlist)


def test_concurrent_connection_attempts_have_independent_states():
    first, first_signed = _signed()
    second, second_signed = _signed()
    assert first.nonce != second.nonce
    assert first_signed != second_signed

    consumed = {first.nonce}
    assert first.nonce in consumed
    second_verified = verify_state(
        second_signed,
        SECRET,
        expected_user_id="user-7",
        expected_provider="oura",
        expected_session_id=SESSION,
        expected_redirect_uri=REDIRECT,
        max_age_seconds=300,
        now=NOW,
    )
    assert second_verified.nonce == second.nonce
    assert second.nonce not in consumed


def test_state_rejects_empty_secret_and_malformed_values():
    state, signed = _signed()
    with pytest.raises(OAuthStateError, match="secret"):
        sign_state(state, b"")
    with pytest.raises(OAuthStateError, match="secret"):
        verify_state(signed, b"", expected_user_id="user-7", expected_provider="oura")
    for value in ("", None, "not-a-state", "a.b.c", "x" * 9000):
        with pytest.raises(OAuthStateError):
            verify_state(
                value,  # type: ignore[arg-type]
                SECRET,
                expected_user_id="user-7",
                expected_provider="oura",
            )
