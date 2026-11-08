"""Truthful product readiness derived from tenant-scoped capability facts.

The product must never claim a connected data source, fresh training data or a
working AI provider when the underlying state is absent, stale or degraded.
This module is a pure projection: callers gather the authenticated user's facts
and the result explains exactly which capabilities are available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from health_freshness import Freshness, classify_freshness

FRESH_FOR = timedelta(hours=24)
STALE_AFTER = timedelta(days=7)


class ReadinessPhase(StrEnum):
    SETUP_REQUIRED = "setup_required"
    CONNECTING = "connecting"
    SYNCING = "syncing"
    READY = "ready"
    STALE = "stale"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class Capability:
    available: bool
    verified: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SetupStep:
    key: str
    label: str
    complete: bool
    action: str


@dataclass(frozen=True, slots=True)
class ProductReadiness:
    phase: ReadinessPhase
    message: str
    next_action: str
    data: Capability
    provider: Capability
    coach_grounded: bool
    setup_steps: tuple[SetupStep, ...]
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "message": self.message,
            "next_action": self.next_action,
            "data": {
                "available": self.data.available,
                "verified": self.data.verified,
                "reason": self.data.reason,
            },
            "provider": {
                "available": self.provider.available,
                "verified": self.provider.verified,
                "reason": self.provider.reason,
            },
            "coach": {
                "grounded": self.coach_grounded,
                "message": self._coach_message(),
            },
            "setup_steps": [
                {
                    "key": step.key,
                    "label": step.label,
                    "complete": step.complete,
                    "action": step.action,
                }
                for step in self.setup_steps
            ],
            "reasons": list(self.reasons),
        }

    def _coach_message(self) -> str:
        if self.provider.available and self.data.available:
            return (
                "Coach can reference your training data and will explain when a "
                "source is stale."
            )
        if self.provider.available:
            return (
                "Coach can answer general questions, but it has no training data "
                "to reference yet."
            )
        if self.data.available:
            return (
                "Your training data is available, but Coach needs an AI provider "
                "key before it can respond."
            )
        return "Coach needs an AI provider and a connected training data source."


def derive_product_readiness(
    *,
    has_source_connected: bool,
    has_training_data: bool,
    last_data_at: datetime | None,
    now: datetime,
    sync_in_progress: bool = False,
    provider_configured: bool = False,
    provider_available: bool = False,
    source_error: str | None = None,
    provider_error: str | None = None,
    fresh_for: timedelta = FRESH_FOR,
    stale_after: timedelta = STALE_AFTER,
) -> ProductReadiness:
    """Derive one honest readiness state from verified capability facts."""

    freshness = classify_freshness(
        last_data_at,
        now=now,
        fresh_for=fresh_for,
        stale_after=stale_after,
    )
    data_reason: str | None = None
    if not has_source_connected:
        data_reason = "No training data source is connected."
    elif not has_training_data:
        data_reason = "The source is connected but no training data has synced yet."
    elif freshness is Freshness.AGING:
        data_reason = "The most recent training data is no longer fresh."
    elif freshness in (Freshness.STALE, Freshness.NEVER):
        data_reason = "The most recent training data is old."

    provider_reason: str | None = None
    if provider_error:
        provider_reason = provider_error
    elif not provider_configured:
        provider_reason = "No AI provider key is configured."
    elif not provider_available:
        provider_reason = "The selected AI provider is unavailable on this server."

    data = Capability(
        available=has_training_data,
        verified=has_training_data,
        reason=data_reason,
    )
    provider = Capability(
        available=provider_available,
        verified=provider_available,
        reason=provider_reason,
    )
    coach_grounded = data.available and provider.available

    reasons: list[str] = []
    if source_error:
        reasons.append(source_error)
    if provider_error:
        reasons.append(provider_error)

    phase, message, next_action = _choose_phase(
        has_source_connected=has_source_connected,
        has_training_data=has_training_data,
        freshness=freshness,
        sync_in_progress=sync_in_progress,
        provider=provider,
        source_error=source_error,
        provider_error=provider_error,
        data_reason=data_reason,
        provider_reason=provider_reason,
    )

    steps = (
        SetupStep(
            "connect_source",
            "Connect a training data source",
            has_source_connected,
            "/settings",
        ),
        SetupStep(
            "sync_source",
            "Sync training history",
            has_training_data,
            "/settings",
        ),
        SetupStep(
            "configure_provider",
            "Configure an AI provider",
            provider.available,
            "/settings",
        ),
        SetupStep(
            "coach_ready",
            "Use grounded Coach",
            coach_grounded,
            "/chat",
        ),
    )

    return ProductReadiness(
        phase=phase,
        message=message,
        next_action=next_action,
        data=data,
        provider=provider,
        coach_grounded=coach_grounded,
        setup_steps=steps,
        reasons=tuple(reason for reason in reasons if reason),
    )


def _choose_phase(
    *,
    has_source_connected: bool,
    has_training_data: bool,
    freshness: Freshness,
    sync_in_progress: bool,
    provider: Capability,
    source_error: str | None,
    provider_error: str | None,
    data_reason: str | None,
    provider_reason: str | None,
) -> tuple[ReadinessPhase, str, str]:
    if source_error:
        return (
            ReadinessPhase.DEGRADED,
            f"Your data source needs attention: {source_error}",
            "/settings",
        )
    if provider_error:
        return (
            ReadinessPhase.DEGRADED,
            f"Coach is unavailable: {provider_error}",
            "/settings",
        )
    if sync_in_progress:
        return (
            ReadinessPhase.SYNCING,
            "Syncing your latest training data.",
            "/settings",
        )
    if not has_source_connected and not has_training_data:
        return (
            ReadinessPhase.SETUP_REQUIRED,
            "Connect Hevy to import your routines and training history.",
            "/settings",
        )
    if has_source_connected and not has_training_data:
        if freshness is Freshness.NEVER:
            return (
                ReadinessPhase.CONNECTING,
                "Your data source is connected. Waiting for the first sync.",
                "/settings",
            )
        return (
            ReadinessPhase.SYNCING,
            "Your data source is connected but no usable training data is available yet.",
            "/settings",
        )
    if not has_source_connected and has_training_data:
        return (
            ReadinessPhase.STALE,
            "Your data source is disconnected. Coach can only reference existing history.",
            "/settings",
        )
    if freshness is not Freshness.FRESH:
        return (
            ReadinessPhase.STALE,
            data_reason or "Your training data is out of date.",
            "/settings",
        )
    if not provider.available:
        return (
            ReadinessPhase.SETUP_REQUIRED,
            provider_reason or "Add an AI provider key to enable Coach.",
            "/settings",
        )
    return (
        ReadinessPhase.READY,
        "Your training data and AI provider are ready.",
        "/dashboard",
    )
