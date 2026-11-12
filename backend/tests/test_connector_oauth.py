from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from connectors.oauth import (
    STATE_TTL_SECONDS,
    OAuthAuthorization,
    OAuthState,
    OAuthStateExpired,
    OAuthStateMismatch,
    RedirectUriError,
    begin_authorization,
    consume_authorization,
    generate_code_verifier,
    pkce_challenge,
    sign_state,
    validate_redirect_uri,
    verify_state,
)
from webapp.oauth_state import SQLiteStateLedger

SECRET = b"synthetic-test-secret"
CALLBACK = "https://app.example.test/google-health/callback"


class _MemoryLedger:
    """Minimal StateLedger for exercising the primitives without SQLite."""

    def __init__(self) -> None:
        self._claims: dict[str, int] = {}

    def claim(self, key: str, *, expires_at: int, now: int) -> bool:
        if not key or expires_at <= now or key in self._claims:
            return False
        self._claims[key] = expires_at
        return True


def _authorization(*, provider: str = "google_health") -> OAuthAuthorization:
    return begin_authorization(
        SECRET,
        user_id="user-a",
        provider=provider,
        redirect_uri=CALLBACK,
        session_id="session-a",
        now=1_000,
    )


def test_oauth_state_round_trip_is_user_provider_and_session_bound():
    state = OAuthState(
        7, "oura", "nonce", session_id="session-a", issued_at=1_000
    )
    signed = sign_state(state, SECRET, now=1_000)
    assert (
        verify_state(
            signed,
            SECRET,
            expected_user_id=7,
            expected_provider="oura",
            expected_session_id="session-a",
            now=1_000,
        )
        == state
    )
    with pytest.raises(OAuthStateMismatch, match="account"):
        verify_state(
            signed,
            SECRET,
            expected_user_id=8,
            expected_provider="oura",
            expected_session_id="session-a",
            now=1_000,
        )
    with pytest.raises(OAuthStateMismatch, match="connector"):
        verify_state(
            signed,
            SECRET,
            expected_user_id=7,
            expected_provider="polar",
            expected_session_id="session-a",
            now=1_000,
        )
    with pytest.raises(OAuthStateMismatch, match="session"):
        verify_state(
            signed,
            SECRET,
            expected_user_id=7,
            expected_provider="oura",
            expected_session_id="session-b",
            now=1_000,
        )


def test_oauth_state_rejects_tampering():
    signed = sign_state(OAuthState(7, "oura", "nonce"), SECRET, now=1_000)
    body, signature = signed.rsplit(".", 1)
    replacement = "0" if signature[0] != "0" else "1"
    tampered = f"{body}.{replacement}{signature[1:]}"
    with pytest.raises(ValueError, match="signature"):
        verify_state(
            tampered,
            SECRET,
            expected_user_id=7,
            expected_provider="oura",
            expected_session_id="",
            now=1_000,
        )


def test_oauth_state_expires_quickly():
    signed = sign_state(OAuthState(7, "oura", "nonce"), SECRET, now=1_000)
    with pytest.raises(OAuthStateExpired, match="expired"):
        verify_state(
            signed,
            SECRET,
            expected_user_id=7,
            expected_provider="oura",
            expected_session_id="",
            now=1_000 + STATE_TTL_SECONDS + 1,
        )


def test_pending_state_is_single_use():
    ledger = _MemoryLedger()
    authorization = _authorization()

    pending = consume_authorization(
        ledger,
        SECRET,
        authorization.state,
        expected_user_id="user-a",
        expected_provider="google_health",
        expected_session_id="session-a",
        now=1_001,
    )

    assert pending.nonce == authorization.nonce
    assert pending.code_verifier == authorization.code_verifier
    assert pending.redirect_uri == CALLBACK
    with pytest.raises(OAuthStateMismatch, match="already used"):
        consume_authorization(
            ledger,
            SECRET,
            authorization.state,
            expected_user_id="user-a",
            expected_provider="google_health",
            expected_session_id="session-a",
            now=1_002,
        )


def test_pending_state_cannot_be_completed_in_another_session():
    authorization = _authorization()

    with pytest.raises(OAuthStateMismatch, match="session"):
        consume_authorization(
            _MemoryLedger(),
            SECRET,
            authorization.state,
            expected_user_id="user-a",
            expected_provider="google_health",
            expected_session_id="session-b",
            now=1_001,
        )


def test_pending_state_rejects_a_provider_mix_up():
    authorization = _authorization(provider="oura")

    with pytest.raises(OAuthStateMismatch, match="connector"):
        consume_authorization(
            _MemoryLedger(),
            SECRET,
            authorization.state,
            expected_user_id="user-a",
            expected_provider="google_health",
            expected_session_id="session-a",
            now=1_001,
        )


def test_concurrent_authorizations_are_independent():
    ledger = _MemoryLedger()
    first = _authorization()
    second = _authorization()
    assert first.nonce != second.nonce

    second_pending = consume_authorization(
        ledger,
        SECRET,
        second.state,
        expected_user_id="user-a",
        expected_provider="google_health",
        expected_session_id="session-a",
        now=1_001,
    )
    first_pending = consume_authorization(
        ledger,
        SECRET,
        first.state,
        expected_user_id="user-a",
        expected_provider="google_health",
        expected_session_id="session-a",
        now=1_002,
    )
    assert first_pending.code_verifier == first.code_verifier
    assert second_pending.code_verifier == second.code_verifier


def test_only_one_concurrent_callback_can_spend_a_state(tmp_path: Path):
    ledger = SQLiteStateLedger(str(tmp_path / "oauth-state.db"))
    # Create the table once so the concurrent claims only contend on the row.
    assert ledger.claim("warm-up", expires_at=10, now=0)
    authorization = _authorization()

    def attempt(_: int) -> bool:
        try:
            consume_authorization(
                ledger,
                SECRET,
                authorization.state,
                expected_user_id="user-a",
                expected_provider="google_health",
                expected_session_id="session-a",
                now=1_001,
            )
        except OAuthStateMismatch:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert results.count(True) == 1


def test_sqlite_ledger_does_not_claim_an_expired_key(tmp_path: Path):
    ledger = SQLiteStateLedger(str(tmp_path / "oauth-state.db"))
    assert not ledger.claim("key", expires_at=100, now=100)
    assert not ledger.claim("key", expires_at=99, now=100)


def test_pkce_challenge_is_s256():
    verifier = generate_code_verifier()
    assert 43 <= len(verifier) <= 128
    expected = hashlib.sha256(verifier.encode("ascii")).digest()
    assert pkce_challenge(verifier) == base64.urlsafe_b64encode(
        expected
    ).rstrip(b"=").decode("ascii")


@pytest.mark.parametrize(
    "candidate",
    [
        "https://app.example.test/google-health/callback/",
        "http://app.example.test/google-health/callback",
        "https://evil.example.test/google-health/callback",
        "https://app.example.test/other",
        "https://user@app.example.test/google-health/callback",
        "https://app.example.test/google-health/callback#fragment",
        "javascript:alert(1)",
        "/relative",
    ],
)
def test_redirect_uri_requires_an_exact_safe_match(candidate: str):
    with pytest.raises(RedirectUriError):
        validate_redirect_uri(candidate, {CALLBACK})


def test_exact_redirect_uri_is_accepted():
    assert validate_redirect_uri(CALLBACK, {CALLBACK}) == CALLBACK
