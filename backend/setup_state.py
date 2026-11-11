"""Tenant-scoped setup state for surfaces that can legitimately be empty.

Progress, stats, history, plan and coach surfaces are empty for very different
reasons: an account that never connected Hevy, an account that connected but
never activated a programme, an account whose sync stopped, and an account
whose Hevy key was removed.  This module derives one deterministic contract
from a single tenant's own records so every surface can say *why* the data is
absent and which verified in-product setup action comes next.

The module never fabricates data, never inspects another tenant's rows, and
never claims a capability the account does not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any

# A tenant with no activity for this many days is reported as stale rather
# than ready.  Seven days matches the weekly training rhythm.
STALE_AFTER_DAYS = 7


class SetupStateKind(StrEnum):
    """Account-level readiness of the training data pipeline."""

    NEW = "new"
    PARTIALLY_CONFIGURED = "partially_configured"
    READY = "ready"
    STALE = "stale"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class SetupAction:
    """A verified, in-product setup step a tenant can actually take."""

    id: str
    label: str
    href: str
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "href": self.href,
            "description": self.description,
        }


CONNECT_HEVY = SetupAction(
    id="connect_hevy",
    label="Connect Hevy",
    href="/settings",
    description=(
        "Add your Hevy API key in Settings so your routines and workouts can be imported."
    ),
)
SYNC_HEVY = SetupAction(
    id="sync_hevy",
    label="Sync Hevy history",
    href="/settings",
    description=(
        "Run a history sync from Settings so your logged sessions and sets appear here."
    ),
)
BUILD_PROGRAMME = SetupAction(
    id="build_programme",
    label="Build your programme",
    href="/programmes",
    description=(
        "Pick your Hevy routines, set your goal and duration, then activate the programme."
    ),
)
REBUILD_PROGRAMME = SetupAction(
    id="rebuild_programme",
    label="Rebuild your programme",
    href="/programmes",
    description=(
        "Your programme no longer prescribes a valid rotation. Rebuild it from your Hevy routines."
    ),
)
CONFIGURE_AI = SetupAction(
    id="configure_ai",
    label="Configure an AI provider",
    href="/settings",
    description=(
        "Add your AI provider key in Settings so Coach can answer from your training data."
    ),
)

#: Every setup action the product can offer, keyed by its stable identifier.
SETUP_ACTIONS: dict[str, SetupAction] = {
    action.id: action
    for action in (CONNECT_HEVY, SYNC_HEVY, BUILD_PROGRAMME, REBUILD_PROGRAMME, CONFIGURE_AI)
}

#: Setup requirements per surface.  The value is the ordered list of checks
#: that must hold before the surface can show meaningful content.
SURFACE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "dashboard": ("hevy_connected", "programme_active"),
    "plan": ("hevy_connected", "programme_active"),
    "progress": ("hevy_connected", "training_data"),
    "stats": ("hevy_connected", "training_data"),
    "history": ("hevy_connected", "session_history"),
    "checkins": ("hevy_connected", "programme_active", "session_history"),
    "coach": ("hevy_connected", "training_data", "ai_provider"),
}

#: Surfaces whose content is produced by the active programme.
PROGRAMME_SURFACES = frozenset({"dashboard", "plan", "checkins"})

_REQUIREMENT_ACTIONS: dict[str, SetupAction] = {
    "hevy_connected": CONNECT_HEVY,
    "programme_active": BUILD_PROGRAMME,
    "training_data": SYNC_HEVY,
    "session_history": SYNC_HEVY,
    "ai_provider": CONFIGURE_AI,
}

_REQUIREMENT_REASONS: dict[str, str] = {
    "hevy_connected": (
        "Hevy is not connected, so no routines or workouts can be imported yet."
    ),
    "programme_active": (
        "No programme is active, so there is no prescription to follow."
    ),
    "training_data": (
        "No logged sets have been imported yet, so there is nothing to chart."
    ),
    "session_history": (
        "No training sessions have been recorded yet, so there is no history to show."
    ),
    "ai_provider": (
        "No AI provider is configured, so Coach cannot answer questions yet."
    ),
}

_STATE_LABELS: dict[SetupStateKind, str] = {
    SetupStateKind.NEW: "Finish setting up your training data",
    SetupStateKind.PARTIALLY_CONFIGURED: "Setup is not finished yet",
    SetupStateKind.READY: "Your training data is up to date",
    SetupStateKind.STALE: "Your training data is out of date",
    SetupStateKind.DISCONNECTED: "Hevy is no longer connected",
}


@dataclass(frozen=True, slots=True)
class AccountSetup:
    """Account-level readiness derived from one tenant's own records."""

    state: SetupStateKind
    checks: dict[str, bool]
    missing: tuple[str, ...]
    last_activity_at: datetime | None
    age_days: int | None
    stale: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "label": _STATE_LABELS[self.state],
            "checks": dict(self.checks),
            "missing": list(self.missing),
            "last_activity_at": (
                self.last_activity_at.isoformat() if self.last_activity_at else None
            ),
            "age_days": self.age_days,
            "stale": self.stale,
        }


@dataclass(frozen=True, slots=True)
class SurfaceSetup:
    """The setup contract rendered by one surface."""

    state: SetupStateKind
    label: str
    reason: str
    action: SetupAction | None
    missing: tuple[str, ...]
    last_activity_at: datetime | None
    age_days: int | None
    stale: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "label": self.label,
            "reason": self.reason,
            "action": self.action.as_dict() if self.action else None,
            "missing": list(self.missing),
            "last_activity_at": (
                self.last_activity_at.isoformat() if self.last_activity_at else None
            ),
            "age_days": self.age_days,
            "stale": self.stale,
        }


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp or a plain calendar date into an aware datetime."""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) == 10:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            return None
        return datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            tzinfo=timezone.utc,
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _latest_datetime(values: list[Any]) -> datetime | None:
    stamps = [stamp for stamp in map(_parse_datetime, values) if stamp is not None]
    return max(stamps) if stamps else None


def derive_account_state(
    *,
    has_hevy_key: bool,
    has_programme: bool,
    has_training_data: bool,
    has_session_history: bool,
    has_ai_provider: bool,
    last_activity_at: datetime | None,
    now: datetime | None = None,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> AccountSetup:
    """Derive the account-level setup state from verified tenant facts."""

    now = now or datetime.now(tz=timezone.utc)
    age_days: int | None = None
    stale = False
    if last_activity_at is not None:
        age_days = max(0, (now - last_activity_at).days)
        stale = age_days > stale_after_days

    checks = {
        "hevy_connected": bool(has_hevy_key),
        "programme_active": bool(has_programme),
        "training_data": bool(has_training_data),
        "session_history": bool(has_session_history),
        "ai_provider": bool(has_ai_provider),
    }

    missing = tuple(name for name, satisfied in checks.items() if not satisfied)

    has_history = bool(has_training_data or has_session_history)
    if not has_hevy_key and (has_programme or has_history):
        state = SetupStateKind.DISCONNECTED
    elif not has_hevy_key and not has_programme:
        state = SetupStateKind.NEW
    elif not has_programme or not has_history:
        state = SetupStateKind.PARTIALLY_CONFIGURED
    elif stale:
        state = SetupStateKind.STALE
    else:
        state = SetupStateKind.READY

    return AccountSetup(
        state=state,
        checks=checks,
        missing=missing,
        last_activity_at=last_activity_at,
        age_days=age_days,
        stale=stale,
    )


def ai_provider_configured(
    *,
    user_id: str,
    db_path: str,
    server_api_key: str | None = None,
) -> bool:
    """Return whether this tenant can reach an AI provider.

    The tenant's own encrypted key for their preferred provider counts, as does
    the server-side Gemini fallback.  No other tenant's credential is read, and
    the key itself never leaves the vault: only a yes/no does.
    """

    from ai_provider import PROVIDERS
    from database import get_user_preferences
    from secret_vault import SecretVault, VaultError

    try:
        prefs = get_user_preferences(user_id, db_path=db_path)
    except Exception:  # noqa: BLE001 - a missing preference row is not an error
        prefs = {}
    provider = str(prefs.get("preferred_ai") or "gemini").lower().strip()
    if provider not in PROVIDERS:
        provider = "gemini"

    try:
        usable = SecretVault(db_path).has_usable_credential(user_id, provider)
    except VaultError:  # no such account: nothing is usable
        usable = False
    if usable:
        return True
    return provider == "gemini" and bool((server_api_key or "").strip())


def build_account_setup(
    *,
    user_id: str,
    db_path: str,
    now: datetime | None = None,
    server_ai_key: str | None = None,
) -> AccountSetup:
    """Read this tenant's records and derive the account setup state."""

    from database import (
        get_active_programme,
        get_daily_logs,
        get_progress_history,
        get_session_volumes,
    )
    from secret_vault import SecretVault, VaultError

    now = now or datetime.now(tz=timezone.utc)
    try:
        has_hevy_key = SecretVault(db_path).has_usable_credential(user_id, "hevy")
    except VaultError:  # no such account: nothing is usable
        has_hevy_key = False

    programme = get_active_programme(user_id, db_path=db_path)
    progress = get_progress_history(db_path=db_path, user_id=user_id)
    sessions = get_session_volumes(db_path=db_path, user_id=user_id)
    logs = get_daily_logs(limit=30, db_path=db_path, user_id=user_id)

    candidates: list[Any] = []
    for entries in progress.values():
        candidates.extend(entry.get("date") for entry in entries)
    candidates.extend(entry.get("date") for entry in sessions)
    candidates.extend(entry.get("date") for entry in logs)

    last_activity_at = _latest_datetime(candidates)

    return derive_account_state(
        has_hevy_key=has_hevy_key,
        has_programme=bool(programme),
        has_training_data=bool(progress),
        has_session_history=bool(sessions or logs),
        has_ai_provider=ai_provider_configured(
            user_id=user_id,
            db_path=db_path,
            server_api_key=server_ai_key,
        ),
        last_activity_at=last_activity_at,
        now=now,
    )


def surface_setup(
    account: AccountSetup,
    surface: str,
    *,
    programme_status: str | None = None,
) -> SurfaceSetup:
    """Derive the setup contract for one surface from the account state."""

    requirements = SURFACE_REQUIREMENTS.get(surface, ())
    unmet = [name for name in requirements if not account.checks.get(name, False)]

    reasons: list[str] = []
    action: SetupAction | None = None
    state = account.state

    programme_issue = _programme_issue(programme_status, surface)
    if programme_issue is not None:
        issue_action, issue_reason = programme_issue
        action = issue_action
        reasons.append(issue_reason)
        state = SetupStateKind.PARTIALLY_CONFIGURED
    elif unmet:
        action = _REQUIREMENT_ACTIONS[unmet[0]]
        reasons.append(_REQUIREMENT_REASONS[unmet[0]])
        state = (
            SetupStateKind.NEW
            if account.state == SetupStateKind.NEW
            else SetupStateKind.PARTIALLY_CONFIGURED
        )

    if account.state == SetupStateKind.DISCONNECTED:
        action = CONNECT_HEVY
        reasons.insert(0, "Your Hevy API key was removed, so nothing new can be imported.")
        state = SetupStateKind.DISCONNECTED
    elif account.state == SetupStateKind.STALE and not unmet and programme_issue is None:
        action = SYNC_HEVY
        reasons.append(
            f"Your last training activity was {account.age_days} days ago, "
            "so this view may be out of date."
        )
        state = SetupStateKind.STALE

    return SurfaceSetup(
        state=state,
        label=_STATE_LABELS[state],
        reason=" ".join(reasons),
        action=action,
        missing=tuple(unmet),
        last_activity_at=account.last_activity_at,
        age_days=account.age_days,
        stale=account.stale,
    )


def _programme_issue(
    programme_status: str | None,
    surface: str,
) -> tuple[SetupAction, str] | None:
    """Return the actionable programme problem for a programme-driven surface."""

    if surface not in PROGRAMME_SURFACES or not programme_status:
        return None
    if programme_status == "needs_review":
        return (
            REBUILD_PROGRAMME,
            "Your programme no longer prescribes a valid rotation.",
        )
    if programme_status == "completed":
        return (
            BUILD_PROGRAMME,
            "This programme cycle has finished, so there is no workout left to prescribe.",
        )
    return None


def surface_setup_payload(
    account: AccountSetup,
    surface: str,
    *,
    programme_status: str | None = None,
) -> dict[str, Any]:
    """Serialise the setup contract for one surface."""

    return surface_setup(
        account,
        surface,
        programme_status=programme_status,
    ).as_dict()


__all__ = [
    "BUILD_PROGRAMME",
    "CONFIGURE_AI",
    "CONNECT_HEVY",
    "REBUILD_PROGRAMME",
    "SETUP_ACTIONS",
    "STALE_AFTER_DAYS",
    "SURFACE_REQUIREMENTS",
    "AccountSetup",
    "SetupAction",
    "SetupStateKind",
    "SurfaceSetup",
    "ai_provider_configured",
    "build_account_setup",
    "derive_account_state",
    "surface_setup",
    "surface_setup_payload",
]
