"""Product-level health capability permission catalogue.

Provider adapters map these capabilities to current provider-specific scopes.
Keeping product intent separate prevents UI copy from hardcoding stale OAuth
scope strings.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PermissionPurpose:
    capability: str
    explanation: str


PERMISSIONS = {
    "sleep": PermissionPurpose("sleep", "Use sleep duration and stages to estimate recovery against your own baseline."),
    "heart_rate": PermissionPurpose("heart_rate", "Use resting heart rate trends as one recovery input."),
    "hrv": PermissionPurpose("hrv", "Use HRV trends when the connected provider supplies them."),
    "activity": PermissionPurpose("activity", "Use recorded activity and training load to understand recent workload."),
    "body": PermissionPurpose("body", "Use weight and body-composition trends in progress views and coaching context."),
    "spo2": PermissionPurpose("spo2", "Use oxygen-saturation data only when explicitly enabled for recovery context."),
    "write_workouts": PermissionPurpose("write_workouts", "Send a workout or training plan to a connected provider only after explicit approval."),
}


def explain_permissions(capabilities: set[str] | frozenset[str]) -> tuple[PermissionPurpose, ...]:
    return tuple(PERMISSIONS[key] for key in sorted(capabilities) if key in PERMISSIONS)
