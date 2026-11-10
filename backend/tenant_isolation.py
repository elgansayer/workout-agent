"""Immutable owner identity and execution guards for background work.

Request handlers prove who owns data through the session. Schedulers, jobs and
other workers run without one, so every worker carries an :class:`OwnerIdentity`
instead. The identity is immutable, is the only way to derive cache, lock,
cursor and notification-batch keys, and is re-authorized against the user store
immediately before work executes. A user id received from a CLI flag, an
environment variable or a queued payload is a claim, never proof.

See ``docs/TENANT_ISOLATION.md`` for the contract new jobs must follow.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, TypeVar
from urllib.parse import quote

from database import get_user_by_id

logger = logging.getLogger(__name__)

T = TypeVar("T")

_MAX_USER_ID_LENGTH = 128
_KEY_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_.-]*")


class OwnershipError(PermissionError):
    """Raised when an owner cannot be proven or does not match a resource."""


@dataclass(frozen=True, slots=True)
class OwnerIdentity:
    """Tenant identity carried by every key, lock, job and AI context."""

    user_id: str

    def __post_init__(self) -> None:
        value = self.user_id
        if not isinstance(value, str) or not value or value != value.strip():
            raise OwnershipError("owner identity requires a non-empty user id")
        if len(value) > _MAX_USER_ID_LENGTH or not value.isprintable():
            raise OwnershipError("owner identity is malformed")


def authorize_owner(db_path: str, claimed_user_id: str | None) -> OwnerIdentity:
    """Re-authorize a claimed user id against the user store, failing closed.

    The returned identity is built from the stored row, not from the claim, so
    callers continue with the canonical id. A missing id, an unknown user or an
    unreachable store all raise :class:`OwnershipError`; there is deliberately
    no fallback to shared or unscoped data.
    """
    if not claimed_user_id:
        raise OwnershipError("a worker requires an explicit owner")
    claimed = OwnerIdentity(claimed_user_id)
    try:
        row = get_user_by_id(claimed.user_id, db_path)
    except (sqlite3.Error, OSError) as exc:  # OSError: database directory unusable
        raise OwnershipError("owner could not be verified") from exc
    if row is None:
        raise OwnershipError("owner does not exist")
    return OwnerIdentity(row["id"])


def owner_scoped_key(owner: OwnerIdentity, namespace: str, *parts: str) -> str:
    """Build a cache, lock, cursor or notification-batch key bound to *owner*.

    The owner is always the second segment and every segment after the
    namespace is percent-encoded, so no caller-supplied part can impersonate
    another owner or namespace and distinct inputs never collide.
    """
    if not isinstance(owner, OwnerIdentity):
        raise OwnershipError("a scoped key requires an OwnerIdentity")
    if not _KEY_SEGMENT.fullmatch(namespace):
        raise ValueError(
            "key namespace must be lowercase letters, digits, '-', '_' or '.'"
        )
    if any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("key parts must be non-empty strings")
    return ":".join([namespace, *(quote(s, safe="") for s in (owner.user_id, *parts))])


@dataclass(frozen=True, slots=True)
class OwnedContext:
    """Prompt or notification input bound to the owner whose data produced it.

    Persistence and delivery take the owner from the context, never from an
    ambient loop variable, so output cannot be written to a different tenant
    than the one whose data was read.
    """

    owner: OwnerIdentity
    purpose: str
    data: Mapping[str, Any]


class OwnerLocks:
    """Non-blocking in-process locks keyed by owner and job.

    A lock never spans owners: one user's slow job cannot delay or suppress
    another user's, and an overlapping duplicate for the same owner is skipped
    instead of racing.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held: set[str] = set()

    @contextmanager
    def try_acquire(self, key: str) -> Iterator[bool]:
        with self._guard:
            acquired = key not in self._held
            if acquired:
                self._held.add(key)
        try:
            yield acquired
        finally:
            if acquired:
                with self._guard:
                    self._held.discard(key)


class JobStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class OwnedJob:
    """A unit of background work that can only ever act for one owner."""

    kind: str
    owner: OwnerIdentity

    @property
    def key(self) -> str:
        return owner_scoped_key(self.owner, "job", self.kind)


@dataclass(frozen=True, slots=True)
class JobOutcome(Generic[T]):
    job: OwnedJob
    status: JobStatus
    result: T | None = None
    error: str | None = None


_DEFAULT_LOCKS = OwnerLocks()


def run_owned_job(
    job: OwnedJob,
    db_path: str,
    work: Callable[[OwnerIdentity], T],
    *,
    locks: OwnerLocks | None = None,
) -> JobOutcome[T]:
    """Re-authorize the job's owner, lock per owner, then run *work* isolated.

    *work* receives the identity freshly verified against the user store, so a
    user removed after the job was queued is denied rather than resurrected.
    Failures are contained and reported in the outcome; they never propagate to
    the next owner's job.
    """
    try:
        owner = authorize_owner(db_path, job.owner.user_id)
    except OwnershipError as exc:
        logger.warning(
            "Denied %s job for owner %s: %s", job.kind, job.owner.user_id, exc
        )
        return JobOutcome(job, JobStatus.DENIED, error=str(exc))

    with (locks or _DEFAULT_LOCKS).try_acquire(job.key) as acquired:
        if not acquired:
            logger.warning(
                "Skipping overlapping %s job for owner %s.", job.kind, owner.user_id
            )
            return JobOutcome(job, JobStatus.SKIPPED)
        try:
            return JobOutcome(job, JobStatus.COMPLETED, result=work(owner))
        except Exception as exc:
            logger.exception("%s job failed for owner %s.", job.kind, owner.user_id)
            return JobOutcome(job, JobStatus.FAILED, error=type(exc).__name__)
