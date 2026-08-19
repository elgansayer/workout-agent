"""Tenant-bound OAuth state primitives for health connectors."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OAuthState:
    user_id: int
    provider: str
    nonce: str

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.provider.strip() or not self.nonce.strip():
            raise ValueError("OAuth state requires user, provider and nonce")


def sign_state(state: OAuthState, secret: bytes) -> str:
    payload = f"{state.user_id}:{state.provider}:{state.nonce}"
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_state(value: str, secret: bytes, *, expected_user_id: int, expected_provider: str) -> OAuthState:
    try:
        user, provider, nonce, signature = value.split(":", 3)
        state = OAuthState(int(user), provider, nonce)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid OAuth state") from exc
    expected = sign_state(state, secret).rsplit(":", 1)[1]
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid OAuth state signature")
    if state.user_id != expected_user_id or state.provider != expected_provider:
        raise ValueError("OAuth state tenant/provider mismatch")
    return state
