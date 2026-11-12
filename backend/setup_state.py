"""Tenant-scoped readiness for surfaces that can legitimately be empty.

Progress, stats, history, plan and coach surfaces are empty or incomplete for
very different reasons: an account that never connected Hevy, one that is
connecting or mid-sync right now, one whose last sync failed or whose Hevy key
was rejected or removed, and one whose data simply stopped updating.  This
module derives one deterministic readiness state from a single tenant's own
records so every surface can say *why* data is absent and which verified
in-product action comes next.

The six account states are ``setup_required``, ``connecting``, ``syncing``,
``ready``, ``stale`` and ``degraded``.  They are reproducibly derived from
records the tenant owns: the stored Hevy credential and its last verification,
the persisted connector operation and last sync outcome
(:mod:`connector_operations`), the imported data and its freshness, the active
programme, and the verification evidence of the AI provider.

The module never fabricates data, never inspects another tenant's rows, and
never claims a capability the account does not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any

from connector_operations import ConnectorStatus, OperationPhase, read_connector_status

# A tenant with no activity and no successful sync for this many days is
# reported as stale rather than ready.  Seven days matches the weekly training
# rhythm.
STALE_AFTER_DAYS = 7


class SetupStateKind(StrEnum):
    """Account-level readiness of the training data pipeline."""

    SETUP_REQUIRED = "setup_required"
    CONNECTING = "connecting"
    SYNCING = "syncing"
    READY = "ready"
    STALE = "stale"
    DEGRADED = "degraded"


class ProviderState(StrEnum):
    """What the account can truthfully say about its AI provider."""

    UNSUPPORTED = "unsupported"
    MISSING_KEY = "missing_key"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


def derive_provider_state(
    *,
    supported: bool,
    configured: bool,
    last_test_status: str | None,
) -> ProviderState:
    """Classify a provider from its configuration and its last connection test.

    A credential that was never tested is ``unverified``; only a passing test
    makes it ``verified``.  A failed test is remembered as ``failed`` until the
    key is replaced or tested again.
    """

    if not supported:
        return ProviderState.UNSUPPORTED
    if not configured:
        return ProviderState.MISSING_KEY
    status = (last_test_status or "").strip().lower()
    if not status:
        return ProviderState.UNVERIFIED
    return ProviderState.VERIFIED if status == "ok" else ProviderState.FAILED


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
RECONNECT_HEVY = SetupAction(
    id="reconnect_hevy",
    label="Reconnect Hevy",
    href="/settings",
    description=(
        "Verify or replace your Hevy API key in Settings so imports can resume."
    ),
)
VERIFY_HEVY = SetupAction(
    id="verify_hevy",
    label="Verify Hevy connection",
    href="/settings",
    description="Run the Hevy connection check again from Settings.",
)
SYNC_HEVY = SetupAction(
    id="sync_hevy",
    label="Sync Hevy history",
    href="/settings",
    description=(
        "Run a history sync from Settings so your logged sessions and sets appear here."
    ),
)
RETRY_SYNC = SetupAction(
    id="retry_sync",
    label="Retry Hevy sync",
    href="/settings",
    description="Run the history sync again from Settings.",
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
VERIFY_AI = SetupAction(
    id="verify_ai",
    label="Test your AI provider",
    href="/settings",
    description=(
        "Run a connection test in Settings so Coach can confirm your AI provider works."
    ),
)
FIX_AI = SetupAction(
    id="fix_ai",
    label="Fix your AI provider",
    href="/settings",
    description=(
        "Your last AI connection test failed. Test it again or replace the key in Settings."
    ),
)

#: Every setup action the product can offer, keyed by its stable identifier.
SETUP_ACTIONS: dict[str, SetupAction] = {
    action.id: action
    for action in (
        CONNECT_HEVY,
        RECONNECT_HEVY,
        VERIFY_HEVY,
        SYNC_HEVY,
        RETRY_SYNC,
        BUILD_PROGRAMME,
        REBUILD_PROGRAMME,
        CONFIGURE_AI,
        VERIFY_AI,
        FIX_AI,
    )
}

#: Setup requirements per surface.  The value is the ordered list of checks
#: that must hold before the surface can show meaningful content.  Coach needs
#: a *verified* provider: a stored key alone does not prove it works.
SURFACE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "dashboard": ("hevy_connected", "programme_active"),
    "plan": ("hevy_connected", "programme_active"),
    "progress": ("hevy_connected", "training_data"),
    "stats": ("hevy_connected", "training_data"),
    "history": ("hevy_connected", "session_history"),
    "checkins": ("hevy_connected", "programme_active", "session_history"),
    "coach": ("hevy_connected", "training_data", "ai_provider", "ai_verified"),
}

#: Surfaces whose content is produced by the active programme.
PROGRAMME_SURFACES = frozenset({"dashboard", "plan", "checkins"})

_REQUIREMENT_ACTIONS: dict[str, SetupAction] = {
    "hevy_connected": CONNECT_HEVY,
    "programme_active": BUILD_PROGRAMME,
    "training_data": SYNC_HEVY,
    "session_history": SYNC_HEVY,
    "ai_provider": CONFIGURE_AI,
    "ai_verified": VERIFY_AI,
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
    "ai_verified": (
        "Your AI provider key has not been tested, so Coach cannot confirm it works."
    ),
}

_NO_WORKOUTS_REASON = (
    "Your last Hevy sync finished but found no logged workouts. "
    "Log a workout in Hevy, then sync again."
)

_PROVIDER_FAILED_REASON = (
    "Your last AI provider connection test failed, so Coach cannot rely on it."
)

_DEGRADED_ACTIONS: dict[str, SetupAction] = {
    "credential_removed": CONNECT_HEVY,
    "credential_rejected": RECONNECT_HEVY,
    "credential_insufficient_scope": RECONNECT_HEVY,
    "connection_unverified": VERIFY_HEVY,
    "sync_failed": RETRY_SYNC,
    "operation_stalled": RETRY_SYNC,
}

_STATE_LABELS: dict[SetupStateKind, str] = {
    SetupStateKind.SETUP_REQUIRED: "Setup is not finished yet",
    SetupStateKind.CONNECTING: "Connecting to Hevy",
    SetupStateKind.SYNCING: "Syncing your Hevy history",
    SetupStateKind.READY: "Your training data is up to date",
    SetupStateKind.STALE: "Your training data is out of date",
    SetupStateKind.DEGRADED: "Your Hevy connection needs attention",
}

_FIRST_RUN_LABEL = "Finish setting up your training data"


def _state_label(state: SetupStateKind, *, first_run: bool) -> str:
    if state is SetupStateKind.SETUP_REQUIRED and first_run:
        return _FIRST_RUN_LABEL
    return _STATE_LABELS[state]


@dataclass(frozen=True, slots=True)
class AccountSetup:
    """Account-level readiness derived from one tenant's own records."""

    state: SetupStateKind
    checks: dict[str, bool]
    missing: tuple[str, ...]
    last_activity_at: datetime | None
    age_days: int | None
    stale: bool
    reason: str = ""
    reason_code: str | None = None
    first_run: bool = False
    connector: ConnectorStatus | None = None
    connection_state: str | None = None
    ai_provider_state: str | None = None
    ai_provider_source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "label": _state_label(self.state, first_run=self.first_run),
            "reason": self.reason,
            "reason_code": self.reason_code,
            "first_run": self.first_run,
            "checks": dict(self.checks),
            "missing": list(self.missing),
            "last_activity_at": (
                self.last_activity_at.isoformat() if self.last_activity_at else None
            ),
            "age_days": self.age_days,
            "stale": self.stale,
            "connection_state": self.connection_state,
            "connector": self.connector.as_dict() if self.connector else None,
            "ai_provider_state": self.ai_provider_state,
            "ai_provider_source": self.ai_provider_source,
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
    reason_code: str | None = None
    first_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "label": self.label,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "first_run": self.first_run,
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


def _minutes_ago(started_at: datetime | None, now: datetime) -> str:
    if started_at is None:
        return "a while ago"
    minutes = max(0, int((now - started_at).total_seconds() // 60))
    if minutes == 0:
        return "less than a minute ago"
    return f"{minutes} minute{'s' if minutes != 1 else ''} ago"


def _connection_problem(
    connection_state: str | None,
    connection_tested_at: datetime | None,
    connector: ConnectorStatus | None,
) -> str | None:
    """Return the degraded reason code for a Hevy connection check, if any.

    An explicit rejection or missing scope always counts.  ``unverified`` means
    Hevy could not be reached when the key was last checked; a sync that
    succeeded afterwards proves the key works, so it clears that doubt.
    """

    if connection_state == "invalid":
        return "credential_rejected"
    if connection_state == "insufficient_scope":
        return "credential_insufficient_scope"
    if connection_state == "unverified":
        later_success = (
            connector is not None
            and connector.last_sync_succeeded
            and connector.last_sync_at is not None
            and connection_tested_at is not None
            and connector.last_sync_at > connection_tested_at
        )
        return None if later_success else "connection_unverified"
    return None


def _degraded_reason(
    code: str,
    connector: ConnectorStatus | None,
) -> str:
    if code == "credential_removed":
        return "Your Hevy API key was removed, so nothing new can be imported."
    if code == "credential_rejected":
        return "Hevy rejected your stored API key, so nothing new can be imported."
    if code == "credential_insufficient_scope":
        return (
            "Your Hevy API key cannot read your workouts, so nothing new can be "
            "imported."
        )
    if code == "connection_unverified":
        return (
            "The last Hevy connection check could not reach Hevy, so the "
            "connection is unconfirmed."
        )
    if code == "sync_failed":
        when = (
            f" on {connector.last_sync_at.date().isoformat()}"
            if connector is not None and connector.last_sync_at is not None
            else ""
        )
        return (
            f"Your last Hevy sync failed{when}, so recent workouts may be missing."
        )
    return "A previous Hevy operation never finished, so this view may be incomplete."


def _live_reason(
    operation: OperationPhase,
    started_at: datetime | None,
    now: datetime,
) -> str:
    if operation is OperationPhase.CONNECTING:
        return (
            "Checking your Hevy API key. This view updates when the check finishes."
        )
    return (
        f"A Hevy sync started {_minutes_ago(started_at, now)} is still running. "
        "This view updates when it finishes, and figures may be incomplete until then."
    )


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
    connector: ConnectorStatus | None = None,
    connection_state: str | None = None,
    connection_tested_at: datetime | None = None,
    ai_provider_state: ProviderState | str | None = None,
    ai_provider_source: str | None = None,
) -> AccountSetup:
    """Derive the account-level readiness from verified tenant facts.

    Precedence, most specific first: a live connect or sync; a stalled
    operation; a missing, rejected or unconfirmed Hevy credential; a failed last
    sync; unfinished setup; stale data; ready.  A live operation wins over
    everything because the data a surface would show is changing under it.
    """

    now = now or datetime.now(tz=timezone.utc)
    age_days: int | None = None
    if last_activity_at is not None:
        age_days = max(0, (now - last_activity_at).days)

    # Data is current when the account trained recently *or* was synced
    # recently: a successful sync of a quiet week is still fresh data.
    last_success_at = (
        connector.last_sync_at
        if connector is not None and connector.last_sync_succeeded
        else None
    )
    basis = max(
        (stamp for stamp in (last_activity_at, last_success_at) if stamp is not None),
        default=None,
    )
    stale = basis is not None and max(0, (now - basis).days) > stale_after_days

    provider_state = ProviderState(ai_provider_state) if ai_provider_state else None
    checks = {
        "hevy_connected": bool(has_hevy_key),
        "programme_active": bool(has_programme),
        "training_data": bool(has_training_data),
        "session_history": bool(has_session_history),
        "ai_provider": bool(has_ai_provider),
        "ai_verified": provider_state is ProviderState.VERIFIED,
    }
    missing = tuple(name for name, satisfied in checks.items() if not satisfied)

    has_history = bool(has_training_data or has_session_history)
    # A first run is about the training pipeline the user has not started.  The
    # AI provider is left out: a deployment's shared server key would otherwise
    # make every brand-new account look as if it had already begun setup.
    first_run = not (has_hevy_key or has_programme or has_history)

    operation = connector.operation if connector is not None else None
    problem = _connection_problem(connection_state, connection_tested_at, connector)

    state: SetupStateKind
    code: str | None
    reason: str
    if operation is not None:
        state = (
            SetupStateKind.CONNECTING
            if operation is OperationPhase.CONNECTING
            else SetupStateKind.SYNCING
        )
        code = operation.value
        reason = _live_reason(
            operation,
            connector.operation_started_at if connector else None,
            now,
        )
    elif connector is not None and connector.operation_stalled:
        state, code = SetupStateKind.DEGRADED, "operation_stalled"
        reason = _degraded_reason(code, connector)
    elif not has_hevy_key and (has_programme or has_history):
        state, code = SetupStateKind.DEGRADED, "credential_removed"
        reason = _degraded_reason(code, connector)
    elif not has_hevy_key:
        state, code = SetupStateKind.SETUP_REQUIRED, "hevy_connected"
        reason = _REQUIREMENT_REASONS["hevy_connected"]
    elif problem is not None:
        state, code = SetupStateKind.DEGRADED, problem
        reason = _degraded_reason(code, connector)
    elif connector is not None and connector.last_sync_failed:
        state, code = SetupStateKind.DEGRADED, "sync_failed"
        reason = _degraded_reason(code, connector)
    elif not has_programme or not has_history:
        state = SetupStateKind.SETUP_REQUIRED
        code = "programme_active" if not has_programme else (
            "training_data" if not has_training_data else "session_history"
        )
        reason = (
            _NO_WORKOUTS_REASON
            if code != "programme_active" and _synced_but_empty(connector)
            else _REQUIREMENT_REASONS[code]
        )
    elif stale:
        state, code = SetupStateKind.STALE, "data_stale"
        reason = (
            f"Your last training activity was {age_days} days ago, "
            "so this view may be out of date."
        )
    else:
        state, code, reason = SetupStateKind.READY, None, ""

    return AccountSetup(
        state=state,
        checks=checks,
        missing=missing,
        last_activity_at=last_activity_at,
        age_days=age_days,
        stale=stale,
        reason=reason,
        reason_code=code,
        first_run=first_run,
        connector=connector,
        connection_state=connection_state,
        ai_provider_state=provider_state.value if provider_state else None,
        ai_provider_source=ai_provider_source,
    )


def _synced_but_empty(connector: ConnectorStatus | None) -> bool:
    """Whether the last sync succeeded yet imported no workouts."""

    return (
        connector is not None
        and connector.last_sync_succeeded
        and connector.last_sync_records == 0
    )


@dataclass(frozen=True, slots=True)
class AiProviderStatus:
    """What this tenant can truthfully say about its AI provider."""

    provider: str
    supported: bool
    configured: bool
    source: str
    state: ProviderState
    last_tested_at: str | None = None
    #: Fixed outcome code of the last connection test (``"ok"`` or a failure
    #: code), never provider text; ``None`` when the key was never tested.
    last_test_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "supported": self.supported,
            "configured": self.configured,
            "source": self.source,
            "state": self.state.value,
            "verified": self.state is ProviderState.VERIFIED,
            "last_tested_at": self.last_tested_at,
        }


def ai_provider_status(
    *,
    user_id: str,
    db_path: str,
    server_api_key: str | None = None,
) -> AiProviderStatus:
    """Return this tenant's AI provider configuration and verification.

    The tenant's own encrypted key for their preferred provider counts, as does
    the server-side Gemini fallback (reported with ``source == "server"``, which
    carries no per-account verification).  Verification is the outcome of the
    last connectivity test of *this* tenant's stored credential; replacing the
    key clears it.  No other tenant's credential is read.
    """

    from ai_provider import PROVIDERS
    from database import get_user_api_key, get_user_preferences
    from secret_vault import SecretVault, VaultError

    try:
        prefs = get_user_preferences(user_id, db_path=db_path)
    except Exception:  # noqa: BLE001 - a missing preference row is not an error
        prefs = {}
    provider = str(prefs.get("preferred_ai") or "gemini").lower().strip()
    supported = provider in PROVIDERS
    if not supported:
        return AiProviderStatus(
            provider=provider,
            supported=False,
            configured=False,
            source="none",
            state=ProviderState.UNSUPPORTED,
        )

    try:
        record = get_user_api_key(user_id, provider, db_path=db_path)
    except Exception:  # noqa: BLE001 - decryption failures mean "not usable"
        record = None
    own_key = bool(record and str(record.get("api_key") or "").strip())

    if own_key:
        last_status: str | None = None
        last_tested_at: str | None = None
        try:
            summary = SecretVault(db_path).describe(user_id, provider)
        except (VaultError, ValueError):
            summary = None
        if summary is not None and summary.configured:
            last_status = summary.last_test_status
            last_tested_at = summary.last_tested_at
        return AiProviderStatus(
            provider=provider,
            supported=True,
            configured=True,
            source="user",
            state=derive_provider_state(
                supported=True, configured=True, last_test_status=last_status
            ),
            last_tested_at=last_tested_at,
            last_test_status=last_status,
        )

    server_key = provider == "gemini" and bool((server_api_key or "").strip())
    return AiProviderStatus(
        provider=provider,
        supported=True,
        configured=server_key,
        source="server" if server_key else "none",
        state=derive_provider_state(
            supported=True, configured=server_key, last_test_status=None
        ),
    )


def ai_provider_configured(
    *,
    user_id: str,
    db_path: str,
    server_api_key: str | None = None,
) -> bool:
    """Return whether this tenant can reach an AI provider."""

    return ai_provider_status(
        user_id=user_id,
        db_path=db_path,
        server_api_key=server_api_key,
    ).configured


def build_account_setup(
    *,
    user_id: str,
    db_path: str,
    now: datetime | None = None,
    server_ai_key: str | None = None,
) -> AccountSetup:
    """Read this tenant's records and derive the account readiness."""

    from database import (
        get_active_programme,
        get_daily_logs,
        get_hevy_connection,
        get_progress_history,
        get_session_volumes,
        get_user_api_keys,
    )

    now = now or datetime.now(tz=timezone.utc)
    keys = get_user_api_keys(user_id, db_path=db_path)
    hevy_key = str((keys.get("hevy") or {}).get("api_key") or "").strip()

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

    connection = get_hevy_connection(user_id, db_path) if hevy_key else None
    provider = ai_provider_status(
        user_id=user_id,
        db_path=db_path,
        server_api_key=server_ai_key,
    )

    return derive_account_state(
        has_hevy_key=bool(hevy_key),
        has_programme=bool(programme),
        has_training_data=bool(progress),
        has_session_history=bool(sessions or logs),
        has_ai_provider=provider.configured,
        last_activity_at=last_activity_at,
        now=now,
        connector=read_connector_status(user_id, db_path=db_path, now=now),
        connection_state=str(connection["state"]) if connection else None,
        connection_tested_at=(
            _parse_datetime(connection.get("tested_at")) if connection else None
        ),
        ai_provider_state=provider.state,
        ai_provider_source=provider.source,
    )


def surface_setup(
    account: AccountSetup,
    surface: str,
    *,
    programme_status: str | None = None,
) -> SurfaceSetup:
    """Derive the setup contract for one surface from the account readiness."""

    requirements = SURFACE_REQUIREMENTS.get(surface, ())
    unmet = [
        name
        for name in requirements
        if not account.checks.get(name, False) and not _needs_no_user_action(account, name)
    ]

    reasons: list[str] = []
    action: SetupAction | None = None
    state = account.state
    code: str | None = None

    if account.state in (SetupStateKind.CONNECTING, SetupStateKind.SYNCING):
        # The data this surface would show is being rebuilt right now: say so
        # instead of offering a setup step that would collide with it.
        reasons.append(account.reason)
        code = account.reason_code
    elif account.state is SetupStateKind.DEGRADED:
        reasons.append(account.reason)
        action = _DEGRADED_ACTIONS[account.reason_code or "sync_failed"]
        code = account.reason_code
    else:
        programme_issue = _programme_issue(programme_status, surface)
        if programme_issue is not None:
            issue_action, issue_reason, issue_code = programme_issue
            action = issue_action
            reasons.append(issue_reason)
            code = issue_code
            state = SetupStateKind.SETUP_REQUIRED
        elif unmet:
            first = unmet[0]
            action, reason = _unmet_action_and_reason(first, account)
            reasons.append(reason)
            code = first
            state = SetupStateKind.SETUP_REQUIRED
        elif account.state is SetupStateKind.STALE:
            action = SYNC_HEVY
            reasons.append(account.reason)
            code = account.reason_code
        elif account.state is SetupStateKind.SETUP_REQUIRED:
            # Setup is unfinished elsewhere, but everything this surface needs
            # is in place, so it can show its content without a setup notice.
            state = SetupStateKind.READY

    return SurfaceSetup(
        state=state,
        label=_state_label(state, first_run=account.first_run),
        reason=" ".join(reasons),
        action=action,
        missing=tuple(unmet),
        last_activity_at=account.last_activity_at,
        age_days=account.age_days,
        stale=account.stale,
        reason_code=code,
        first_run=account.first_run,
    )


def _needs_no_user_action(account: AccountSetup, requirement: str) -> bool:
    """Whether an unmet requirement is one the account cannot act on.

    A deployment's shared server key carries no per-account verification and
    cannot be tested from the account, so an "untested provider" setup step
    would point at nothing.  Coach still never *claims* it works: that comes
    from the provider state, which stays ``unverified``.  A key the account owns
    is different: testing it is one click away, so the step stays.
    """

    return (
        requirement == "ai_verified"
        and account.ai_provider_source == "server"
        and account.ai_provider_state == ProviderState.UNVERIFIED.value
    )


def _unmet_action_and_reason(
    requirement: str,
    account: AccountSetup,
) -> tuple[SetupAction, str]:
    """Return the action and reason for an unmet requirement of a surface."""

    if requirement == "ai_verified" and account.ai_provider_state == (
        ProviderState.FAILED.value
    ):
        return FIX_AI, _PROVIDER_FAILED_REASON
    if requirement in ("training_data", "session_history") and _synced_but_empty(
        account.connector
    ):
        return SYNC_HEVY, _NO_WORKOUTS_REASON
    return _REQUIREMENT_ACTIONS[requirement], _REQUIREMENT_REASONS[requirement]


def _programme_issue(
    programme_status: str | None,
    surface: str,
) -> tuple[SetupAction, str, str] | None:
    """Return the actionable programme problem for a programme-driven surface."""

    if surface not in PROGRAMME_SURFACES or not programme_status:
        return None
    if programme_status == "needs_review":
        return (
            REBUILD_PROGRAMME,
            "Your programme no longer prescribes a valid rotation.",
            "programme_needs_review",
        )
    if programme_status == "completed":
        return (
            BUILD_PROGRAMME,
            "This programme cycle has finished, so there is no workout left to prescribe.",
            "programme_completed",
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
    "FIX_AI",
    "REBUILD_PROGRAMME",
    "RECONNECT_HEVY",
    "RETRY_SYNC",
    "SETUP_ACTIONS",
    "STALE_AFTER_DAYS",
    "SURFACE_REQUIREMENTS",
    "SYNC_HEVY",
    "VERIFY_AI",
    "VERIFY_HEVY",
    "AccountSetup",
    "AiProviderStatus",
    "ProviderState",
    "SetupAction",
    "SetupStateKind",
    "SurfaceSetup",
    "ai_provider_configured",
    "ai_provider_status",
    "build_account_setup",
    "derive_account_state",
    "derive_provider_state",
    "surface_setup",
    "surface_setup_payload",
]
