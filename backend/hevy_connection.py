"""Verify Hevy credentials before they are stored or activated.

The settings form used to accept any string as a Hevy API key, so a typo or a
revoked key was stored and silently activated: every later sync failed without
the user ever learning that the credential was the problem.

This module is the gate in front of storage. A candidate credential is
exercised against the Hevy API with a small, bounded set of requests that
prove two things:

* the key is accepted and identifies the connected account, and
* the key carries the minimum access the product needs (read one logged
  workout).

Everything after that is optional capability metadata. Only safe metadata is
returned to callers: the connected account identifier, the test time, the API
version and a capability map. The credential itself is never logged, echoed or
persisted by this module.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from hevy_client import (
    API_VERSION,
    CREDENTIAL_TEST_TIMEOUT,
    HevyProbe,
    is_transmittable_credential,
    probe,
)

logger = logging.getLogger(__name__)

# The product cannot do anything useful without reading logged workouts, so
# read access to that collection is the minimum required capability.
REQUIRED_CAPABILITY = "workouts"

# Optional capabilities the product uses when the account exposes them.
_OPTIONAL_CAPABILITY_PATHS: tuple[tuple[str, str], ...] = (
    ("routines", "routines"),
    ("exercise_templates", "exercise_templates"),
    ("workout_count", "workouts/count"),
)


class HevyCredentialState(StrEnum):
    """Outcome of a credential verification."""

    CONNECTED = "connected"
    INVALID = "invalid"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class HevyCredentialTest:
    """Safe result of verifying a Hevy credential."""

    state: HevyCredentialState
    tested_at: datetime
    api_version: str = API_VERSION
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    account_username: str | None = None
    account_id: str | None = None
    workout_count: int | None = None
    detail: str = ""

    @property
    def connected(self) -> bool:
        """True when the credential was proven usable."""
        return self.state is HevyCredentialState.CONNECTED

    def as_public_metadata(self) -> dict[str, Any]:
        """Return tenant-facing metadata; contains no credential material."""
        return {
            "state": self.state.value,
            "account_username": self.account_username,
            "account_id": self.account_id,
            "tested_at": self.tested_at.isoformat(),
            "api_version": self.api_version,
            "capabilities": dict(self.capabilities),
        }


def _bounded_page_params() -> dict[str, Any]:
    """Page parameters that ask Hevy for a single item."""
    return {"page": 1, "pageSize": 1}


def _resolve_api_version(*probes: HevyProbe) -> str:
    """Return the API version Hevy reported, preferring an explicit one."""
    for result in probes:
        if result.api_version and result.api_version != API_VERSION:
            return result.api_version
    return API_VERSION


def _account_username(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("username")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _account_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("id")
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return None


def _workout_count(payload: Mapping[str, Any] | None) -> int | None:
    if not payload:
        return None
    value = payload.get("workout_count")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _optional_capability(
    api_key: str,
    path: str,
    *,
    timeout: int,
) -> tuple[bool, HevyProbe]:
    """Probe one optional capability with a single bounded request."""
    params = None if path.endswith("/count") else _bounded_page_params()
    result = probe(api_key, path, params=params, timeout=timeout)
    return result.ok, result


def verify_hevy_credentials(
    api_key: str,
    *,
    timeout: int = CREDENTIAL_TEST_TIMEOUT,
) -> HevyCredentialTest:
    """Verify a candidate Hevy credential with bounded requests.

    The credential is never logged. The returned record contains only safe
    metadata about the connected account and the verified capabilities.
    """
    tested_at = datetime.now(timezone.utc)
    candidate = api_key.strip()
    if not candidate:
        return HevyCredentialTest(
            state=HevyCredentialState.INVALID,
            tested_at=tested_at,
            api_version=API_VERSION,
            detail="No Hevy API key was supplied.",
        )
    if not is_transmittable_credential(candidate):
        # The key cannot be placed in an HTTP header, so it can never
        # authenticate. Reject it without transmitting it.
        logger.warning("Rejected a Hevy API key that cannot be sent to Hevy.")
        return HevyCredentialTest(
            state=HevyCredentialState.INVALID,
            tested_at=tested_at,
            api_version=API_VERSION,
            detail="That API key contains characters Hevy cannot accept.",
        )

    identity = probe(candidate, "user/info", timeout=timeout)
    if identity.rejected:
        logger.warning("Hevy rejected the supplied API key.")
        return HevyCredentialTest(
            state=HevyCredentialState.INVALID,
            tested_at=tested_at,
            api_version=identity.api_version,
            detail="Hevy rejected this API key. Generate a new key and try again.",
        )
    if not identity.ok or identity.payload is None:
        logger.warning("Could not confirm the Hevy account for the supplied key.")
        return HevyCredentialTest(
            state=HevyCredentialState.UNVERIFIED,
            tested_at=tested_at,
            api_version=identity.api_version,
            detail="Could not reach Hevy to confirm this API key. Try again shortly.",
        )

    payload = identity.payload
    account_username = _account_username(payload)
    account_id = _account_id(payload)

    access = probe(
        candidate,
        "workouts",
        params=_bounded_page_params(),
        timeout=timeout,
    )
    if access.status_code == 401:
        logger.warning("Hevy rejected the supplied API key while reading workouts.")
        return HevyCredentialTest(
            state=HevyCredentialState.INVALID,
            tested_at=tested_at,
            api_version=access.api_version,
            account_username=account_username,
            account_id=account_id,
            detail="Hevy rejected this API key. Generate a new key and try again.",
        )
    if access.status_code == 403:
        logger.warning("Hevy denied workout access for the supplied API key.")
        return HevyCredentialTest(
            state=HevyCredentialState.INSUFFICIENT_SCOPE,
            tested_at=tested_at,
            api_version=access.api_version,
            account_username=account_username,
            account_id=account_id,
            detail=(
                "This API key cannot read your Hevy workouts. "
                "Check that your Hevy subscription allows API access."
            ),
        )
    if not access.ok:
        logger.warning("Could not confirm Hevy workout access for the supplied key.")
        return HevyCredentialTest(
            state=HevyCredentialState.UNVERIFIED,
            tested_at=tested_at,
            api_version=access.api_version,
            account_username=account_username,
            account_id=account_id,
            detail="Could not reach Hevy to confirm this API key. Try again shortly.",
        )

    capabilities: dict[str, bool] = {REQUIRED_CAPABILITY: True}
    workout_count: int | None = None
    api_version = _resolve_api_version(identity, access)
    for capability, path in _OPTIONAL_CAPABILITY_PATHS:
        available, result = _optional_capability(
            candidate,
            path,
            timeout=timeout,
        )
        capabilities[capability] = available
        if capability == "workout_count":
            workout_count = _workout_count(result.payload)

    return HevyCredentialTest(
        state=HevyCredentialState.CONNECTED,
        tested_at=tested_at,
        api_version=api_version,
        capabilities=capabilities,
        account_username=account_username,
        account_id=account_id,
        workout_count=workout_count,
        detail="Hevy API key verified.",
    )
