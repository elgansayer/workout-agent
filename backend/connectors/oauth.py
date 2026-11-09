"""Replay-resistant OAuth 2.0 state, nonce and PKCE primitives.

The state value is a signed, versioned JSON document. The HMAC covers every
serialised field, so a caller cannot swap the tenant, connector, browser
session or redirect URI without invalidating the signature. The PKCE
``code_verifier`` is intentionally kept out of the state value and stored
server-side. Callers are expected to consume the nonce exactly once through a
shared store (see ``database.consume_meta`` in the web app) so a replayed
callback cannot be exchanged twice.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_STATE_TTL_SECONDS = 300
MAX_STATE_LENGTH = 8192
_CLOCK_SKEW_SECONDS = 30
_PAYLOAD_VERSION = 1
_PKCE_VERIFIER_BYTES = 64


class OAuthStateError(ValueError):
    """Raised when an OAuth correlation value is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class OAuthState:
    """Correlation data bound to a tenant, connector and browser session."""

    user_id: int | str
    provider: str
    nonce: str
    session_id: str = ""
    redirect_uri: str = ""
    issued_at: int = 0
    code_verifier: str | None = None

    def __post_init__(self) -> None:
        if not str(self.user_id).strip():
            raise OAuthStateError("OAuth state requires a user")
        if not self.provider.strip():
            raise OAuthStateError("OAuth state requires a provider")
        if not self.nonce.strip():
            raise OAuthStateError("OAuth state requires a nonce")


def generate_nonce() -> str:
    """Return an unguessable single-use correlation nonce."""

    return secrets.token_urlsafe(32)


def generate_code_verifier() -> str:
    """Return a PKCE code verifier within the RFC 7636 length bounds."""

    return _b64encode(secrets.token_bytes(_PKCE_VERIFIER_BYTES))


def code_challenge_for(verifier: str) -> str:
    """Return the S256 PKCE challenge for *verifier*."""

    if not verifier or len(verifier) < 43 or len(verifier) > 128:
        raise OAuthStateError("invalid PKCE code verifier")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64encode(digest)


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` using S256."""

    verifier = generate_code_verifier()
    return verifier, code_challenge_for(verifier)


def create_oauth_state(
    *,
    user_id: int | str,
    provider: str,
    session_id: str,
    redirect_uri: str,
    secret: bytes,
    with_pkce: bool = True,
    now: float | None = None,
    nonce: str | None = None,
) -> tuple[OAuthState, str]:
    """Build a fresh state value and its signed representation."""

    issued_at = int(now if now is not None else time.time())
    verifier = generate_code_verifier() if with_pkce else None
    state = OAuthState(
        user_id=str(user_id),
        provider=provider,
        nonce=nonce or generate_nonce(),
        session_id=session_id,
        redirect_uri=redirect_uri,
        issued_at=issued_at,
        code_verifier=verifier,
    )
    return state, sign_state(state, secret)


def sign_state(state: OAuthState, secret: bytes) -> str:
    """Serialise and sign *state* with HMAC-SHA256."""

    if not secret:
        raise OAuthStateError("OAuth state signing secret is required")
    payload = json.dumps(
        _state_payload(state),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    body = _b64encode(payload)
    signature = _b64encode(
        hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{body}.{signature}"


def verify_state(
    value: str | None,
    secret: bytes,
    *,
    expected_user_id: int | str,
    expected_provider: str,
    expected_session_id: str | None = None,
    expected_redirect_uri: str | None = None,
    max_age_seconds: int | None = None,
    now: float | None = None,
) -> OAuthState:
    """Verify a signed state and its tenant/provider/session/redirect bindings."""

    if not secret:
        raise OAuthStateError("OAuth state signing secret is required")
    if not isinstance(value, str) or not value or len(value) > MAX_STATE_LENGTH:
        raise OAuthStateError("invalid OAuth state")

    try:
        body, signature = value.split(".", 1)
    except ValueError as exc:
        raise OAuthStateError("invalid OAuth state") from exc

    try:
        expected_signature = _b64encode(
            hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected_signature):
            raise OAuthStateError("invalid OAuth state signature")
        payload = json.loads(_b64decode(body))
        state = _state_from_payload(payload)
    except (
        TypeError,
        ValueError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        if isinstance(exc, OAuthStateError):
            raise
        raise OAuthStateError("invalid OAuth state") from exc

    if str(state.user_id) != str(expected_user_id):
        raise OAuthStateError("OAuth state tenant/provider mismatch")
    if state.provider != expected_provider:
        raise OAuthStateError("OAuth state tenant/provider mismatch")
    if expected_session_id is not None and not hmac.compare_digest(
        state.session_id, expected_session_id
    ):
        raise OAuthStateError("OAuth state session mismatch")
    if expected_redirect_uri is not None and not hmac.compare_digest(
        state.redirect_uri, expected_redirect_uri
    ):
        raise OAuthStateError("OAuth state redirect mismatch")

    if max_age_seconds is not None:
        current = int(now if now is not None else time.time())
        if state.issued_at <= 0:
            raise OAuthStateError("expired OAuth state")
        age = current - state.issued_at
        if age > max_age_seconds:
            raise OAuthStateError("expired OAuth state")
        if age < -_CLOCK_SKEW_SECONDS:
            raise OAuthStateError("invalid OAuth state issue time")

    return state


def is_redirect_uri_allowed(candidate: str, allowlist: Iterable[str]) -> bool:
    """Return True only for an exact-match allowlisted redirect URI."""

    if not isinstance(candidate, str) or not candidate:
        return False
    if any(character in candidate for character in ("\r", "\n", "\t", " ")):
        return False
    return any(candidate == allowed for allowed in allowlist if allowed)


def require_redirect_uri(candidate: str, allowlist: Iterable[str]) -> str:
    """Return *candidate* when exact-match allowlisted, else fail closed."""

    if not is_redirect_uri_allowed(candidate, allowlist):
        raise OAuthStateError("redirect URI is not allowlisted")
    return candidate


def _state_payload(state: OAuthState) -> dict[str, object]:
    return {
        "v": _PAYLOAD_VERSION,
        "uid": state.user_id,
        "provider": state.provider,
        "nonce": state.nonce,
        "sid": state.session_id,
        "redirect_uri": state.redirect_uri,
        "iat": int(state.issued_at),
    }


def _state_from_payload(payload: object) -> OAuthState:
    if not isinstance(payload, dict):
        raise OAuthStateError("invalid OAuth state")
    if payload.get("v") != _PAYLOAD_VERSION:
        raise OAuthStateError("unsupported OAuth state version")
    uid = payload.get("uid")
    if isinstance(uid, bool) or not isinstance(uid, (int, str)):
        raise OAuthStateError("invalid OAuth state")
    try:
        return OAuthState(
            user_id=payload["uid"],
            provider=payload["provider"],
            nonce=payload["nonce"],
            session_id=payload["sid"],
            redirect_uri=payload["redirect_uri"],
            issued_at=int(payload["iat"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, OAuthStateError):
            raise
        raise OAuthStateError("invalid OAuth state") from exc


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
