"""Tenant-scoped integration status: persisted facts in, one state model out.

``integration_state`` is pure. This module reads the facts it needs for one
authenticated user (credential presence, the ``connector_runs`` ledger, the
active programme), records connector runs, and turns state changes into logs and
in-app alerts. Every surface (settings, dashboard, plan, programme builder,
coach, notifications, API) calls this module instead of inventing its own flags.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ai_provider import PROVIDERS, available_providers
from connectors.base import ConnectorState, ConnectorStatus
from connectors.errors import map_http_error
from database import (
    delete_connector_runs,
    finish_connector_run,
    get_active_programme,
    get_connector_runs,
    get_credential_presence,
    get_meta,
    get_user_by_id,
    get_user_preferences,
    record_connector_run,
    save_notification,
    start_connector_run,
)
from health_freshness import Freshness, classify_freshness
from health_observability import ConnectorMetric
from health_sync import SyncState
from integration_state import (
    AccountStatus,
    IntegrationFacts,
    IntegrationPolicy,
    IntegrationState,
    IntegrationStatus,
    NextAction,
    Reason,
    RunFact,
    RunKind,
    Transition,
    derive_account_status,
    derive_integration_state,
    derive_programme_state,
    notification_for,
)
from programme_runtime import local_today, resolve_programme_runtime

logger = logging.getLogger(__name__)

HEVY = "hevy"
GOOGLE_HEALTH = "google_health"
AI_PROVIDER = "ai_provider"
PROGRAMME = "programme"

# Required components decide the account state, in dependency order: workout
# data first, then the programme built from it, then the AI coach that explains it.
REQUIRED_COMPONENTS = (HEVY, PROGRAMME, AI_PROVIDER)

GOOGLE_HEALTH_TOKEN_KEY = "google_health_refresh_token"

HEVY_POLICY = IntegrationPolicy(manual_sync=True, manual_verify=True)
GOOGLE_HEALTH_POLICY = IntegrationPolicy()
AI_POLICY = IntegrationPolicy(requires_sync=False)

_HEVY_LABEL = "Hevy"
_GOOGLE_HEALTH_LABEL = "Google Health"


@dataclass(frozen=True, slots=True)
class StatusContext:
    """Process-level inputs the web layer supplies; nothing here is per user."""

    db_path: str
    google_health_configured: bool = False
    server_gemini_key: str | None = None


@dataclass(frozen=True, slots=True)
class RunHandle:
    """An open ledger row plus the state the user saw before it started."""

    user_id: str
    component: str
    connector: str
    kind: RunKind
    run_id: int
    started_at: datetime
    before: IntegrationStatus


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _run_facts(user_id: str, connector: str, db_path: str) -> tuple[RunFact, ...]:
    facts: list[RunFact] = []
    for row in get_connector_runs(user_id, connector, db_path):
        started = _parse(row["started_at"])
        try:
            kind, status = RunKind(row["kind"]), SyncState(row["status"])
        except ValueError:
            logger.warning("Ignoring connector run with an unknown kind or status.")
            continue
        if started is None:
            continue
        facts.append(
            RunFact(
                kind=kind,
                status=status,
                started_at=started,
                finished_at=_parse(row["finished_at"]),
                error_code=row["error_code"],
                retryable=bool(row["retryable"]),
                fetched=row["fetched"],
                written=row["written"],
                skipped=row["skipped"],
            )
        )
    return tuple(facts)


def ai_connector_id(provider: str) -> str:
    """Ledger identity of one user-selectable AI provider."""
    return f"ai:{provider.lower()}"


def _ai_selection(user_id: str, ctx: StatusContext) -> tuple[str, str]:
    """Return the user's preferred AI provider id and its display name."""
    prefs = get_user_preferences(user_id, db_path=ctx.db_path)
    provider = str(prefs.get("preferred_ai") or "gemini").lower().strip()
    if provider not in PROVIDERS:
        provider = "gemini"
    names = {item["id"]: item["name"] for item in available_providers()}
    return provider, names.get(provider, provider.title())


def _connector_id(user_id: str, component: str, ctx: StatusContext) -> str:
    if component == AI_PROVIDER:
        return ai_connector_id(_ai_selection(user_id, ctx)[0])
    if component in (HEVY, GOOGLE_HEALTH):
        return component
    raise ValueError(f"component {component!r} has no connector run ledger")


def _facts_for(user_id: str, component: str, ctx: StatusContext) -> IntegrationFacts:
    presence = get_credential_presence(user_id, ctx.db_path)
    if component == HEVY:
        return IntegrationFacts(
            connector=HEVY,
            label=_HEVY_LABEL,
            policy=HEVY_POLICY,
            credential_present=HEVY in presence,
            credential_changed_at=_parse(presence.get(HEVY)),
            runs=_run_facts(user_id, HEVY, ctx.db_path),
            settings_href="/settings?section=connections",
        )
    if component == AI_PROVIDER:
        provider, name = _ai_selection(user_id, ctx)
        has_user_key = provider in presence
        # The server-wide Gemini key is a documented fallback for Gemini only.
        has_fallback = provider == "gemini" and bool(ctx.server_gemini_key)
        return IntegrationFacts(
            connector=ai_connector_id(provider),
            label=f"AI coach ({name})",
            policy=AI_POLICY,
            credential_present=has_user_key or has_fallback,
            credential_changed_at=_parse(presence.get(provider)),
            runs=_run_facts(user_id, ai_connector_id(provider), ctx.db_path),
            settings_href="/settings?section=ai",
        )
    if component == GOOGLE_HEALTH:
        runs = _run_facts(user_id, GOOGLE_HEALTH, ctx.db_path)
        token = bool(get_meta(GOOGLE_HEALTH_TOKEN_KEY, ctx.db_path, user_id=user_id))
        authorised = [
            run.started_at
            for run in runs
            if run.kind is RunKind.AUTHORIZE and run.status is SyncState.SUCCEEDED
        ]
        return IntegrationFacts(
            connector=GOOGLE_HEALTH,
            label=_GOOGLE_HEALTH_LABEL,
            policy=GOOGLE_HEALTH_POLICY,
            available=ctx.google_health_configured,
            unavailable_detail="Google Health OAuth credentials are not set on the server.",
            credential_present=token,
            # A stored refresh token only exists after a successful code exchange.
            credential_verified=token,
            credential_changed_at=max(authorised) if authorised and token else None,
            runs=runs,
            settings_href="/settings?section=connections",
        )
    raise ValueError(f"unknown component: {component}")


def _programme_status(
    user_id: str, now: datetime, ctx: StatusContext
) -> IntegrationStatus:
    active = get_active_programme(user_id, db_path=ctx.db_path)
    user = get_user_by_id(user_id, ctx.db_path)
    timezone_name = (user.get("timezone") if user else None) or "UTC"
    # The runtime status depends only on the stored programme and the date.
    runtime = resolve_programme_runtime(
        active,
        [],
        today=local_today(timezone_name, now),
        timezone_name=timezone_name,
    )
    return derive_programme_state(
        has_active_programme=bool(active),
        runtime_status=runtime.status,
        runtime_message=runtime.message,
    )


def component_status(
    user_id: str,
    component: str,
    ctx: StatusContext,
    *,
    now: datetime | None = None,
) -> IntegrationStatus:
    """Derive one component's status for this tenant."""
    moment = _now(now)
    if component == PROGRAMME:
        return _programme_status(user_id, moment, ctx)
    return derive_integration_state(_facts_for(user_id, component, ctx), now=moment)


def build_account_status(
    user_id: str,
    ctx: StatusContext,
    *,
    now: datetime | None = None,
    programme: IntegrationStatus | None = None,
) -> AccountStatus:
    """Derive every component and the account state for the authenticated user.

    A caller that has already resolved the programme runtime for this request
    (the dashboard) passes its derived *programme* status so both agree exactly.
    """
    moment = _now(now)
    components = {
        HEVY: component_status(user_id, HEVY, ctx, now=moment),
        PROGRAMME: programme or component_status(user_id, PROGRAMME, ctx, now=moment),
        AI_PROVIDER: component_status(user_id, AI_PROVIDER, ctx, now=moment),
        GOOGLE_HEALTH: component_status(user_id, GOOGLE_HEALTH, ctx, now=moment),
    }
    return derive_account_status(components, required=REQUIRED_COMPONENTS, now=moment)


def status_from_connector(
    status: ConnectorStatus, *, label: str, now: datetime | None = None
) -> IntegrationStatus:
    """Project a registry connector's own status onto the shared state model."""
    moment = _now(now)
    success = status.last_success_at
    freshness = classify_freshness(success, now=moment) if success else None
    mapping = {
        ConnectorState.DISCONNECTED: (
            IntegrationState.SETUP_REQUIRED,
            Reason.CREDENTIAL_MISSING,
        ),
        ConnectorState.PENDING: (IntegrationState.CONNECTING, Reason.AUTHORIZING),
        ConnectorState.ATTENTION: (IntegrationState.DEGRADED, Reason.PROVIDER_FAILING),
        ConnectorState.ERROR: (IntegrationState.DEGRADED, Reason.PROVIDER_FAILING),
    }
    if status.state is ConnectorState.CONNECTED:
        if success is None:
            state, reason = IntegrationState.CONNECTING, Reason.AWAITING_FIRST_SYNC
        elif freshness is Freshness.STALE:
            state, reason = IntegrationState.STALE, Reason.DATA_STALE
        else:
            state, reason = IntegrationState.READY, Reason.UP_TO_DATE
    else:
        state, reason = mapping[status.state]
    return IntegrationStatus(
        connector=status.provider,
        label=label,
        state=state,
        reason=reason,
        message=status.message or f"{label} is {state.value.replace('_', ' ')}.",
        next_action=NextAction.NONE,
        retryable=status.retryable,
        freshness=freshness,
        last_attempt_at=status.last_attempt_at,
        last_success_at=success,
    )


# ---------------------------------------------------------------------------
# Recording connector runs
# ---------------------------------------------------------------------------


def _log_run(
    connector: str,
    kind: RunKind,
    status: SyncState,
    started: datetime,
    finished: datetime,
    records: int,
) -> None:
    duration_ms = max(0, int((finished - started).total_seconds() * 1000))
    metric = ConnectorMetric(
        connector, kind.value, status.value, duration_ms, max(0, records)
    )
    logger.info(
        "connector_run %s duration_ms=%d records=%d",
        metric.labels(),
        metric.duration_ms,
        metric.records,
    )


def _finalise(
    before: IntegrationStatus,
    after: IntegrationStatus,
    *,
    user_id: str,
    component: str,
    ctx: StatusContext,
    notify: bool,
) -> Transition:
    transition = Transition(component, before, after)
    if transition.changed:
        # Provider, states and codes only: no tenant identifiers or payloads.
        logger.info(
            "integration_state_transition connector=%s from=%s/%s to=%s/%s error_code=%s",
            after.connector,
            before.state.value,
            before.reason.value,
            after.state.value,
            after.reason.value,
            after.last_error_code,
        )
        draft = notification_for(transition) if notify else None
        if draft is not None:
            try:
                save_notification(
                    user_id,
                    draft.title,
                    draft.message,
                    type="sync",
                    link=draft.link,
                    db_path=ctx.db_path,
                )
            except Exception:  # alerts must never break the user action
                logger.exception("Could not save the integration notification.")
    return transition


def begin_run(
    user_id: str,
    component: str,
    kind: RunKind,
    ctx: StatusContext,
    *,
    now: datetime | None = None,
) -> RunHandle | None:
    """Open a ledger row. Returns ``None`` when one is already in flight."""
    moment = _now(now)
    connector = _connector_id(user_id, component, ctx)
    before = component_status(user_id, component, ctx, now=moment)
    policy = _policy_for(component)
    run_id = start_connector_run(
        user_id,
        connector,
        kind.value,
        exclusive=kind is not RunKind.AUTHORIZE,
        lease_seconds=int(policy.in_flight_ttl.total_seconds()),
        now=moment,
        db_path=ctx.db_path,
    )
    if run_id is None:
        return None
    return RunHandle(user_id, component, connector, kind, run_id, moment, before)


def open_run(
    user_id: str,
    component: str,
    kind: RunKind,
    ctx: StatusContext,
    *,
    now: datetime | None = None,
) -> RunHandle | None:
    """Find this tenant's in-flight run of *kind*, e.g. for an OAuth callback."""
    moment = _now(now)
    connector = _connector_id(user_id, component, ctx)
    for row in reversed(get_connector_runs(user_id, connector, ctx.db_path)):
        if row["kind"] == kind.value and row["status"] == "running":
            started = _parse(row["started_at"]) or moment
            before = component_status(user_id, component, ctx, now=moment)
            return RunHandle(user_id, component, connector, kind, row["id"], started, before)
    return None


def complete_run(
    handle: RunHandle,
    status: SyncState,
    ctx: StatusContext,
    *,
    error_code: str | None = None,
    retryable: bool = False,
    fetched: int = 0,
    written: int = 0,
    skipped: int = 0,
    notify: bool = True,
    now: datetime | None = None,
) -> Transition:
    """Close a run and report the resulting transition (idempotent)."""
    moment = _now(now)
    closed = finish_connector_run(
        handle.user_id,
        handle.run_id,
        status.value,
        error_code=error_code,
        retryable=retryable,
        fetched=fetched,
        written=written,
        skipped=skipped,
        now=moment,
        db_path=ctx.db_path,
    )
    after = component_status(handle.user_id, handle.component, ctx, now=moment)
    if not closed:
        # Duplicate or late completion: report state, change nothing, alert nobody.
        return Transition(handle.component, after, after)
    _log_run(handle.connector, handle.kind, status, handle.started_at, moment, written)
    return _finalise(
        handle.before,
        after,
        user_id=handle.user_id,
        component=handle.component,
        ctx=ctx,
        notify=notify,
    )


def record_run(
    user_id: str,
    component: str,
    kind: RunKind,
    status: SyncState,
    ctx: StatusContext,
    *,
    error_code: str | None = None,
    retryable: bool = False,
    fetched: int = 0,
    written: int = 0,
    skipped: int = 0,
    notify: bool = True,
    now: datetime | None = None,
) -> Transition:
    """Record a finished attempt (tests, on-demand requests) in one step."""
    moment = _now(now)
    connector = _connector_id(user_id, component, ctx)
    before = component_status(user_id, component, ctx, now=moment)
    record_connector_run(
        user_id,
        connector,
        kind.value,
        status.value,
        error_code=error_code,
        retryable=retryable,
        fetched=fetched,
        written=written,
        skipped=skipped,
        now=moment,
        db_path=ctx.db_path,
    )
    after = component_status(user_id, component, ctx, now=moment)
    _log_run(connector, kind, status, moment, moment, written)
    return _finalise(
        before, after, user_id=user_id, component=component, ctx=ctx, notify=notify
    )


def forget_connector_history(user_id: str, connector: str, ctx: StatusContext) -> int:
    """Drop a disconnected connector's run history for this tenant."""
    return delete_connector_runs(user_id, connector, ctx.db_path)


def _policy_for(component: str) -> IntegrationPolicy:
    return {
        HEVY: HEVY_POLICY,
        GOOGLE_HEALTH: GOOGLE_HEALTH_POLICY,
        AI_PROVIDER: AI_POLICY,
    }[component]


def classify_provider_error(exc: BaseException) -> tuple[str, bool]:
    """Map an SDK/HTTP exception to ``(error_code, retryable)`` without its text.

    Provider messages may echo request details, so only the HTTP-like status
    (``status_code`` or ``code``) and the exception class name are inspected.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    if isinstance(status, int) and 400 <= status <= 599:
        mapped = map_http_error(status, provider="provider")
        return mapped.code, mapped.retryable
    name = type(exc).__name__.lower()
    if any(token in name for token in ("timeout", "connection", "unavailable")):
        return "provider_unavailable", True
    return "provider_error", True
