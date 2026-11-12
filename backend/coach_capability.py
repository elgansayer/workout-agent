"""Server-derived capability state for the Coach surface.

The Coach may only claim what the authenticated user actually has, and only
once it is verified: access to training data, and a working AI provider.  A
stored key is configuration, not proof; only a passing connection test makes a
provider ``verified``.  This module keeps that decision deterministic and free
of provider or database side effects so it can be tested across every
capability combination, and so the chat route can enforce exactly what the UI
shows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from health_freshness import Freshness, classify_freshness
from setup_state import ProviderState, derive_provider_state

TRAINING_SOURCES = ("training_logs", "progress_history", "personal_records")
COACH_SOURCES = (*TRAINING_SOURCES, "body_metrics")

SOURCE_LABELS = {
    "training_logs": "training logs",
    "progress_history": "exercise progress",
    "personal_records": "personal records",
    "body_metrics": "body composition",
    "programme": "active programme",
}

#: Readiness states in which the data Coach would read is being rebuilt.
_REBUILDING = frozenset({"connecting", "syncing"})

#: Provider states that still let Coach attempt an answer.  An untested key may
#: well work, so it is allowed but never *claimed* to work; a key whose test
#: failed is known not to, so Coach fails closed.
_CHAT_PROVIDER_STATES = frozenset({ProviderState.UNVERIFIED, ProviderState.VERIFIED})


class CoachStatus(StrEnum):
    READY = "ready"
    STALE_DATA = "stale_data"
    NO_DATA = "no_data"
    PROVIDER_MISSING = "provider_missing"
    PROVIDER_UNVERIFIED = "provider_unverified"
    PROVIDER_FAILED = "provider_failed"
    SYNCING = "syncing"
    DEGRADED = "degraded"
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"


#: What the chat route tells a caller it cannot serve, by status.  The UI shows
#: the same condition from the same state, so the two never disagree.
BLOCKED_MESSAGES = {
    CoachStatus.UNCONFIGURED: (
        "Coach has no AI provider and no training data yet. Add an AI provider "
        "key in Settings first."
    ),
    CoachStatus.PROVIDER_MISSING: (
        "Coach needs an AI provider before it can answer. Add a key in Settings."
    ),
    CoachStatus.PROVIDER_FAILED: (
        "Your AI provider's last connection test failed, so Coach is paused. "
        "Test it again or replace the key in Settings."
    ),
}


@dataclass(frozen=True, slots=True)
class SourceCapability:
    available: bool
    freshness: Freshness
    last_observed_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "freshness": self.freshness.value,
            "last_observed_at": self.last_observed_at,
        }


@dataclass(frozen=True, slots=True)
class CoachCapabilityState:
    provider_name: str | None
    provider_configured: bool
    provider_state: str
    sync_configured: bool
    sync_state: str
    sources: Mapping[str, SourceCapability]
    programme_available: bool
    has_training_data: bool
    has_fresh_training_data: bool
    can_chat: bool
    status: CoachStatus
    provider_verified: bool = False
    provider_source: str = "none"
    readiness: str | None = None
    verified_data: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_name": self.provider_name,
            "provider_configured": self.provider_configured,
            "provider_state": self.provider_state,
            "provider_verified": self.provider_verified,
            "provider_source": self.provider_source,
            "sync_configured": self.sync_configured,
            "sync_state": self.sync_state,
            "readiness": self.readiness,
            "sources": {
                name: source.as_dict() for name, source in self.sources.items()
            },
            "programme_available": self.programme_available,
            "has_training_data": self.has_training_data,
            "has_fresh_training_data": self.has_fresh_training_data,
            "verified_data": self.verified_data,
            "can_chat": self.can_chat,
            "status": self.status.value,
        }

    def blocked_message(self) -> str | None:
        """Why the chat route refuses this account, or ``None`` when it serves."""

        if self.can_chat:
            return None
        return BLOCKED_MESSAGES.get(
            self.status, "Coach is not available for this account yet."
        )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _derive_status(
    *,
    provider_state: ProviderState,
    has_training_data: bool,
    has_fresh_training_data: bool,
    readiness: str | None,
) -> CoachStatus:
    """Pick the single most important thing Coach has to say about itself.

    ``ready`` is reserved for the case where both facts are verified: the
    provider passed its connection test, and the account has current training
    data that is not being rebuilt or failing to refresh.
    """

    if provider_state in (ProviderState.UNSUPPORTED, ProviderState.MISSING_KEY):
        return (
            CoachStatus.PROVIDER_MISSING
            if has_training_data
            else CoachStatus.UNCONFIGURED
        )
    if provider_state is ProviderState.FAILED:
        return CoachStatus.PROVIDER_FAILED
    if readiness in _REBUILDING:
        return CoachStatus.SYNCING
    if not has_training_data:
        return CoachStatus.NO_DATA
    if readiness == "degraded":
        return CoachStatus.DEGRADED
    if not has_fresh_training_data:
        return CoachStatus.STALE_DATA
    if provider_state is ProviderState.UNVERIFIED:
        return CoachStatus.PROVIDER_UNVERIFIED
    return CoachStatus.READY


def derive_coach_capability(
    *,
    provider_name: str | None,
    provider_configured: bool,
    provider_supported: bool,
    sync_configured: bool,
    observed_at: Mapping[str, datetime | None],
    programme_available: bool,
    now: datetime,
    training_fresh_for: timedelta = timedelta(days=7),
    training_stale_after: timedelta = timedelta(days=21),
    provider_test_status: str | None = None,
    provider_source: str = "none",
    readiness: str | None = None,
) -> CoachCapabilityState:
    """Build a truthful Coach capability state from server-side facts.

    ``provider_test_status`` is the outcome of the last connection test of the
    account's own stored credential (``"ok"`` or a failure code); ``None``
    means it was never tested.  ``readiness`` is the account's readiness state
    from :mod:`setup_state`, when the caller has it.
    """

    sources: dict[str, SourceCapability] = {}
    for name in COACH_SOURCES:
        observed = _utc(observed_at.get(name))
        freshness = classify_freshness(
            observed,
            now=now,
            fresh_for=training_fresh_for,
            stale_after=training_stale_after,
        )
        sources[name] = SourceCapability(
            available=observed is not None,
            freshness=freshness,
            last_observed_at=observed.isoformat() if observed is not None else None,
        )

    has_training_data = any(sources[name].available for name in TRAINING_SOURCES)
    has_fresh_training_data = any(
        sources[name].available and sources[name].freshness is Freshness.FRESH
        for name in TRAINING_SOURCES
    )

    if not sync_configured:
        sync_state = "not_configured"
    elif has_training_data:
        sync_state = "synced"
    else:
        sync_state = "never_synced"

    provider_state = derive_provider_state(
        supported=provider_supported,
        configured=provider_configured,
        last_test_status=provider_test_status,
    )

    return CoachCapabilityState(
        provider_name=provider_name,
        provider_configured=provider_configured,
        provider_state=provider_state.value,
        provider_verified=provider_state is ProviderState.VERIFIED,
        provider_source=provider_source,
        sync_configured=sync_configured,
        sync_state=sync_state,
        readiness=readiness,
        sources=sources,
        programme_available=programme_available,
        has_training_data=has_training_data,
        has_fresh_training_data=has_fresh_training_data,
        verified_data=has_training_data and readiness not in _REBUILDING,
        can_chat=provider_state in _CHAT_PROVIDER_STATES,
        status=_derive_status(
            provider_state=provider_state,
            has_training_data=has_training_data,
            has_fresh_training_data=has_fresh_training_data,
            readiness=readiness,
        ),
    )


def capability_prompt_block(state: CoachCapabilityState) -> str:
    """Return a prompt section that only claims the data the user actually has."""

    lines = ["Server-verified data access for this user:"]
    for name in COACH_SOURCES:
        source = state.sources[name]
        if source.available:
            detail = (
                f"available ({source.freshness.value}; last observed "
                f"{source.last_observed_at})"
            )
        else:
            detail = "unavailable"
        lines.append(f"- {SOURCE_LABELS[name]}: {detail}")
    lines.append(
        "- active programme: "
        + ("available" if state.programme_available else "unavailable")
    )
    lines.append(f"- data sync: {state.sync_state}")
    if state.readiness:
        lines.append(f"- data readiness: {state.readiness}")
    if not state.has_training_data:
        lines.append(
            "No training data is available. Do not claim access to training logs, "
            "records, or history."
        )
    if not any(source.available for source in state.sources.values()) and not (
        state.programme_available
    ):
        lines.append(
            "No user data of any kind is available. Do not claim access to logs, "
            "metrics, records, or history, and say clearly that the data has not "
            "been synced."
        )
    if state.readiness in _REBUILDING:
        lines.append(
            "A Hevy sync is running, so the data above may be incomplete. Say so "
            "instead of drawing conclusions from partial history."
        )
    elif state.readiness in ("degraded", "stale"):
        lines.append(
            "The latest Hevy sync did not complete or the data is out of date. Tell "
            "the user the figures come from their last successful sync."
        )
    lines.append(
        "Only reference sources marked available. Never infer, invent, or claim "
        "access to data that is unavailable."
    )
    return "\n".join(lines)
