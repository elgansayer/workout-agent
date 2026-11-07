#!/usr/bin/env python3
"""Reference implementation of the autonomous implementation queue contract.

The OpenHands Agent Canvas on the VPS is the single autonomous scheduler for this
repository. It is the only component allowed to select implementation work from the
queue. GitHub Actions stay deterministic verification: they lint, type-check, test,
build and enforce policy, but they never select, claim or implement an issue.

This module implements the contract documented in
``docs/AUTONOMOUS_QUEUE_CONTRACT.md``:

* eligible labels decide which GitHub issues may be selected;
* claims are atomic and leased, so a failed worker cannot strand an issue;
* heartbeats renew a lease and are fenced by a lease token;
* cancellation is terminal and releases the lease;
* branch naming, pull-request creation and review handling are deterministic.

The ledger stores repository metadata only: issue numbers, titles, worker identities
and lease bookkeeping. It never stores product, tenant, health or credential data.

Every state-changing command fails closed inside GitHub Actions, so the queue can
never be driven from a CI runner.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ELIGIBLE_LABEL = "agent:queued"
CLAIMED_LABEL = "agent:in-progress"
REVIEW_LABEL = "agent:review"
BLOCKED_LABEL = "agent:blocked"
CANCELLED_LABEL = "agent:cancelled"

CONTRACT_LABELS: tuple[str, ...] = (
    ELIGIBLE_LABEL,
    CLAIMED_LABEL,
    REVIEW_LABEL,
    BLOCKED_LABEL,
    CANCELLED_LABEL,
)

PRIORITY_LABELS: tuple[str, ...] = (
    "priority:high",
    "priority:medium",
    "priority:low",
)

PR_TITLE_TYPES: tuple[str, ...] = (
    "feat",
    "fix",
    "chore",
    "docs",
    "refactor",
    "test",
    "perf",
    "build",
    "ci",
)

BRANCH_PREFIX = "agent/issue-"
BRANCH_SLUG_LIMIT = 48

QUEUE_ENVIRONMENT_VARIABLES: tuple[str, ...] = (
    "AGENT_QUEUE_DB",
    "AGENT_QUEUE_LEASE_SECONDS",
    "AGENT_QUEUE_HEARTBEAT_SECONDS",
    "AGENT_QUEUE_MAX_ATTEMPTS",
    "AGENT_QUEUE_ELIGIBLE_LABEL",
    "GITHUB_REPOSITORY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)

DEFAULT_LEASE_SECONDS = 1800
DEFAULT_HEARTBEAT_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 3

# Commands that change queue state. Only the OpenHands VPS scheduler may run
# them, and every one of them fails closed when ``GITHUB_ACTIONS`` is set.
STATE_CHANGING_COMMANDS: tuple[str, ...] = (
    "sync",
    "claim",
    "heartbeat",
    "pr",
    "review",
    "cancel",
    "requeue",
    "done",
)

# Commands that only read the ledger. They are safe to run anywhere.
READ_ONLY_COMMANDS: tuple[str, ...] = ("status",)

STATE_PENDING = "pending"
STATE_CLAIMED = "claimed"
STATE_REVIEW = "review"
STATE_BLOCKED = "blocked"
STATE_CANCELLED = "cancelled"
STATE_DONE = "done"

TERMINAL_STATES: tuple[str, ...] = (STATE_BLOCKED, STATE_CANCELLED, STATE_DONE)

ACTION_REQUEUE = "requeue"
ACTION_BLOCK = "block"
ACTION_AWAIT_MERGE = "await_merge"
ACTION_HOLD = "hold"

CHECK_SUCCESS = "success"
CHECK_FAILURE = "failure"

# Verification outcomes reported by GitHub for a pull request.
_CHECK_FAILURE_STATES = frozenset(
    {
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "startup_failure",
        "error",
    }
)
_CHECK_PENDING_STATES = frozenset(
    {"pending", "in_progress", "queued", "waiting", "requested"}
)
_CHECK_SUCCESS_STATES = frozenset({"success", "neutral", "skipped"})

GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "workout-agent-agent-queue/1"

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER_PATH = REPO_ROOT / "data" / "agent_queue.db"


class QueueError(RuntimeError):
    """Raised when the queue contract is violated by a caller."""


@dataclass(frozen=True)
class QueuePolicy:
    """Tunable parts of the queue contract."""

    lease_seconds: int = DEFAULT_LEASE_SECONDS
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    eligible_label: str = ELIGIBLE_LABEL

    def validate(self) -> list[str]:
        """Return the contract violations of this policy."""

        problems: list[str] = []
        if self.lease_seconds <= 0:
            problems.append("lease_seconds must be positive")
        if self.heartbeat_seconds <= 0:
            problems.append("heartbeat_seconds must be positive")
        if self.heartbeat_seconds >= self.lease_seconds:
            problems.append("heartbeat_seconds must be shorter than lease_seconds")
        elif self.lease_seconds < 3 * self.heartbeat_seconds:
            problems.append("lease_seconds must be at least three heartbeat intervals")
        if self.max_attempts < 1:
            problems.append("max_attempts must be at least 1")
        if not self.eligible_label.strip():
            problems.append("eligible_label must not be empty")
        return problems

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> QueuePolicy:
        """Build a policy from environment variables, falling back to defaults."""

        env = queue_environment() if environ is None else environ

        def _int(name: str, fallback: int) -> int:
            raw = env.get(name, "").strip()
            if not raw:
                return fallback
            try:
                return int(raw)
            except ValueError:
                return fallback

        return cls(
            lease_seconds=_int("AGENT_QUEUE_LEASE_SECONDS", DEFAULT_LEASE_SECONDS),
            heartbeat_seconds=_int(
                "AGENT_QUEUE_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS
            ),
            max_attempts=_int("AGENT_QUEUE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
            eligible_label=env.get("AGENT_QUEUE_ELIGIBLE_LABEL", "").strip()
            or ELIGIBLE_LABEL,
        )


def queue_environment() -> dict[str, str]:
    """Return the queue settings that are safe to expose.

    Secret values are never returned. The GitHub token is read straight from
    the process environment by :func:`github_token`, so no caller can log,
    serialise or cache it by accident. The variable names are still declared
    with literal lookups so the repository inventory can discover them.
    """

    return {
        "AGENT_QUEUE_LEASE_SECONDS": os.environ.get("AGENT_QUEUE_LEASE_SECONDS", ""),
        "AGENT_QUEUE_HEARTBEAT_SECONDS": os.environ.get(
            "AGENT_QUEUE_HEARTBEAT_SECONDS", ""
        ),
        "AGENT_QUEUE_MAX_ATTEMPTS": os.environ.get("AGENT_QUEUE_MAX_ATTEMPTS", ""),
        "AGENT_QUEUE_ELIGIBLE_LABEL": os.environ.get("AGENT_QUEUE_ELIGIBLE_LABEL", ""),
        "AGENT_QUEUE_DB": os.environ.get("AGENT_QUEUE_DB", ""),
        "GITHUB_REPOSITORY": os.environ.get("GITHUB_REPOSITORY", ""),
    }


def github_token(environ: Mapping[str, str] | None = None) -> str:
    """Return the GitHub token used to talk to the API.

    The token is read here rather than through :func:`queue_environment` so it
    never becomes part of a settings mapping that could be printed or cached.
    """

    if environ is None:
        token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
    else:
        token = environ.get("GITHUB_TOKEN", "") or environ.get("GH_TOKEN", "")
    token = token.strip()
    if not token:
        raise QueueError("GITHUB_TOKEN or GH_TOKEN is required to talk to GitHub")
    return token


@dataclass(frozen=True)
class Claim:
    """A leased implementation item."""

    issue_number: int
    title: str
    worker_id: str
    lease_token: str
    lease_expires_at: float
    attempts: int
    priority: int
    branch: str


def label_names(issue: Mapping[str, Any]) -> set[str]:
    """Return the label names of a GitHub issue payload."""

    names: set[str] = set()
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, Mapping) else label
        if isinstance(name, str) and name:
            names.add(name)
    return names


def priority_of(labels: Iterable[str]) -> int:
    """Return the sort priority of an issue, lowest value first."""

    names = set(labels)
    for index, label in enumerate(PRIORITY_LABELS):
        if label in names:
            return index
    return len(PRIORITY_LABELS)


def is_eligible_issue(
    issue: Mapping[str, Any], *, eligible_label: str = ELIGIBLE_LABEL
) -> bool:
    """Return whether a GitHub issue may be selected for implementation.

    Only open issues carry the eligible label. Blocked and cancelled issues are
    never selectable, and pull requests are not implementation work.
    """

    if issue.get("pull_request"):
        return False
    if str(issue.get("state", "")).strip().lower() != "open":
        return False
    labels = label_names(issue)
    if eligible_label not in labels:
        return False
    return not labels.intersection({BLOCKED_LABEL, CANCELLED_LABEL})


def slugify(text: str, *, limit: int = BRANCH_SLUG_LIMIT) -> str:
    """Return a deterministic, git-safe slug for an issue title."""

    cleaned = "".join(char if char.isalnum() else " " for char in text.lower())
    slug = "-".join(part for part in cleaned.split() if part)
    slug = slug[:limit].rstrip("-")
    return slug or "issue"


def branch_name(issue_number: int, title: str) -> str:
    """Return the reserved branch name for an implementation item."""

    return f"{BRANCH_PREFIX}{issue_number}-{slugify(title)}"


def pull_request_title(summary: str, issue_number: int, change_type: str = "chore") -> str:
    """Return the canonical pull-request title for an implementation item."""

    if change_type not in PR_TITLE_TYPES:
        raise QueueError(
            f"change_type must be one of {', '.join(PR_TITLE_TYPES)}, got {change_type!r}"
        )
    text = " ".join(summary.split())
    return f"{change_type}: {text} (#{issue_number})"


def pull_request_body(
    summary: str,
    issue_number: int,
    *,
    changes: Sequence[str] = (),
    testing: Sequence[str] = (),
) -> str:
    """Return the canonical pull-request body for an implementation item."""

    lines = [
        "## Summary",
        summary.strip() or f"Implements #{issue_number}.",
        "",
        "## Related Issue",
        f"Fixes #{issue_number}",
        "",
        "## Changes",
    ]
    lines.extend(f"- {entry}" for entry in changes)
    if not changes:
        lines.append("- (none recorded)")
    lines.extend(["", "## Testing"])
    lines.extend(f"- {entry}" for entry in testing)
    if not testing:
        lines.append("- (none recorded)")
    return "\n".join(lines).rstrip() + "\n"


def label_transitions(state: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the (add, remove) contract labels for a queue state."""

    transitions: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
        STATE_PENDING: ((ELIGIBLE_LABEL,), ()),
        STATE_CLAIMED: ((CLAIMED_LABEL,), (ELIGIBLE_LABEL,)),
        STATE_REVIEW: ((REVIEW_LABEL,), (CLAIMED_LABEL,)),
        STATE_BLOCKED: ((BLOCKED_LABEL,), (CLAIMED_LABEL, ELIGIBLE_LABEL)),
        STATE_CANCELLED: ((CANCELLED_LABEL,), (CLAIMED_LABEL, ELIGIBLE_LABEL)),
        STATE_DONE: ((), (CLAIMED_LABEL, REVIEW_LABEL, ELIGIBLE_LABEL)),
    }
    if state not in transitions:
        raise QueueError(f"unknown queue state {state!r}")
    return transitions[state]


def _check_state(entry: Mapping[str, Any]) -> str | None:
    """Return the reported state of a single check run or commit status."""

    for key in ("conclusion", "state", "status"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
    return None


def checks_conclusion(pull_request: Mapping[str, Any]) -> str | None:
    """Return the verification conclusion of a pull request.

    ``"success"`` is returned only when every reported check succeeded, and
    ``"failure"`` when any check failed. ``None`` means the checks are unknown
    or still running, which the review contract treats as a hold: the scheduler
    never declares a pull request merge-ready without confirmed checks.
    """

    rollup = pull_request.get("status_check_rollup")
    if not isinstance(rollup, list) or not rollup:
        return None
    states: set[str] = set()
    for entry in rollup:
        if not isinstance(entry, Mapping):
            continue
        state = _check_state(entry)
        if state:
            states.add(state)
    if not states:
        return None
    if states & _CHECK_FAILURE_STATES:
        return CHECK_FAILURE
    if states & _CHECK_PENDING_STATES:
        return None
    if states <= _CHECK_SUCCESS_STATES:
        return CHECK_SUCCESS
    return None


def review_action(
    review_state: str,
    *,
    checks_conclusion: str | None = None,
    attempts: int = 0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    """Return the queue action for a pull-request review outcome.

    ``attempts`` is the number of implementation attempts already spent on the
    item. A change request requeues the item while attempts remain and blocks it
    for human attention once they are exhausted. An approval makes the item ready
    for a human merge **only** when the verification checks succeeded; failed or
    unknown checks hold the item so it is re-checked. The scheduler never merges
    or approves its own work.
    """

    state = (review_state or "").strip().upper()
    if state in {"CHANGES_REQUESTED", "CLOSED"}:
        return ACTION_BLOCK if attempts >= max_attempts else ACTION_REQUEUE
    if state == "APPROVED":
        if checks_conclusion == CHECK_SUCCESS:
            return ACTION_AWAIT_MERGE
        return ACTION_HOLD
    return ACTION_HOLD


class QueueLedger:
    """Durable ledger of implementation items and their leases.

    The ledger is repository metadata only. It holds issue numbers, titles, worker
    identities and lease bookkeeping; it never holds product, tenant, health or
    credential data.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_LEDGER_PATH,
        policy: QueuePolicy | None = None,
    ) -> None:
        self.path = Path(path)
        self.policy = policy or QueuePolicy()
        problems = self.policy.validate()
        if problems:
            raise QueueError("invalid queue policy: " + "; ".join(problems))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextlib.contextmanager
    def _immediate(self, connection: sqlite3.Connection) -> Iterator[None]:
        """Run a write transaction that serialises concurrent claimers."""

        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def _initialise(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_items (
                    issue_number INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    pull_request_number INTEGER,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_items_state "
                "ON queue_items (state, priority, issue_number)"
            )
        finally:
            connection.close()

    def _next_state(self, attempts: int) -> str:
        """Return the state an item takes when it returns to the queue."""

        return STATE_BLOCKED if attempts >= self.policy.max_attempts else STATE_PENDING

    def register(self, issue_number: int, title: str, *, priority: int = 0) -> str:
        """Register an eligible issue and return its ledger state.

        Terminal items are never revived by a later registration, so a cancelled
        or blocked issue stays out of the queue.
        """

        if issue_number <= 0:
            raise QueueError("issue_number must be positive")
        connection = self._connect()
        try:
            with self._immediate(connection):
                row = connection.execute(
                    "SELECT state FROM queue_items WHERE issue_number = ?", (issue_number,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO queue_items "
                        "(issue_number, title, state, priority, attempts, updated_at) "
                        "VALUES (?, ?, ?, ?, 0, ?)",
                        (issue_number, title, STATE_PENDING, priority, time.time()),
                    )
                    return STATE_PENDING
                state = str(row["state"])
                if state in TERMINAL_STATES:
                    return state
                connection.execute(
                    "UPDATE queue_items SET title = ?, priority = ?, updated_at = ? "
                    "WHERE issue_number = ?",
                    (title, priority, time.time(), issue_number),
                )
                return state
        finally:
            connection.close()

    def register_issues(self, issues: Iterable[Mapping[str, Any]]) -> dict[int, str]:
        """Register every eligible issue from a GitHub issue payload."""

        states: dict[int, str] = {}
        for issue in issues:
            if not is_eligible_issue(issue, eligible_label=self.policy.eligible_label):
                continue
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            states[number] = self.register(
                number,
                str(issue.get("title") or ""),
                priority=priority_of(label_names(issue)),
            )
        return states

    def claim(self, worker_id: str, *, now: float | None = None) -> Claim | None:
        """Atomically claim the next eligible item for a worker.

        The claim runs inside an immediate transaction, so two workers polling the
        same ledger can never take the same issue. Items whose lease expired are
        released first, which makes a crashed worker recoverable.
        """

        if not worker_id.strip():
            raise QueueError("worker_id must not be empty")
        moment = time.time() if now is None else now
        connection = self._connect()
        try:
            with self._immediate(connection):
                self._expire_leases(connection, moment)
                row = connection.execute(
                    "SELECT issue_number, title, attempts, priority FROM queue_items "
                    "WHERE state = ? AND attempts < ? "
                    "ORDER BY priority ASC, issue_number ASC LIMIT 1",
                    (STATE_PENDING, self.policy.max_attempts),
                ).fetchone()
                if row is None:
                    return None
                token = uuid.uuid4().hex
                expires_at = moment + self.policy.lease_seconds
                updated = connection.execute(
                    "UPDATE queue_items SET state = ?, worker_id = ?, lease_token = ?, "
                    "lease_expires_at = ?, attempts = attempts + 1, updated_at = ? "
                    "WHERE issue_number = ? AND state = ?",
                    (
                        STATE_CLAIMED,
                        worker_id,
                        token,
                        expires_at,
                        moment,
                        row["issue_number"],
                        STATE_PENDING,
                    ),
                ).rowcount
                if updated != 1:
                    return None
                issue_number = int(row["issue_number"])
                title = str(row["title"])
                attempts = int(row["attempts"]) + 1
                priority = int(row["priority"])
        finally:
            connection.close()
        return Claim(
            issue_number=issue_number,
            title=title,
            worker_id=worker_id,
            lease_token=token,
            lease_expires_at=expires_at,
            attempts=attempts,
            priority=priority,
            branch=branch_name(issue_number, title),
        )

    def _expire_leases(self, connection: sqlite3.Connection, moment: float) -> list[int]:
        """Return leases whose deadline passed to the pending or blocked state."""

        rows = connection.execute(
            "SELECT issue_number, attempts FROM queue_items "
            "WHERE state = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
            (STATE_CLAIMED, moment),
        ).fetchall()
        expired: list[int] = []
        for row in rows:
            attempts = int(row["attempts"])
            connection.execute(
                "UPDATE queue_items SET state = ?, worker_id = NULL, "
                "lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE issue_number = ? AND state = ?",
                (
                    self._next_state(attempts),
                    moment,
                    row["issue_number"],
                    STATE_CLAIMED,
                ),
            )
            expired.append(int(row["issue_number"]))
        return expired

    def heartbeat(
        self,
        issue_number: int,
        worker_id: str,
        lease_token: str,
        *,
        now: float | None = None,
    ) -> float | None:
        """Renew a held lease and return its new deadline.

        ``None`` is returned when the caller no longer holds an unexpired lease.
        That fences a stalled worker which woke up after its lease expired: it must
        not resurrect a claim another worker may already have taken.
        """

        moment = time.time() if now is None else now
        connection = self._connect()
        try:
            expires_at = moment + self.policy.lease_seconds
            updated = connection.execute(
                "UPDATE queue_items SET lease_expires_at = ?, updated_at = ? "
                "WHERE issue_number = ? AND state = ? AND worker_id = ? "
                "AND lease_token = ? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at > ?",
                (
                    expires_at,
                    moment,
                    issue_number,
                    STATE_CLAIMED,
                    worker_id,
                    lease_token,
                    moment,
                ),
            ).rowcount
            return expires_at if updated == 1 else None
        finally:
            connection.close()

    def verify_lease(
        self,
        issue_number: int,
        worker_id: str,
        lease_token: str,
        *,
        now: float | None = None,
    ) -> Claim:
        """Return the held claim, or fail closed when the caller lost the lease.

        The check is what stops a fenced worker from creating a pull request for
        work another worker may already own.
        """

        moment = time.time() if now is None else now
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT issue_number, title, worker_id, lease_token, lease_expires_at, "
                "attempts, priority FROM queue_items "
                "WHERE issue_number = ? AND state = ? AND worker_id = ? "
                "AND lease_token = ? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at > ?",
                (
                    issue_number,
                    STATE_CLAIMED,
                    worker_id,
                    lease_token,
                    moment,
                ),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise QueueError(
                f"issue #{issue_number} is not leased to {worker_id}; "
                "the lease expired or was taken over"
            )
        issue = int(row["issue_number"])
        title = str(row["title"])
        return Claim(
            issue_number=issue,
            title=title,
            worker_id=str(row["worker_id"]),
            lease_token=str(row["lease_token"]),
            lease_expires_at=float(row["lease_expires_at"]),
            attempts=int(row["attempts"]),
            priority=int(row["priority"]),
            branch=branch_name(issue, title),
        )

    def cancel(self, issue_number: int, *, reason: str = "") -> bool:
        """Cancel an item and release its lease."""

        connection = self._connect()
        try:
            updated = connection.execute(
                "UPDATE queue_items SET state = ?, worker_id = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE issue_number = ? AND state NOT IN (?, ?)",
                (
                    STATE_CANCELLED,
                    time.time(),
                    issue_number,
                    STATE_CANCELLED,
                    STATE_DONE,
                ),
            ).rowcount
            return updated == 1
        finally:
            connection.close()

    def requeue(self, issue_number: int, *, reason: str = "") -> str:
        """Return an item to the pending state after a review outcome."""

        connection = self._connect()
        try:
            with self._immediate(connection):
                row = connection.execute(
                    "SELECT state, attempts FROM queue_items WHERE issue_number = ?",
                    (issue_number,),
                ).fetchone()
                if row is None:
                    raise QueueError(f"issue #{issue_number} is not tracked")
                state = str(row["state"])
                if state in TERMINAL_STATES:
                    return state
                attempts = int(row["attempts"])
                new_state = self._next_state(attempts)
                connection.execute(
                    "UPDATE queue_items SET state = ?, worker_id = NULL, "
                    "lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                    "WHERE issue_number = ?",
                    (new_state, time.time(), issue_number),
                )
                return new_state
        finally:
            connection.close()

    def mark_review(
        self,
        issue_number: int,
        worker_id: str,
        lease_token: str,
        pull_request_number: int | None = None,
        *,
        now: float | None = None,
    ) -> bool:
        """Move a claimed item into the review state.

        Only the holder of a live lease may move an item, so a worker whose
        lease expired cannot hand the item over to the review state.
        """

        moment = time.time() if now is None else now
        connection = self._connect()
        try:
            updated = connection.execute(
                "UPDATE queue_items SET state = ?, pull_request_number = ?, updated_at = ? "
                "WHERE issue_number = ? AND state = ? AND worker_id = ? AND lease_token = ? "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (
                    STATE_REVIEW,
                    pull_request_number,
                    time.time(),
                    issue_number,
                    STATE_CLAIMED,
                    worker_id,
                    lease_token,
                    moment,
                ),
            ).rowcount
            return updated == 1
        finally:
            connection.close()

    def mark_done(self, issue_number: int) -> bool:
        """Mark a reviewed item as done.

        Only an item that reached the review state can be closed. An item that is
        still queued or claimed must go through the normal flow so the queue never
        loses track of work.
        """

        connection = self._connect()
        try:
            updated = connection.execute(
                "UPDATE queue_items SET state = ?, updated_at = ? "
                "WHERE issue_number = ? AND state = ?",
                (STATE_DONE, time.time(), issue_number, STATE_REVIEW),
            ).rowcount
            return updated == 1
        finally:
            connection.close()

    def status(self) -> list[dict[str, Any]]:
        """Return every tracked item with its current state."""

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT issue_number, title, state, priority, attempts, worker_id, "
                "lease_expires_at, pull_request_number FROM queue_items "
                "ORDER BY priority ASC, issue_number ASC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()


class GitHubClient:
    """Minimal GitHub REST client for the queue contract operations."""

    def __init__(
        self,
        token: str,
        repository: str,
        *,
        api_url: str = GITHUB_API,
        transport: Any | None = None,
    ) -> None:
        if not token.strip():
            raise QueueError("a GitHub token is required")
        if "/" not in repository.strip("/"):
            raise QueueError("repository must be given as owner/name")
        self.token = token
        self.repository = repository.strip("/")
        self.api_url = api_url.rstrip("/")
        self.transport = transport

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        expected: Sequence[int] = (200,),
    ) -> Any:
        url = f"{self.api_url}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        if self.transport is not None:
            status, raw = self.transport(method, url, body)
        else:
            status, raw = self._http_request(method, url, body)
        if status not in expected:
            raise QueueError(f"GitHub {method} {path} returned status {status}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _http_request(self, method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as error:
            return int(error.code), error.read()

    def list_open_issues(self, *, pages: int = 10) -> list[dict[str, Any]]:
        """Return the open issues of the repository."""

        issues: list[dict[str, Any]] = []
        for page in range(1, max(pages, 1) + 1):
            chunk = self._request(
                "GET",
                f"/repos/{self.repository}/issues?state=open&per_page=100&page={page}",
            )
            if not isinstance(chunk, list):
                raise QueueError("GitHub issues endpoint returned a non-list payload")
            issues.extend(item for item in chunk if isinstance(item, dict))
            if len(chunk) < 100:
                break
        return issues

    def add_labels(self, issue_number: int, labels: Sequence[str]) -> None:
        """Add labels to an issue."""

        if not labels:
            return
        self._request(
            "POST",
            f"/repos/{self.repository}/issues/{issue_number}/labels",
            {"labels": list(labels)},
            expected=(200, 201),
        )

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a single label from an issue."""

        self._request(
            "DELETE",
            f"/repos/{self.repository}/issues/{issue_number}/labels/{label}",
            expected=(200, 204, 404),
        )

    def apply_labels(
        self,
        issue_number: int,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
    ) -> None:
        """Apply the contract label transition to an issue."""

        if add:
            self.add_labels(issue_number, add)
        for label in remove:
            self.remove_label(issue_number, label)

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> int:
        """Open a pull request and return its number."""

        payload = self._request(
            "POST",
            f"/repos/{self.repository}/pulls",
            {"title": title, "body": body, "head": head, "base": base},
            expected=(201,),
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("number"), int):
            raise QueueError("GitHub did not return a pull-request number")
        return int(payload["number"])

    def list_reviews(self, pull_request_number: int) -> list[dict[str, Any]]:
        """Return the reviews of a pull request."""

        reviews = self._request(
            "GET", f"/repos/{self.repository}/pulls/{pull_request_number}/reviews"
        )
        if not isinstance(reviews, list):
            raise QueueError("GitHub reviews endpoint returned a non-list payload")
        return [item for item in reviews if isinstance(item, dict)]

    def get_pull_request(self, pull_request_number: int) -> dict[str, Any]:
        """Return a pull request payload, including its verification rollup."""

        payload = self._request(
            "GET", f"/repos/{self.repository}/pulls/{pull_request_number}"
        )
        if not isinstance(payload, dict):
            raise QueueError("GitHub pull request endpoint returned a non-object payload")
        return payload

    def pull_request_checks_conclusion(self, pull_request_number: int) -> str | None:
        """Return the confirmed verification conclusion of a pull request."""

        return checks_conclusion(self.get_pull_request(pull_request_number))

    def latest_review_state(self, pull_request_number: int) -> str | None:
        """Return the state of the most recent submitted review."""

        states = [
            str(review.get("state", "")).strip().upper()
            for review in self.list_reviews(pull_request_number)
            if str(review.get("state", "")).strip().upper() not in {"PENDING", ""}
        ]
        return states[-1] if states else None


def guard_not_github_actions(environ: Mapping[str, str] | None = None) -> None:
    """Fail closed when a queue command runs inside GitHub Actions."""

    env = os.environ if environ is None else environ
    flag = env.get("GITHUB_ACTIONS", "").strip().lower()
    if flag in {"true", "1"}:
        raise QueueError(
            "GitHub Actions must not select implementation work; "
            "the OpenHands VPS scheduler is the single autonomous scheduler"
        )


def github_client(repository: str) -> GitHubClient:
    """Return a GitHub client for the queue commands."""

    return GitHubClient(github_token(), repository)


def _repository(explicit: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    if explicit is not None:
        repository = explicit.strip()
    elif environ is None:
        repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    else:
        repository = environ.get("GITHUB_REPOSITORY", "").strip()
    if not repository:
        raise QueueError("GITHUB_REPOSITORY is required to talk to GitHub")
    return repository


def _ledger_path(
    explicit: Path | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    env = queue_environment() if environ is None else environ
    if explicit is not None:
        return explicit
    configured = env.get("AGENT_QUEUE_DB", "").strip()
    return Path(configured) if configured else DEFAULT_LEDGER_PATH


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autonomous implementation queue.")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to the queue ledger (default: AGENT_QUEUE_DB or data/agent_queue.db).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="Register eligible GitHub issues.")
    sync.add_argument("--repo", default=None, help="owner/name of the repository.")

    claim = subparsers.add_parser("claim", help="Claim the next eligible item.")
    claim.add_argument("--worker", required=True, help="Worker identity taking the lease.")
    claim.add_argument(
        "--apply-labels",
        action="store_true",
        help="Mirror the claim label transition on GitHub.",
    )
    claim.add_argument("--repo", default=None, help="owner/name of the repository.")

    heartbeat = subparsers.add_parser("heartbeat", help="Renew a held lease.")
    heartbeat.add_argument("--worker", required=True)
    heartbeat.add_argument("--issue", type=int, required=True)
    heartbeat.add_argument("--token", required=True, help="Lease token from the claim.")

    pull_request = subparsers.add_parser("pr", help="Open the pull request for a claim.")
    pull_request.add_argument("--issue", type=int, required=True)
    pull_request.add_argument("--worker", required=True)
    pull_request.add_argument("--token", required=True)
    pull_request.add_argument("--summary", required=True)
    pull_request.add_argument("--type", default="chore", choices=list(PR_TITLE_TYPES))
    pull_request.add_argument("--base", default="main")
    pull_request.add_argument("--repo", default=None)

    review = subparsers.add_parser("review", help="Apply a pull-request review outcome.")
    review.add_argument("--issue", type=int, required=True)
    review.add_argument("--pull-request", type=int, required=True)
    review.add_argument("--repo", default=None)
    review.add_argument("--apply-labels", action="store_true")

    cancel = subparsers.add_parser("cancel", help="Cancel an item.")
    cancel.add_argument("--issue", type=int, required=True)
    cancel.add_argument("--reason", default="")
    cancel.add_argument("--repo", default=None, help="owner/name of the repository.")
    cancel.add_argument("--apply-labels", action="store_true")

    requeue = subparsers.add_parser("requeue", help="Return an item to the queue.")
    requeue.add_argument("--issue", type=int, required=True)
    requeue.add_argument("--reason", default="")

    done = subparsers.add_parser("done", help="Mark a reviewed item as done.")
    done.add_argument("--issue", type=int, required=True)
    done.add_argument("--repo", default=None, help="owner/name of the repository.")
    done.add_argument("--apply-labels", action="store_true")

    subparsers.add_parser("status", help="Show the ledger contents.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)

    try:
        policy = QueuePolicy.from_env()
        ledger = QueueLedger(_ledger_path(args.db), policy)

        if args.command == "sync":
            guard_not_github_actions()
            repository = _repository(args.repo)
            client = github_client(repository)
            states = ledger.register_issues(client.list_open_issues())
            print(json.dumps({"registered": len(states), "states": states}, indent=2))
            return 0

        if args.command == "claim":
            guard_not_github_actions()
            claim = ledger.claim(args.worker)
            if claim is None:
                print(json.dumps({"claimed": None}, indent=2))
                return 0
            if args.apply_labels:
                repository = _repository(args.repo)
                client = github_client(repository)
                add, remove = label_transitions(STATE_CLAIMED)
                client.apply_labels(claim.issue_number, add=add, remove=remove)
            print(
                json.dumps(
                    {
                        "claimed": {
                            "issue_number": claim.issue_number,
                            "title": claim.title,
                            "worker_id": claim.worker_id,
                            "lease_token": claim.lease_token,
                            "lease_expires_at": claim.lease_expires_at,
                            "attempts": claim.attempts,
                            "branch": claim.branch,
                        }
                    },
                    indent=2,
                )
            )
            return 0

        if args.command == "heartbeat":
            guard_not_github_actions()
            expires_at = ledger.heartbeat(args.issue, args.worker, args.token)
            print(json.dumps({"lease_expires_at": expires_at}, indent=2))
            return 0 if expires_at is not None else 1

        if args.command == "pr":
            guard_not_github_actions()
            claim = ledger.verify_lease(args.issue, args.worker, args.token)
            repository = _repository(args.repo)
            client = github_client(repository)
            number = client.create_pull_request(
                title=pull_request_title(args.summary, args.issue, args.type),
                body=pull_request_body(args.summary, args.issue),
                head=claim.branch,
                base=args.base,
            )
            handed_over = ledger.mark_review(args.issue, args.worker, args.token, number)
            if not handed_over:
                raise QueueError(
                    f"the lease for issue #{args.issue} expired before the pull "
                    f"request #{number} could be recorded"
                )
            add, remove = label_transitions(STATE_REVIEW)
            client.apply_labels(args.issue, add=add, remove=remove)
            print(
                json.dumps(
                    {"pull_request": number, "branch": claim.branch}, indent=2
                )
            )
            return 0

        if args.command == "review":
            guard_not_github_actions()
            tracked = {item["issue_number"]: item for item in ledger.status()}
            item = tracked.get(args.issue)
            if item is None:
                raise QueueError(f"issue #{args.issue} is not tracked")
            recorded = item.get("pull_request_number")
            if recorded is None:
                raise QueueError(
                    f"issue #{args.issue} has no pull request yet; a review "
                    "outcome may only be applied to the pull request the "
                    "scheduler opened for it"
                )
            if int(recorded) != int(args.pull_request):
                raise QueueError(
                    f"pull request #{args.pull_request} does not belong to issue "
                    f"#{args.issue}; the ledger records pull request #{recorded}"
                )
            repository = _repository(args.repo)
            client = github_client(repository)
            review_state = client.latest_review_state(args.pull_request) or ""
            conclusion = client.pull_request_checks_conclusion(args.pull_request)
            action = review_action(
                review_state,
                checks_conclusion=conclusion,
                attempts=int(item["attempts"]),
                max_attempts=policy.max_attempts,
            )
            if action in {ACTION_REQUEUE, ACTION_BLOCK}:
                new_state = ledger.requeue(args.issue, reason=review_state)
            else:
                new_state = str(item["state"])
            if args.apply_labels and new_state != str(item["state"]):
                add, remove = label_transitions(new_state)
                client.apply_labels(args.issue, add=add, remove=remove)
            print(
                json.dumps(
                    {
                        "review_state": review_state,
                        "checks_conclusion": conclusion,
                        "action": action,
                        "state": new_state,
                    },
                    indent=2,
                )
            )
            return 0

        if args.command == "cancel":
            guard_not_github_actions()
            cancelled = ledger.cancel(args.issue, reason=args.reason)
            if cancelled and args.apply_labels:
                repository = _repository(args.repo)
                client = github_client(repository)
                add, remove = label_transitions(STATE_CANCELLED)
                client.apply_labels(args.issue, add=add, remove=remove)
            print(json.dumps({"cancelled": cancelled}, indent=2))
            return 0

        if args.command == "requeue":
            guard_not_github_actions()
            state = ledger.requeue(args.issue, reason=args.reason)
            print(json.dumps({"state": state}, indent=2))
            return 0

        if args.command == "done":
            guard_not_github_actions()
            finished = ledger.mark_done(args.issue)
            if finished and args.apply_labels:
                repository = _repository(args.repo)
                client = github_client(repository)
                add, remove = label_transitions(STATE_DONE)
                client.apply_labels(args.issue, add=add, remove=remove)
            print(json.dumps({"done": finished}, indent=2))
            return 0

        if args.command == "status":
            print(json.dumps(ledger.status(), indent=2))
            return 0
    except QueueError as error:
        print(f"agent queue error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"agent queue storage error: {error}", file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
