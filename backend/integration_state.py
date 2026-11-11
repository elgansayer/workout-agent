"""Deterministic readiness state machine for account and connector integrations.

An integration's state is never stored. It is derived on demand from persisted
facts (credential presence and a bounded ledger of connector runs) and an
explicit clock, so every surface that evaluates the same facts at the same
``now`` reports the same state, reason and next action. The transition table,
policies and failure handling are documented in
``docs/INTEGRATION_STATE_MACHINE.md``.

This module is pure: it performs no I/O, reads no secrets and never touches the
tenant boundary. Tenant-scoped fact collection lives in ``integration_status``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from health_freshness import Freshness, classify_freshness
from health_sync import SyncState


class IntegrationState(StrEnum):
    SETUP_REQUIRED = "setup_required"
    CONNECTING = "connecting"
    SYNCING = "syncing"
    READY = "ready"
    STALE = "stale"
    DEGRADED = "degraded"


class Reason(StrEnum):
    # setup_required
    NOT_AVAILABLE = "not_available"
    CREDENTIAL_MISSING = "credential_missing"
    AUTHORIZATION_FAILED = "authorization_failed"
    CREDENTIAL_REJECTED = "credential_rejected"  # also reported as degraded
    PROGRAMME_MISSING = "programme_missing"
    # connecting
    AUTHORIZING = "authorizing"
    VERIFYING = "verifying"
    AWAITING_VERIFICATION = "awaiting_verification"
    AWAITING_FIRST_SYNC = "awaiting_first_sync"
    # syncing
    SYNC_IN_PROGRESS = "sync_in_progress"
    # ready
    UP_TO_DATE = "up_to_date"
    CONFIGURED = "configured"
    PROGRAMME_ACTIVE = "programme_active"
    # stale
    DATA_STALE = "data_stale"
    PROGRAMME_COMPLETED = "programme_completed"
    # degraded
    PARTIAL_IMPORT = "partial_import"
    PROVIDER_FAILING = "provider_failing"
    PROGRAMME_NEEDS_REVIEW = "programme_needs_review"


class NextAction(StrEnum):
    NONE = "none"
    CONNECT = "connect"
    RECONNECT = "reconnect"
    VERIFY = "verify"
    SYNC = "sync"
    BUILD_PROGRAMME = "build_programme"
    REVIEW_PROGRAMME = "review_programme"


class RunKind(StrEnum):
    AUTHORIZE = "authorize"  # OAuth / consent flow
    TEST = "test"  # credential verification or read-through probe
    REQUEST = "request"  # on-demand provider call serving a user feature
    SYNC = "sync"  # background or manual data import


RunStatus = SyncState

# Provider failures that only a new or re-authorised credential can fix.
AUTH_ERROR_CODES = frozenset({"unauthorized", "forbidden", "invalid_grant"})

# Least ready first. Used to pick the account-level state and tie-break order.
SEVERITY: tuple[IntegrationState, ...] = (
    IntegrationState.SETUP_REQUIRED,
    IntegrationState.DEGRADED,
    IntegrationState.CONNECTING,
    IntegrationState.STALE,
    IntegrationState.SYNCING,
    IntegrationState.READY,
)
# States in which a required component limits what the product can do.
BLOCKING_STATES = frozenset(
    {
        IntegrationState.SETUP_REQUIRED,
        IntegrationState.DEGRADED,
        IntegrationState.CONNECTING,
    }
)


@dataclass(frozen=True, slots=True)
class IntegrationPolicy:
    """Thresholds and capabilities that differ per connector, not per user."""

    requires_sync: bool = True  # False: used on demand, no background pipeline
    manual_sync: bool = False  # the user can start a sync from the UI
    manual_verify: bool = False  # the user can verify a stored credential
    fresh_for: timedelta = timedelta(hours=24)
    stale_after: timedelta = timedelta(hours=72)
    in_flight_ttl: timedelta = timedelta(minutes=15)
    degrade_after_failures: int = 2

    def __post_init__(self) -> None:
        if self.fresh_for > self.stale_after:
            raise ValueError("fresh_for must not exceed stale_after")
        if self.in_flight_ttl <= timedelta(0) or self.degrade_after_failures < 1:
            raise ValueError(
                "in_flight_ttl and degrade_after_failures must be positive"
            )


@dataclass(frozen=True, slots=True)
class RunFact:
    """One persisted connector run (see ``connector_runs``)."""

    kind: RunKind
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    retryable: bool = False
    fetched: int = 0
    written: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class IntegrationFacts:
    """Everything the state machine may know about one connector and tenant.

    ``runs`` should be supplied oldest first; derivation re-sorts by
    ``started_at`` with a stable sort, so ties keep the supplied order.
    """

    connector: str
    label: str
    policy: IntegrationPolicy = field(default_factory=IntegrationPolicy)
    available: bool = True
    unavailable_detail: str | None = None
    credential_present: bool = False
    credential_changed_at: datetime | None = None
    credential_verified: bool = False
    runs: tuple[RunFact, ...] = ()
    settings_href: str = "/settings"


@dataclass(frozen=True, slots=True)
class IntegrationStatus:
    connector: str
    label: str
    state: IntegrationState
    reason: Reason
    message: str
    next_action: NextAction = NextAction.NONE
    action_label: str | None = None
    action_href: str | None = None
    retryable: bool = False
    freshness: Freshness | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_code: str | None = None
    consecutive_failures: int = 0
    last_run_counts: Mapping[str, int] | None = None

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe projection shared by every API surface."""
        return {
            "connector": self.connector,
            "label": self.label,
            "state": self.state.value,
            "reason": self.reason.value,
            "message": self.message,
            "next_action": self.next_action.value,
            "action_label": self.action_label,
            "action_href": self.action_href,
            "retryable": self.retryable,
            "freshness": self.freshness.value if self.freshness else None,
            "last_attempt_at": _iso(self.last_attempt_at),
            "last_success_at": _iso(self.last_success_at),
            "last_error_code": self.last_error_code,
            "consecutive_failures": self.consecutive_failures,
            "last_run": dict(self.last_run_counts) if self.last_run_counts else None,
        }


@dataclass(frozen=True, slots=True)
class AccountStatus:
    generated_at: datetime
    state: IntegrationState
    reason: Reason
    message: str
    next_action: NextAction
    action_label: str | None
    action_href: str | None
    required: tuple[str, ...]
    blocking: tuple[str, ...]
    components: Mapping[str, IntegrationStatus]

    def to_payload(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "state": self.state.value,
            "reason": self.reason.value,
            "message": self.message,
            "next_action": self.next_action.value,
            "action_label": self.action_label,
            "action_href": self.action_href,
            "required": list(self.required),
            "blocking": list(self.blocking),
            "components": {
                name: {**status.to_payload(), "required": name in self.required}
                for name, status in self.components.items()
            },
        }


@dataclass(frozen=True, slots=True)
class Transition:
    """A change of derived state around one event, used for logs and alerts."""

    component: str
    before: IntegrationStatus
    after: IntegrationStatus

    @property
    def changed(self) -> bool:
        return (self.before.state, self.before.reason) != (
            self.after.state,
            self.after.reason,
        )


@dataclass(frozen=True, slots=True)
class NotificationDraft:
    title: str
    message: str
    link: str


# ---------------------------------------------------------------------------
# Copy. Every surface renders these strings; nothing here embeds provider text.
# ---------------------------------------------------------------------------

_MESSAGES: dict[Reason, str] = {
    Reason.NOT_AVAILABLE: "{label} is not available on this server.",
    Reason.CREDENTIAL_MISSING: "{label} is not set up yet.",
    Reason.AUTHORIZATION_FAILED: (
        "The last attempt to connect {label} was not completed."
    ),
    Reason.CREDENTIAL_REJECTED: (
        "{label} rejected your credentials. Update them to continue."
    ),
    Reason.PROGRAMME_MISSING: "Build your programme from Hevy to see your plan.",
    Reason.AUTHORIZING: "Waiting for {label} to finish authorising.",
    Reason.VERIFYING: "Checking your {label} connection.",
    Reason.AWAITING_VERIFICATION: (
        "{label} credentials are saved but have not been verified yet."
    ),
    Reason.AWAITING_FIRST_SYNC: (
        "{label} is connected. The first sync has not completed yet."
    ),
    Reason.SYNC_IN_PROGRESS: "Syncing {label}.",
    Reason.UP_TO_DATE: "{label} is up to date (last synced {ago}).",
    Reason.CONFIGURED: "{label} is configured.",
    Reason.PROGRAMME_ACTIVE: "Your programme is active.",
    Reason.DATA_STALE: "{label} was last synced {ago}. Sync again to refresh it.",
    Reason.PROGRAMME_COMPLETED: (
        "This programme is complete. Review it before starting a new programme."
    ),
    Reason.PARTIAL_IMPORT: (
        "The last {label} import was only partly saved ({written} of {fetched} "
        "records). Run the sync again."
    ),
    Reason.PROVIDER_FAILING: "{label} returned an error. Try again shortly.",
    Reason.PROGRAMME_NEEDS_REVIEW: (
        "Your programme needs review before it can be used."
    ),
}
_FAILURE_MESSAGES: dict[str, str] = {
    "rate_limited": "{label} is limiting requests. Try again shortly.",
    "provider_unavailable": "{label} is temporarily unavailable. Try again shortly.",
    "interrupted": "The last {label} sync did not finish. Run it again.",
}
_ACTION_LABELS: dict[NextAction, str] = {
    NextAction.CONNECT: "Set up {label}",
    NextAction.RECONNECT: "Update {label} credentials",
    NextAction.VERIFY: "Verify connection",
    NextAction.SYNC: "Sync now",
    NextAction.BUILD_PROGRAMME: "Open the programme builder",
    NextAction.REVIEW_PROGRAMME: "Review programme",
}
PROGRAMME_HREF = "/programmes"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ago(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        count, unit = seconds // 60, "minute"
    elif seconds < 48 * 3600:
        count, unit = seconds // 3600, "hour"
    else:
        count, unit = seconds // 86400, "day"
    return f"{count} {unit}{'' if count == 1 else 's'} ago"


def failure_message(label: str, error_code: str | None) -> str:
    """Safe, user-facing copy for one failed attempt; never provider text."""
    if error_code in AUTH_ERROR_CODES:
        return _MESSAGES[Reason.CREDENTIAL_REJECTED].format(label=label)
    template = _FAILURE_MESSAGES.get(
        error_code or "", _MESSAGES[Reason.PROVIDER_FAILING]
    )
    return template.format(label=label)


def _status(
    facts: IntegrationFacts,
    state: IntegrationState,
    reason: Reason,
    *,
    action: NextAction = NextAction.NONE,
    message: str | None = None,
    ago: str = "an unknown time ago",
    **fields: Any,
) -> IntegrationStatus:
    """Build a status, downgrading actions the connector cannot offer.

    ``message`` is used verbatim when given; otherwise the reason's template is
    filled in. Server-supplied text is therefore never treated as a format string.
    """
    policy = facts.policy
    if (action is NextAction.SYNC and not policy.manual_sync) or (
        action is NextAction.VERIFY and not policy.manual_verify
    ):
        action = NextAction.NONE
    if message is None:
        counts = fields.get("last_run_counts") or {}
        message = _MESSAGES[reason].format(
            label=facts.label,
            ago=ago,
            written=counts.get("written", 0),
            fetched=counts.get("fetched", 0),
        )
    return IntegrationStatus(
        connector=facts.connector,
        label=facts.label,
        state=state,
        reason=reason,
        message=message,
        next_action=action,
        action_label=(
            _ACTION_LABELS[action].format(label=facts.label)
            if action is not NextAction.NONE
            else None
        ),
        action_href=facts.settings_href if action is not NextAction.NONE else None,
        **fields,
    )


# ---------------------------------------------------------------------------
# Connector derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Evidence:
    finished: tuple[RunFact, ...]
    in_flight: tuple[RunFact, ...]
    data_runs: tuple[RunFact, ...]
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    streak: int
    verified: bool


def _collect_evidence(facts: IntegrationFacts, now: datetime) -> _Evidence:
    policy = facts.policy
    changed = _utc(facts.credential_changed_at) if facts.credential_changed_at else None
    finished: list[RunFact] = []
    in_flight: list[RunFact] = []
    normalised = [
        replace(
            run,
            started_at=_utc(run.started_at),
            finished_at=_utc(run.finished_at) if run.finished_at else None,
        )
        for run in facts.runs
    ]
    for run in sorted(normalised, key=lambda item: item.started_at):
        # Evidence gathered against a previous credential says nothing about
        # the one currently on file.
        if changed is not None and run.started_at < changed:
            continue
        if run.status is RunStatus.RUNNING:
            if run.kind is RunKind.REQUEST:
                continue
            if now - run.started_at >= policy.in_flight_ttl:
                # A crashed worker must not leave the connector syncing forever.
                finished.append(
                    replace(
                        run,
                        status=RunStatus.FAILED,
                        finished_at=run.started_at + policy.in_flight_ttl,
                        error_code="interrupted",
                        retryable=True,
                    )
                )
            else:
                in_flight.append(run)
        else:
            finished.append(replace(run, finished_at=run.finished_at or run.started_at))

    data_kind = RunKind.SYNC if policy.requires_sync else RunKind.REQUEST
    data_runs = tuple(run for run in finished if run.kind is data_kind)
    successes = [
        run.finished_at or run.started_at
        for run in data_runs
        if run.status is RunStatus.SUCCEEDED
    ]
    streak = 0
    for run in reversed(data_runs):
        if run.status is RunStatus.SUCCEEDED:
            break
        streak += 1
    touched = [run.finished_at or run.started_at for run in finished] + [
        run.started_at for run in in_flight
    ]
    return _Evidence(
        finished=tuple(finished),
        in_flight=tuple(in_flight),
        data_runs=data_runs,
        last_attempt_at=max(touched) if touched else None,
        last_success_at=max(successes) if successes else None,
        streak=streak,
        verified=facts.credential_verified
        or any(run.status is RunStatus.SUCCEEDED for run in finished),
    )


def _credential_rejection(
    facts: IntegrationFacts, evidence: _Evidence
) -> tuple[RunFact | None, bool]:
    """Return the unresolved auth failure (if any) and whether the credential
    had succeeded before it."""
    finished = evidence.finished
    index = max(
        (
            position
            for position, run in enumerate(finished)
            if run.status is RunStatus.FAILED and run.error_code in AUTH_ERROR_CODES
        ),
        default=None,
    )
    if index is None:
        return None, False
    if any(run.status is RunStatus.SUCCEEDED for run in finished[index + 1 :]):
        return None, False
    had_success = facts.credential_verified or any(
        run.status is RunStatus.SUCCEEDED for run in finished[:index]
    )
    return finished[index], had_success


def derive_integration_state(
    facts: IntegrationFacts, *, now: datetime
) -> IntegrationStatus:
    """Derive one connector's state. Pure and deterministic for (facts, now)."""
    now = _utc(now)
    policy = facts.policy
    evidence = _collect_evidence(facts, now)
    freshness = (
        classify_freshness(
            evidence.last_success_at,
            now=now,
            fresh_for=policy.fresh_for,
            stale_after=policy.stale_after,
        )
        if policy.requires_sync
        else None
    )
    last_data = evidence.data_runs[-1] if evidence.data_runs else None
    common: dict[str, Any] = {
        "freshness": freshness,
        "last_attempt_at": evidence.last_attempt_at,
        "last_success_at": evidence.last_success_at,
        "last_error_code": (
            last_data.error_code
            if last_data and last_data.status is not RunStatus.SUCCEEDED
            else None
        ),
        "consecutive_failures": evidence.streak,
        "last_run_counts": (
            {
                "fetched": last_data.fetched,
                "written": last_data.written,
                "skipped": last_data.skipped,
            }
            if last_data
            else None
        ),
    }

    def status(
        state: IntegrationState,
        reason: Reason,
        *,
        action: NextAction = NextAction.NONE,
        message: str | None = None,
        retryable: bool = False,
        **extra: Any,
    ) -> IntegrationStatus:
        return _status(
            facts,
            state,
            reason,
            action=action,
            message=message,
            retryable=retryable,
            **{**common, **extra},
        )

    if not facts.available:
        return status(
            IntegrationState.SETUP_REQUIRED,
            Reason.NOT_AVAILABLE,
            message=facts.unavailable_detail,
        )

    if not facts.credential_present:
        if any(run.kind is RunKind.AUTHORIZE for run in evidence.in_flight):
            return status(IntegrationState.CONNECTING, Reason.AUTHORIZING)
        authorizations = [
            run for run in evidence.finished if run.kind is RunKind.AUTHORIZE
        ]
        if authorizations and authorizations[-1].status is not RunStatus.SUCCEEDED:
            return status(
                IntegrationState.SETUP_REQUIRED,
                Reason.AUTHORIZATION_FAILED,
                action=NextAction.CONNECT,
                last_error_code=authorizations[-1].error_code,
            )
        return status(
            IntegrationState.SETUP_REQUIRED,
            Reason.CREDENTIAL_MISSING,
            action=NextAction.CONNECT,
        )

    if evidence.in_flight:
        kinds = {run.kind for run in evidence.in_flight}
        if RunKind.SYNC in kinds:
            return status(IntegrationState.SYNCING, Reason.SYNC_IN_PROGRESS)
        if RunKind.AUTHORIZE in kinds:
            return status(IntegrationState.CONNECTING, Reason.AUTHORIZING)
        return status(IntegrationState.CONNECTING, Reason.VERIFYING)

    rejection, had_success = _credential_rejection(facts, evidence)
    if rejection is not None:
        return status(
            IntegrationState.DEGRADED
            if had_success
            else IntegrationState.SETUP_REQUIRED,
            Reason.CREDENTIAL_REJECTED,
            action=NextAction.RECONNECT,
            last_error_code=rejection.error_code,
        )

    if last_data is not None and last_data.status is RunStatus.PARTIAL:
        return status(
            IntegrationState.DEGRADED,
            Reason.PARTIAL_IMPORT,
            action=NextAction.SYNC,
            retryable=True,
        )

    if last_data is not None and evidence.streak:
        absorbed = (
            last_data.retryable
            and evidence.streak < policy.degrade_after_failures
            and (not policy.requires_sync or freshness is Freshness.FRESH)
        )
        if not absorbed:
            return status(
                IntegrationState.DEGRADED,
                Reason.PROVIDER_FAILING,
                action=(
                    NextAction.SYNC
                    if policy.requires_sync
                    else (
                        NextAction.NONE if last_data.retryable else NextAction.RECONNECT
                    )
                ),
                message=failure_message(facts.label, last_data.error_code),
                retryable=last_data.retryable,
            )

    if not policy.requires_sync:
        return status(IntegrationState.READY, Reason.CONFIGURED)

    if evidence.last_success_at is None:
        if evidence.verified:
            return status(
                IntegrationState.CONNECTING,
                Reason.AWAITING_FIRST_SYNC,
                action=NextAction.SYNC,
            )
        return status(
            IntegrationState.CONNECTING,
            Reason.AWAITING_VERIFICATION,
            action=NextAction.VERIFY,
        )

    ago = _ago(now - evidence.last_success_at)
    if freshness is Freshness.STALE:
        return status(
            IntegrationState.STALE,
            Reason.DATA_STALE,
            action=NextAction.SYNC,
            ago=ago,
        )
    return status(IntegrationState.READY, Reason.UP_TO_DATE, ago=ago)


# ---------------------------------------------------------------------------
# Programme and account derivation
# ---------------------------------------------------------------------------


def derive_programme_state(
    *,
    has_active_programme: bool,
    runtime_status: str,
    runtime_message: str | None = None,
) -> IntegrationStatus:
    """Map the persisted active programme and its runtime status onto states."""
    facts = IntegrationFacts(
        connector="programme",
        label="Programme",
        settings_href=PROGRAMME_HREF,
    )
    if not has_active_programme or runtime_status == "setup_required":
        return _status(
            facts,
            IntegrationState.SETUP_REQUIRED,
            Reason.PROGRAMME_MISSING,
            action=NextAction.BUILD_PROGRAMME,
        )
    if runtime_status == "needs_review":
        return _status(
            facts,
            IntegrationState.DEGRADED,
            Reason.PROGRAMME_NEEDS_REVIEW,
            action=NextAction.REVIEW_PROGRAMME,
            message=runtime_message or _MESSAGES[Reason.PROGRAMME_NEEDS_REVIEW],
        )
    if runtime_status == "completed":
        return _status(
            facts,
            IntegrationState.STALE,
            Reason.PROGRAMME_COMPLETED,
            action=NextAction.BUILD_PROGRAMME,
            message=runtime_message or _MESSAGES[Reason.PROGRAMME_COMPLETED],
        )
    return _status(
        facts,
        IntegrationState.READY,
        Reason.PROGRAMME_ACTIVE,
        message=runtime_message or _MESSAGES[Reason.PROGRAMME_ACTIVE],
    )


def derive_account_status(
    components: Mapping[str, IntegrationStatus],
    *,
    required: Sequence[str],
    now: datetime,
) -> AccountStatus:
    """Aggregate required components: the least ready one defines the account."""
    missing = [name for name in required if name not in components]
    if missing or not required:
        raise ValueError(f"account status needs required components: {missing}")
    worst = min(
        required,
        key=lambda name: (SEVERITY.index(components[name].state), required.index(name)),
    )
    lead = components[worst]
    return AccountStatus(
        generated_at=_utc(now),
        state=lead.state,
        reason=lead.reason,
        message=lead.message,
        next_action=lead.next_action,
        action_label=lead.action_label,
        action_href=lead.action_href,
        required=tuple(required),
        blocking=tuple(
            name for name in required if components[name].state in BLOCKING_STATES
        ),
        components=dict(components),
    )


# ---------------------------------------------------------------------------
# Transition alerts
# ---------------------------------------------------------------------------


def notification_for(transition: Transition) -> NotificationDraft | None:
    """Return an alert only when the derived state meaningfully changed.

    Unchanged (state, reason) pairs never notify, so repeated failures and
    retries cannot spam the user.
    """
    if not transition.changed:
        return None
    before, after = transition.before, transition.after
    link = after.action_href or "/settings"
    needs_attention = after.state is IntegrationState.DEGRADED or (
        after.state is IntegrationState.SETUP_REQUIRED
        and after.reason is Reason.CREDENTIAL_REJECTED
    )
    if needs_attention:
        return NotificationDraft(
            f"⚠️ {after.label} needs attention", after.message, link
        )
    was_broken = before.state is IntegrationState.DEGRADED or (
        before.state is IntegrationState.SETUP_REQUIRED
        and before.reason is Reason.CREDENTIAL_REJECTED
    )
    if was_broken and after.state is IntegrationState.READY:
        return NotificationDraft(
            f"✓ {after.label} is working again", after.message, link
        )
    return None


def coach_data_notice(hevy: IntegrationStatus, *, has_local_data: bool) -> str:
    """One deterministic paragraph grounding Coach in how current its data is.

    Built only from derived state, never from user text, so it is safe to place
    in a prompt. Empty when the workout data is current enough to need no caveat.
    ``has_local_data`` says whether imported history exists locally: history
    imported before sync tracking began is real data of unknown age, not absent.
    """
    if hevy.state is IntegrationState.READY:
        return ""
    if hevy.last_success_at is None:
        if has_local_data:
            return (
                "No successful Hevy sync has been recorded, so the age of the "
                f"workout history below is unknown. Status: {hevy.message} "
                "Mention that recent sessions may be missing when it matters."
            )
        return (
            "No workout history has been imported from Hevy yet. "
            f"Status: {hevy.message} "
            "Do not invent training figures; say plainly that no data is available."
        )
    synced = _utc(hevy.last_success_at).date().isoformat()
    if hevy.state is IntegrationState.DEGRADED:
        return (
            f"Hevy syncing is failing. Status: {hevy.message} "
            f"The training data below is only current to {synced} and may miss "
            "recent sessions; tell the user."
        )
    if hevy.state is IntegrationState.STALE or hevy.freshness is Freshness.STALE:
        return (
            f"Workout history was last synced on {synced}; newer sessions may be "
            "missing. Mention this when it matters."
        )
    return ""
