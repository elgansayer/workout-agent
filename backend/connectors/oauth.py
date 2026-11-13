"""Tenant-, session- and connector-bound OAuth correlation primitives.

An authorization request is made replay resistant by combining three things:

* ``state`` is an HMAC-signed envelope.  It names the connector, nonce,
  redirect URI and issue time, and carries *keyed binding tags* for the
  initiating account and session.  The tags are HMACs, never the raw user or
  session identifier, because ``state`` travels through the browser URL, the
  provider and access logs.  A state minted for another account, session or
  connector therefore fails verification instead of merely failing a lookup.
* the PKCE verifier is derived from the server secret and the state, so it is
  never stored or sent through the browser, and a code intercepted in transit
  cannot be redeemed without the secret.
* a server-side :class:`StateLedger` records each state the first time it is
  consumed.  A signed state alone is replayable until it expires, and session
  cookies are client-held, so a copy of an older cookie cannot be trusted to
  remember that a state was already spent.  The claim is atomic, so concurrent
  callbacks cannot both succeed.

Verification always runs before the claim, so only the owning account and
session can burn a state.  Nothing in this module persists a credential.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

STATE_TTL_SECONDS = 600
CLOCK_SKEW_SECONDS = 60
_MAX_STATE_LENGTH = 2048
_TAG_BYTES = 16

_PURPOSE_STATE = b"oauth-state-signature\0"
_PURPOSE_BINDING = b"oauth-state-binding\0"
_PURPOSE_PKCE = b"oauth-pkce-verifier\0"


class OAuthStateError(ValueError):
    """Base error raised when an OAuth state cannot be trusted."""


class OAuthStateExpired(OAuthStateError):
    """Raised when the correlation state has outlived its short TTL."""


class OAuthStateMismatch(OAuthStateError):
    """Raised on replay, mix-up, tenant or session binding failures."""


class RedirectUriError(ValueError):
    """Raised when a redirect URI is malformed or not exactly allow-listed."""


class StateLedger(Protocol):
    """Atomic, server-side record of consumed state (see ``webapp.oauth_state``)."""

    def claim(self, key: str, *, expires_at: int, now: int) -> bool:
        """Record *key*; return ``True`` for exactly one caller per live key."""


@dataclass(frozen=True, slots=True)
class OAuthState:
    user_id: str | int
    provider: str
    nonce: str
    session_id: str = ""
    redirect_uri: str = ""
    issued_at: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.user_id, int):
            valid_user = self.user_id > 0
        else:
            valid_user = bool(str(self.user_id).strip())
        if not valid_user or not self.provider.strip() or not self.nonce.strip():
            raise ValueError("OAuth state requires user, provider and nonce")


@dataclass(frozen=True, slots=True)
class OAuthAuthorization:
    """A freshly started authorization request."""

    state: str
    nonce: str
    code_verifier: str
    code_challenge: str
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class PendingOAuthState:
    """What a consumed state authorises the callback to complete."""

    nonce: str
    code_verifier: str
    redirect_uri: str
    issued_at: int


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _mac(secret: bytes, purpose: bytes, *parts: str) -> bytes:
    message = purpose + b"\0".join(part.encode("utf-8") for part in parts)
    return hmac.new(secret, message, hashlib.sha256).digest()


def _binding_tag(secret: bytes, kind: str, nonce: str, value: str) -> str:
    """Keyed, per-attempt tag standing in for an identifier inside ``state``."""

    return _b64encode(_mac(secret, _PURPOSE_BINDING, kind, nonce, value)[:_TAG_BYTES])


def _signature(body: str, secret: bytes) -> str:
    return _b64encode(_mac(secret, _PURPOSE_STATE, body))


def sign_state(state: OAuthState, secret: bytes, *, now: int | None = None) -> str:
    """Return a compact, signed representation of *state*.

    The payload carries the connector, nonce, redirect URI and issue time in
    clear (all are already visible in the authorization URL) plus binding tags
    for the account and session.  Neither identifier itself is included.
    """

    issued_at = state.issued_at or int(time.time() if now is None else now)
    payload = {
        "p": state.provider,
        "n": state.nonce,
        "r": state.redirect_uri,
        "t": issued_at,
        "u": _binding_tag(secret, "user", state.nonce, str(state.user_id)),
        "s": _binding_tag(secret, "session", state.nonce, state.session_id),
    }
    body = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{body}.{_signature(body, secret)}"


def verify_state(
    value: str,
    secret: bytes,
    *,
    expected_user_id: str | int,
    expected_provider: str,
    expected_session_id: str,
    max_age: int = STATE_TTL_SECONDS,
    now: int | None = None,
) -> OAuthState:
    """Verify signature, expiry and account/connector/session bindings.

    ``expected_session_id`` is required: pass ``""`` only when the caller truly
    has no session (the explicit local mode), never to skip the check.

    Raises :class:`OAuthStateExpired` for a stale state and
    :class:`OAuthStateMismatch` for a replay/mix-up attempt.
    """

    if not isinstance(value, str) or not value or len(value) > _MAX_STATE_LENGTH:
        raise OAuthStateError("invalid OAuth state")
    try:
        body, signature = value.split(".", 1)
        if not body or not signature:
            raise ValueError
        payload = json.loads(_b64decode(body).decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError
        provider = str(payload["p"])
        nonce = str(payload["n"])
        redirect_uri = str(payload.get("r") or "")
        issued_at = int(payload["t"])
        user_tag = str(payload["u"])
        session_tag = str(payload["s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OAuthStateError("invalid OAuth state") from exc

    try:
        valid_signature = hmac.compare_digest(signature, _signature(body, secret))
    except (TypeError, UnicodeEncodeError) as exc:
        raise OAuthStateError("invalid OAuth state signature") from exc
    if not valid_signature:
        raise OAuthStateError("invalid OAuth state signature")

    current = int(time.time() if now is None else now)
    if issued_at <= 0 or current - issued_at > max_age:
        raise OAuthStateExpired("OAuth state expired")
    if issued_at > current + CLOCK_SKEW_SECONDS:
        raise OAuthStateError("invalid OAuth state timestamp")
    if provider != expected_provider:
        raise OAuthStateMismatch("OAuth state connector mismatch")
    if not hmac.compare_digest(
        user_tag, _binding_tag(secret, "user", nonce, str(expected_user_id))
    ):
        raise OAuthStateMismatch("OAuth state account mismatch")
    if not hmac.compare_digest(
        session_tag, _binding_tag(secret, "session", nonce, expected_session_id)
    ):
        raise OAuthStateMismatch("OAuth state session mismatch")
    try:
        return OAuthState(
            user_id=expected_user_id,
            provider=provider,
            nonce=nonce,
            session_id=expected_session_id,
            redirect_uri=redirect_uri,
            issued_at=issued_at,
        )
    except ValueError as exc:
        raise OAuthStateError("invalid OAuth state") from exc


def generate_nonce() -> str:
    """Return a fresh unpredictable nonce."""

    return secrets.token_urlsafe(32)


def generate_code_verifier() -> str:
    """Return a random PKCE verifier within RFC 7636's 43-128 character range.

    For flows that keep the verifier in process (the one-time CLI helper).  The
    web flows use :func:`derive_code_verifier`, which needs no storage.
    """

    return secrets.token_urlsafe(64)


def derive_code_verifier(secret: bytes, state: str) -> str:
    """Return the PKCE verifier bound to *state*, derived from a server secret.

    The verifier is recomputed at the callback instead of being stored or put
    in a cookie, so it never leaves the server.  It is 43 characters from the
    RFC 7636 unreserved alphabet.
    """

    return _b64encode(_mac(secret, _PURPOSE_PKCE, state))


def pkce_challenge(verifier: str) -> str:
    """Return the S256 PKCE challenge for *verifier*."""

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64encode(digest)


def validate_redirect_uri(redirect_uri: str, allowed: Iterable[str]) -> str:
    """Require a safe absolute URI that exactly matches the allowlist.

    The comparison is a plain string equality: no prefix, suffix, case, scheme
    or host normalisation is applied, so a lookalike URI cannot match.
    """

    if not isinstance(redirect_uri, str) or not redirect_uri:
        raise RedirectUriError("redirect URI is required")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F or char == "\\" for char in redirect_uri):
        raise RedirectUriError("redirect URI contains unsafe characters")
    try:
        parsed = urlsplit(redirect_uri)
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise RedirectUriError("redirect URI is not a safe absolute URI") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or has_credentials
        or parsed.fragment
    ):
        raise RedirectUriError("redirect URI is not a safe absolute URI")
    if redirect_uri not in frozenset(allowed):
        raise RedirectUriError("redirect URI is not allow-listed")
    return redirect_uri


def claim_key(provider: str, tenant: str | int | None, state: str) -> str:
    """Return the ledger key for *state*.

    The key digests the connector, tenant and state, so the ledger is
    tenant-scoped without storing any identifier, and the state itself never
    reaches the database.
    """

    material = "\0".join((provider, "" if tenant is None else str(tenant), state))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def begin_authorization(
    secret: bytes,
    *,
    user_id: str | int,
    provider: str,
    redirect_uri: str,
    session_id: str,
    now: int | None = None,
) -> OAuthAuthorization:
    """Create state, nonce and PKCE material for one authorization attempt.

    Attempts are independent: nothing is shared between them, so concurrent
    attempts by the same account (two tabs, a double click) never overwrite one
    another, and none of the material is stored anywhere.
    """

    current = int(time.time() if now is None else now)
    nonce = generate_nonce()
    state = sign_state(
        OAuthState(
            user_id=user_id,
            provider=provider,
            nonce=nonce,
            session_id=session_id,
            redirect_uri=redirect_uri,
            issued_at=current,
        ),
        secret,
    )
    verifier = derive_code_verifier(secret, state)
    return OAuthAuthorization(
        state=state,
        nonce=nonce,
        code_verifier=verifier,
        code_challenge=pkce_challenge(verifier),
        redirect_uri=redirect_uri,
    )


def consume_authorization(
    ledger: StateLedger,
    secret: bytes,
    value: str,
    *,
    expected_user_id: str | int,
    expected_provider: str,
    expected_session_id: str,
    max_age: int = STATE_TTL_SECONDS,
    now: int | None = None,
) -> PendingOAuthState:
    """Verify *value* and burn it in the ledger.

    The state is verified first, so only its owner can spend it.  The ledger
    claim is atomic: when two callbacks present the same state, one wins and
    the other raises :class:`OAuthStateMismatch`, even inside the TTL and even
    from an older copy of the initiating session.
    """

    current = int(time.time() if now is None else now)
    state = verify_state(
        value,
        secret,
        expected_user_id=expected_user_id,
        expected_provider=expected_provider,
        expected_session_id=expected_session_id,
        max_age=max_age,
        now=current,
    )
    claimed = ledger.claim(
        claim_key(expected_provider, expected_user_id, value),
        expires_at=state.issued_at + max_age + CLOCK_SKEW_SECONDS,
        now=current,
    )
    if not claimed:
        raise OAuthStateMismatch("OAuth state is unknown or already used")
    return PendingOAuthState(
        nonce=state.nonce,
        code_verifier=derive_code_verifier(secret, value),
        redirect_uri=state.redirect_uri,
        issued_at=state.issued_at,
    )


__all__ = [
    "CLOCK_SKEW_SECONDS",
    "STATE_TTL_SECONDS",
    "OAuthAuthorization",
    "OAuthState",
    "OAuthStateError",
    "OAuthStateExpired",
    "OAuthStateMismatch",
    "PendingOAuthState",
    "RedirectUriError",
    "StateLedger",
    "begin_authorization",
    "claim_key",
    "consume_authorization",
    "derive_code_verifier",
    "generate_code_verifier",
    "generate_nonce",
    "pkce_challenge",
    "sign_state",
    "validate_redirect_uri",
    "verify_state",
]
