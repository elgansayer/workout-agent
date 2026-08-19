"""Reconcile wearable activities with strength-workout records without merging provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class SessionWindow:
    user_id: int
    source: str
    source_id: str
    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True, slots=True)
class SessionLink:
    user_id: int
    left_source: str
    left_id: str
    right_source: str
    right_id: str
    overlap_seconds: float


def reconcile_sessions(left: SessionWindow, right: SessionWindow, *, tolerance: timedelta = timedelta(minutes=20)) -> SessionLink | None:
    if left.user_id != right.user_id:
        raise ValueError("cannot reconcile sessions across users")
    start = max(left.started_at, right.started_at)
    end = min(left.ended_at, right.ended_at)
    overlap = max((end - start).total_seconds(), 0.0)
    close_start = abs((left.started_at - right.started_at).total_seconds()) <= tolerance.total_seconds()
    if overlap <= 0 and not close_start:
        return None
    return SessionLink(left.user_id, left.source, left.source_id, right.source, right.source_id, overlap)
