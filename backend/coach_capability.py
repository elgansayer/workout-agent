"""Server-derived capability state for the Coach surface.

The Coach may only claim access to data the authenticated user actually has.
This module keeps that decision deterministic and free of provider or database
side effects so it can be tested across every capability combination.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from health_freshness import Freshness, classify_freshness

TRAINING_SOURCES = ("training_logs", "progress_history", "personal_records")
COACH_SOURCES = (*TRAINING_SOURCES, "body_metrics")

SOURCE_LABELS = {
    "training_logs": "training logs",
    "progress_history": "exercise progress",
    "personal_records": "personal records",
    "body_metrics": "body composition",
    "programme": "active programme",
}


class CoachStatus(StrEnum):
    READY = "ready"
    STALE_DATA = "stale_data"
    NO_DATA = "no_data"
    PROVIDER_MISSING = "provider_missing"
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"


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

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_name": self.provider_name,
            "provider_configured": self.provider_configured,
            "provider_state": self.provider_state,
            "sync_configured": self.sync_configured,
            "sync_state": self.sync_state,
            "sources": {
                name: source.as_dict() for name, source in self.sources.items()
            },
            "programme_available": self.programme_available,
            "has_training_data": self.has_training_data,
            "has_fresh_training_data": self.has_fresh_training_data,
            "can_chat": self.can_chat,
            "status": self.status.value,
        }


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _derive_status(
    *,
    provider_configured: bool,
    has_training_data: bool,
    has_fresh_training_data: bool,
) -> CoachStatus:
    if not provider_configured and not has_training_data:
        return CoachStatus.UNCONFIGURED
    if not provider_configured:
        return CoachStatus.PROVIDER_MISSING
    if not has_training_data:
        return CoachStatus.NO_DATA
    if not has_fresh_training_data:
        return CoachStatus.STALE_DATA
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
) -> CoachCapabilityState:
    """Build a truthful Coach capability state from server-side facts."""

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

    if not provider_supported:
        provider_state = "unsupported"
    elif provider_configured:
        provider_state = "ready"
    else:
        provider_state = "missing_key"

    return CoachCapabilityState(
        provider_name=provider_name,
        provider_configured=provider_configured,
        provider_state=provider_state,
        sync_configured=sync_configured,
        sync_state=sync_state,
        sources=sources,
        programme_available=programme_available,
        has_training_data=has_training_data,
        has_fresh_training_data=has_fresh_training_data,
        can_chat=provider_configured,
        status=_derive_status(
            provider_configured=provider_configured,
            has_training_data=has_training_data,
            has_fresh_training_data=has_fresh_training_data,
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
    lines.append(
        "Only reference sources marked available. Never infer, invent, or claim "
        "access to data that is unavailable."
    )
    return "\n".join(lines)
