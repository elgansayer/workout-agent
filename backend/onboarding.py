"""Resumable first-run onboarding derived from verified server state.

Onboarding is not a client-side wizard. Every completed step is recomputed from
tenant-scoped persistence each time the state is requested; only deliberate
skips of optional steps are stored. This keeps progress honest after credentials
are revoked or a programme is deactivated.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

from connectors.base import ConnectorContext, ConnectorState
from connectors.builtin import build_builtin_registry
from database import (
    get_active_programme,
    get_meta,
    get_onboarding_progress,
    get_push_subscriptions,
    get_user_api_keys,
    get_user_preferences,
)

logger = logging.getLogger(__name__)

_GOOGLE_HEALTH_TOKEN_KEY = "google_health_refresh_token"


@dataclass(frozen=True, slots=True)
class OnboardingStep:
    id: str
    title: str
    description: str
    required: bool
    action_label: str
    href: str
    consequence: str


ONBOARDING_STEPS: tuple[OnboardingStep, ...] = (
    OnboardingStep(
        id="account",
        title="Account preferences",
        description="Choose your goals and experience level so coaching starts from the right baseline.",
        required=True,
        action_label="Set preferences",
        href="/settings",
        consequence="",
    ),
    OnboardingStep(
        id="hevy",
        title="Connect Hevy",
        description="Connect your Hevy account so workouts and routines can sync.",
        required=True,
        action_label="Connect Hevy",
        href="/settings",
        consequence="",
    ),
    OnboardingStep(
        id="health",
        title="Add health data",
        description="Connect a wearable or Google Health for recovery and readiness context.",
        required=False,
        action_label="Connect health data",
        href="/settings",
        consequence=(
            "Without health data, recovery and readiness guidance stays limited. "
            "You can connect a provider later in Settings."
        ),
    ),
    OnboardingStep(
        id="ai",
        title="Choose an AI provider",
        description="Add your own AI provider key for coaching and generated insights.",
        required=False,
        action_label="Choose provider",
        href="/settings",
        consequence=(
            "Without an AI provider, coach replies and generated insights are "
            "unavailable. You can add a key later in Settings."
        ),
    ),
    OnboardingStep(
        id="programme",
        title="Select your programme",
        description="Pick Hevy routines and activate a programme for your next workouts.",
        required=True,
        action_label="Build programme",
        href="/programmes",
        consequence="",
    ),
    OnboardingStep(
        id="notifications",
        title="Enable notifications",
        description="Turn on push notifications for workout reminders and check-ins.",
        required=False,
        action_label="Enable notifications",
        href="/settings",
        consequence=(
            "Without notifications, you will not receive workout or check-in "
            "reminders. You can enable them later in Settings."
        ),
    ),
)

STEP_IDS = frozenset(step.id for step in ONBOARDING_STEPS)
OPTIONAL_STEP_IDS = frozenset(step.id for step in ONBOARDING_STEPS if not step.required)


def _account_complete(user_id: str, db_path: str) -> bool:
    preferences = get_user_preferences(user_id, db_path)
    goals = preferences.get("goals")
    return isinstance(goals, list) and bool(goals)


def _hevy_complete(user_id: str, db_path: str) -> bool:
    keys = get_user_api_keys(user_id, db_path)
    hevy = keys.get("hevy") or {}
    return bool(str(hevy.get("api_key") or "").strip())


def _health_complete(user_id: str, db_path: str) -> bool:
    if str(get_meta(_GOOGLE_HEALTH_TOKEN_KEY, db_path, user_id=user_id) or "").strip():
        return True
    try:
        registry = build_builtin_registry()
        context = ConnectorContext(user_id=user_id)
        for connector in registry.all():
            if connector.status(context).state is ConnectorState.CONNECTED:
                return True
    except Exception:
        logger.exception("Could not read connector status during onboarding.")
    return False


def _ai_complete(user_id: str, db_path: str) -> bool:
    preferences = get_user_preferences(user_id, db_path)
    provider = str(preferences.get("preferred_ai") or "gemini").strip().lower()
    keys = get_user_api_keys(user_id, db_path)
    record = keys.get(provider) or {}
    if str(record.get("api_key") or "").strip():
        return True
    return provider == "gemini" and bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _programme_complete(user_id: str, db_path: str) -> bool:
    active = get_active_programme(user_id, db_path)
    return bool(active and active.get("definition"))


def _notifications_complete(user_id: str, db_path: str) -> bool:
    return bool(get_push_subscriptions(user_id, db_path))


def _is_complete(step_id: str, user_id: str, db_path: str) -> bool:
    checks = {
        "account": _account_complete,
        "hevy": _hevy_complete,
        "health": _health_complete,
        "ai": _ai_complete,
        "programme": _programme_complete,
        "notifications": _notifications_complete,
    }
    return checks[step_id](user_id, db_path)


def build_onboarding_state(
    user_id: str,
    db_path: str = "workout_agent.db",
) -> dict[str, Any]:
    """Return the current onboarding sequence and next actions for one user."""
    progress = get_onboarding_progress(user_id, db_path)
    steps: list[dict[str, Any]] = []

    for position, step in enumerate(ONBOARDING_STEPS, start=1):
        completed = _is_complete(step.id, user_id, db_path)
        skipped = (
            not completed
            and not step.required
            and progress.get(step.id) == "skipped"
        )
        status = "complete" if completed else ("skipped" if skipped else "pending")
        steps.append(
            {
                **asdict(step),
                "order": position,
                "status": status,
                "completed": completed,
                "skipped": skipped,
                "resolved": completed or skipped,
            }
        )

    required_steps = [step for step in steps if step["required"]]
    pending_required = [step for step in required_steps if not step["completed"]]
    pending_steps = [step for step in steps if not step["resolved"]]
    pending_optional = [step for step in pending_steps if not step["required"]]
    resolved_count = sum(1 for step in steps if step["resolved"])
    completed_count = sum(1 for step in steps if step["completed"])
    required_completed = sum(1 for step in required_steps if step["completed"])

    next_action = pending_required[0] if pending_required else (
        pending_optional[0] if pending_optional else None
    )

    return {
        "steps": steps,
        "ready": not pending_required,
        "complete": not pending_steps,
        "next_action": next_action,
        "next_required_action": pending_required[0] if pending_required else None,
        "total_count": len(steps),
        "resolved_count": resolved_count,
        "completed_count": completed_count,
        "required_total": len(required_steps),
        "required_completed": required_completed,
        "progress_percent": round(resolved_count / len(steps) * 100) if steps else 100,
    }


__all__ = [
    "ONBOARDING_STEPS",
    "OPTIONAL_STEP_IDS",
    "STEP_IDS",
    "OnboardingStep",
    "build_onboarding_state",
]
