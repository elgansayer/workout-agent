"""Tenant-scoped, resumable first-run onboarding.

The sequence is always derived from the authenticated tenant's own records, so
a freshly loaded page resumes at the real state instead of trusting whatever a
browser remembers. Only the deliberate decision to skip an optional step is
persisted, and it is stored against the tenant's ``user_id``.

No step is reported complete because a client claims it: the checks below read
the same records the rest of the product uses (saved preferences, the Hevy
credential, health connections, the AI provider resolver, the active programme
and push subscriptions).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from database import DEFAULT_DB_PATH, _connect


class OnboardingStepStatus(StrEnum):
    """Verified state of one first-run step."""

    COMPLETE = "complete"
    PENDING = "pending"
    SKIPPED = "skipped"


class OnboardingError(ValueError):
    """A step cannot be skipped or resumed as requested."""


@dataclass(frozen=True, slots=True)
class StepDefinition:
    """Static description of one step in the onboarding sequence."""

    id: str
    title: str
    description: str
    href: str
    optional: bool
    skip_consequence: str | None = None


#: The canonical onboarding sequence. Required steps unlock real training data;
#: optional steps improve coaching but never block the account from being ready.
STEP_DEFINITIONS: tuple[StepDefinition, ...] = (
    StepDefinition(
        id="preferences",
        title="Set your account preferences",
        description=(
            "Choose your goal, experience level and coaching style so the "
            "programme and Coach fit you instead of using generic defaults."
        ),
        href="/settings?section=profile",
        optional=True,
        skip_consequence=(
            "Programme and Coach will use the default hypertrophy goal and "
            "intermediate experience level."
        ),
    ),
    StepDefinition(
        id="hevy",
        title="Connect Hevy",
        description=(
            "Add your Hevy API key so your routines and logged workouts can be "
            "imported. This is the source of the training data the app coaches from."
        ),
        href="/settings?section=connections",
        optional=False,
    ),
    StepDefinition(
        id="health_data",
        title="Connect health data",
        description=(
            "Optional: connect a health source so sleep, HRV and resting heart "
            "rate can inform readiness and recovery guidance."
        ),
        href="/settings?section=connections",
        optional=True,
        skip_consequence=(
            "Coach and readiness guidance will use workout data only; sleep, "
            "HRV and resting heart rate stay unavailable."
        ),
    ),
    StepDefinition(
        id="ai_provider",
        title="Configure an AI provider",
        description=(
            "Optional: add your own AI provider key so Coach answers with your "
            "chosen provider. The server fallback is used when no key is stored."
        ),
        href="/settings?section=ai",
        optional=True,
        skip_consequence=(
            "Coach will have no provider when the server has no fallback key, "
            "so coaching replies stay unavailable until you add one."
        ),
    ),
    StepDefinition(
        id="programme",
        title="Select your programme",
        description=(
            "Pick your Hevy routines, choose a duration and activate the "
            "programme so every training day has a real prescription."
        ),
        href="/programmes",
        optional=False,
    ),
    StepDefinition(
        id="notifications",
        title="Turn on notifications",
        description=(
            "Optional: allow browser push reminders for planned sessions and "
            "check-ins."
        ),
        href="/settings?section=notifications",
        optional=True,
        skip_consequence=(
            "You will not receive push reminders; you can enable them later in "
            "Settings without losing your setup progress."
        ),
    ),
)

_STEP_BY_ID = {step.id: step for step in STEP_DEFINITIONS}
_OPTIONAL_IDS = frozenset(step.id for step in STEP_DEFINITIONS if step.optional)


@dataclass(frozen=True, slots=True)
class OnboardingStep:
    """One step with the state verified for this tenant."""

    id: str
    title: str
    description: str
    href: str
    optional: bool
    status: OnboardingStepStatus
    skip_consequence: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "href": self.href,
            "optional": self.optional,
            "status": str(self.status),
            "skip_consequence": self.skip_consequence,
        }


@dataclass(frozen=True, slots=True)
class OnboardingState:
    """Resumable onboarding state derived from the tenant's records."""

    steps: tuple[OnboardingStep, ...]
    next_step: OnboardingStep | None
    account_ready: bool
    completed_count: int
    skipped_count: int
    total_steps: int
    progress_percent: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": [step.as_dict() for step in self.steps],
            "next_step": self.next_step.as_dict() if self.next_step else None,
            "account_ready": self.account_ready,
            "completed_count": self.completed_count,
            "skipped_count": self.skipped_count,
            "total_steps": self.total_steps,
            "progress_percent": self.progress_percent,
        }


def _skipped_steps(user_id: str, db_path: str) -> set[str]:
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT step FROM onboarding_skips WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    # Only optional steps can ever be skipped. A stale or tampered required
    # entry is ignored so it can never make the account look ready.
    return {str(row[0]) for row in rows} & _OPTIONAL_IDS


def _preferences_saved(user_id: str, db_path: str) -> bool:
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM user_preferences WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


def _hevy_connected(user_id: str, db_path: str) -> bool:
    from secret_vault import SecretVault, VaultError

    try:
        return SecretVault(db_path).has_usable_credential(user_id, "hevy")
    except VaultError:
        return False


def _health_connected(user_id: str, db_path: str) -> bool:
    from health_connection_store import list_connections

    return any(
        str(connection.get("state")) == "connected"
        for connection in list_connections(user_id, db_path=db_path)
    )


def _programme_active(user_id: str, db_path: str) -> bool:
    from database import get_active_programme

    return bool(get_active_programme(user_id, db_path=db_path))


def _notifications_enabled(user_id: str, db_path: str) -> bool:
    from database import get_push_subscriptions

    return bool(get_push_subscriptions(user_id, db_path=db_path))


def _ai_provider_ready(
    user_id: str, db_path: str, server_ai_key: str | None
) -> bool:
    from setup_state import ai_provider_configured

    return ai_provider_configured(
        user_id=user_id,
        db_path=db_path,
        server_api_key=server_ai_key,
    )


def _verified_completion(
    step_id: str,
    *,
    user_id: str,
    db_path: str,
    server_ai_key: str | None,
) -> bool:
    if step_id == "preferences":
        return _preferences_saved(user_id, db_path)
    if step_id == "hevy":
        return _hevy_connected(user_id, db_path)
    if step_id == "health_data":
        return _health_connected(user_id, db_path)
    if step_id == "ai_provider":
        return _ai_provider_ready(user_id, db_path, server_ai_key)
    if step_id == "programme":
        return _programme_active(user_id, db_path)
    if step_id == "notifications":
        return _notifications_enabled(user_id, db_path)
    return False


def build_onboarding(
    *,
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
    server_ai_key: str | None = None,
) -> OnboardingState:
    """Derive the tenant's resumable onboarding state from verified records."""

    if not isinstance(user_id, str) or not user_id.strip():
        raise OnboardingError("An authenticated user is required.")

    skipped = _skipped_steps(user_id, db_path)
    steps: list[OnboardingStep] = []
    for definition in STEP_DEFINITIONS:
        complete = _verified_completion(
            definition.id,
            user_id=user_id,
            db_path=db_path,
            server_ai_key=server_ai_key,
        )
        if complete:
            status = OnboardingStepStatus.COMPLETE
        elif definition.id in skipped:
            status = OnboardingStepStatus.SKIPPED
        else:
            status = OnboardingStepStatus.PENDING
        steps.append(
            OnboardingStep(
                id=definition.id,
                title=definition.title,
                description=definition.description,
                href=definition.href,
                optional=definition.optional,
                status=status,
                skip_consequence=definition.skip_consequence,
            )
        )

    completed_count = sum(
        1 for step in steps if step.status is OnboardingStepStatus.COMPLETE
    )
    skipped_count = sum(
        1 for step in steps if step.status is OnboardingStepStatus.SKIPPED
    )
    total = len(steps)
    resolved = completed_count + skipped_count
    progress_percent = round(resolved / total * 100) if total else 100

    required_pending = [
        step
        for step in steps
        if not step.optional and step.status is OnboardingStepStatus.PENDING
    ]
    optional_pending = [
        step
        for step in steps
        if step.optional and step.status is OnboardingStepStatus.PENDING
    ]

    # Required work is always the highest-value next action. Optional work is
    # only surfaced once the account can already do something useful.
    next_step = (
        required_pending[0]
        if required_pending
        else (optional_pending[0] if optional_pending else None)
    )

    return OnboardingState(
        steps=tuple(steps),
        next_step=next_step,
        account_ready=not required_pending,
        completed_count=completed_count,
        skipped_count=skipped_count,
        total_steps=total,
        progress_percent=progress_percent,
    )


def _definition(step_id: str) -> StepDefinition:
    definition = _STEP_BY_ID.get(step_id)
    if definition is None:
        raise OnboardingError(f"Unknown onboarding step '{step_id}'.")
    return definition


def skip_step(
    user_id: str,
    step_id: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
    server_ai_key: str | None = None,
) -> OnboardingState:
    """Persist an explicit skip for one optional step for this tenant."""

    definition = _definition(step_id)
    if not definition.optional:
        raise OnboardingError(
            f"'{definition.title}' is required and cannot be skipped."
        )
    if not isinstance(user_id, str) or not user_id.strip():
        raise OnboardingError("An authenticated user is required.")

    now = datetime.now(tz=timezone.utc).isoformat()
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO onboarding_skips (user_id, step, skipped_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, step) DO UPDATE SET
                skipped_at = excluded.skipped_at
            """,
            (user_id, definition.id, now),
        )
    return build_onboarding(
        user_id=user_id, db_path=db_path, server_ai_key=server_ai_key
    )


def resume_step(
    user_id: str,
    step_id: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
    server_ai_key: str | None = None,
) -> OnboardingState:
    """Forget a previous skip so the step appears in the sequence again."""

    definition = _definition(step_id)
    if not isinstance(user_id, str) or not user_id.strip():
        raise OnboardingError("An authenticated user is required.")
    with _connect(db_path) as connection:
        connection.execute(
            "DELETE FROM onboarding_skips WHERE user_id = ? AND step = ?",
            (user_id, definition.id),
        )
    return build_onboarding(
        user_id=user_id, db_path=db_path, server_ai_key=server_ai_key
    )


__all__ = [
    "STEP_DEFINITIONS",
    "OnboardingError",
    "OnboardingState",
    "OnboardingStep",
    "OnboardingStepStatus",
    "StepDefinition",
    "build_onboarding",
    "resume_step",
    "skip_step",
]
