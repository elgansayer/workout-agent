"""Product capability matrix for supported health providers."""

from __future__ import annotations


PROVIDER_CAPABILITIES = {
    "garmin": frozenset({"sleep", "heart_rate", "hrv", "steps", "stress", "spo2", "respiration", "body", "activity"}),
    "health_connect": frozenset({"sleep", "heart_rate", "hrv", "steps", "body", "activity", "spo2"}),
    "fitbit": frozenset({"sleep", "heart_rate", "activity", "workouts", "body"}),
    "oura": frozenset({"sleep", "heart_rate", "hrv", "activity", "workouts", "readiness", "spo2"}),
    "polar": frozenset({"sleep", "heart_rate", "activity", "training", "recovery"}),
    "withings": frozenset({"sleep", "heart_rate", "body"}),
}
