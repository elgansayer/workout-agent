"""Central data-classification and handling policy for Workout Agent.

The policy is deliberately code-readable: callers should use these helpers
instead of inventing per-feature rules for logs and exports.  Unknown fields
fail closed to ``internal`` rather than being treated as public.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DataClass(StrEnum):
    PUBLIC = "public"
    IDENTIFIER = "identifier"
    HEALTH = "health"
    WORKOUT = "workout"
    PROMPT = "prompt"
    ANALYTICS = "analytics"
    CREDENTIAL = "credential"
    INTERNAL = "internal"


@dataclass(frozen=True)
class HandlingPolicy:
    storage: str
    encryption: str
    logging: str
    retention: str
    exportable: bool
    access: str


POLICIES: dict[DataClass, HandlingPolicy] = {
    DataClass.PUBLIC: HandlingPolicy(
        storage="normal application storage",
        encryption="transport encryption; at-rest encryption inherited from platform",
        logging="allowed",
        retention="product/documentation lifecycle",
        exportable=True,
        access="public",
    ),
    DataClass.IDENTIFIER: HandlingPolicy(
        storage="tenant-scoped storage only",
        encryption="encrypted transport and protected at-rest storage",
        logging="redacted or pseudonymised only",
        retention="account lifetime plus documented operational retention",
        exportable=True,
        access="owner and explicitly authorised operators",
    ),
    DataClass.HEALTH: HandlingPolicy(
        storage="tenant-scoped storage only",
        encryption="encrypted transport and protected at-rest storage",
        logging="never log raw values",
        retention="user-controlled health-data retention policy",
        exportable=True,
        access="owner and purpose-authorised processing only",
    ),
    DataClass.WORKOUT: HandlingPolicy(
        storage="tenant-scoped storage only",
        encryption="encrypted transport and protected at-rest storage",
        logging="metadata only; no raw workout payloads",
        retention="user-controlled workout-data retention policy",
        exportable=True,
        access="owner and purpose-authorised processing only",
    ),
    DataClass.PROMPT: HandlingPolicy(
        storage="tenant-scoped when persisted",
        encryption="encrypted transport and protected at-rest storage",
        logging="never log raw prompt or response payloads",
        retention="shortest period required for the enabled coaching feature",
        exportable=True,
        access="owner and selected AI processor only",
    ),
    DataClass.ANALYTICS: HandlingPolicy(
        storage="prefer aggregated or pseudonymised storage",
        encryption="encrypted transport and protected at-rest storage",
        logging="aggregated values only",
        retention="bounded analytics window",
        exportable=True,
        access="owner plus authorised product analytics",
    ),
    DataClass.CREDENTIAL: HandlingPolicy(
        storage="encrypted secret store only",
        encryption="authenticated application-layer encryption plus encrypted transport",
        logging="never",
        retention="until rotation, revocation, disconnect, or account deletion",
        exportable=False,
        access="narrow credential service only",
    ),
    DataClass.INTERNAL: HandlingPolicy(
        storage="protected application storage",
        encryption="encrypted transport and platform at-rest controls",
        logging="safe metadata only",
        retention="documented operational retention",
        exportable=False,
        access="authorised application/operator paths only",
    ),
}

# Exact schema/API names that carry an unambiguous classification.  Keep this
# list explicit so schema reviews can assert that sensitive fields are known.
FIELD_CLASSES: dict[str, DataClass] = {
    # credentials / auth material
    "api_key": DataClass.CREDENTIAL,
    "client_secret": DataClass.CREDENTIAL,
    "refresh_token": DataClass.CREDENTIAL,
    "access_token": DataClass.CREDENTIAL,
    "authorization": DataClass.CREDENTIAL,
    "password": DataClass.CREDENTIAL,
    "cookie": DataClass.CREDENTIAL,
    "auth": DataClass.CREDENTIAL,
    "p256dh": DataClass.CREDENTIAL,
    "vapid_private_key": DataClass.CREDENTIAL,
    "web_auth_secret": DataClass.CREDENTIAL,
    "encryption_key": DataClass.CREDENTIAL,
    # identifiers
    "user_id": DataClass.IDENTIFIER,
    "email": DataClass.IDENTIFIER,
    "display_name": DataClass.IDENTIFIER,
    "ip": DataClass.IDENTIFIER,
    "endpoint": DataClass.IDENTIFIER,
    "device_id": DataClass.IDENTIFIER,
    "external_user_id": DataClass.IDENTIFIER,
    # health
    "weight_kg": DataClass.HEALTH,
    "body_fat_pct": DataClass.HEALTH,
    "muscle_pct": DataClass.HEALTH,
    "resting_hr": DataClass.HEALTH,
    "hrv": DataClass.HEALTH,
    "sleep": DataClass.HEALTH,
    "recovery": DataClass.HEALTH,
    "health_payload": DataClass.HEALTH,
    # workout/training
    "hevy_payload": DataClass.WORKOUT,
    "workout": DataClass.WORKOUT,
    "workouts": DataClass.WORKOUT,
    "exercise_name": DataClass.WORKOUT,
    "top_weight_kg": DataClass.WORKOUT,
    "top_reps": DataClass.WORKOUT,
    "sets": DataClass.WORKOUT,
    "routine_id": DataClass.WORKOUT,
    "routine_key": DataClass.WORKOUT,
    "plan": DataClass.WORKOUT,
    # AI prompts / responses
    "prompt": DataClass.PROMPT,
    "system_prompt": DataClass.PROMPT,
    "content": DataClass.PROMPT,
    "reasoning": DataClass.PROMPT,
    "insight_json": DataClass.PROMPT,
    "insight_markdown": DataClass.PROMPT,
    # analytics / derived metrics
    "confidence": DataClass.ANALYTICS,
    "score": DataClass.ANALYTICS,
    "correlation": DataClass.ANALYTICS,
}

_CREDENTIAL_FRAGMENTS = (
    "api_key",
    "secret",
    "token",
    "password",
    "authorization",
    "cookie",
    "private_key",
)
_HEALTH_FRAGMENTS = (
    "heart_rate",
    "resting_hr",
    "hrv",
    "body_fat",
    "muscle_pct",
    "sleep",
    "recovery",
    "health",
)
_WORKOUT_FRAGMENTS = (
    "workout",
    "exercise",
    "routine",
    "rep",
    "set_count",
    "weight_kg",
    "training",
)
_PROMPT_FRAGMENTS = ("prompt", "reasoning", "chat_message", "ai_response")
_IDENTIFIER_FRAGMENTS = ("user_id", "email", "display_name", "device_id", "ip_address")
_ANALYTICS_FRAGMENTS = ("analytics", "correlation", "confidence", "score", "aggregate")


def _normalise_field_name(field_name: str) -> str:
    return field_name.strip().lower().replace("-", "_")


def classify_field(field_name: str) -> DataClass:
    """Return the classification for a field name, failing closed to internal."""
    normalised = _normalise_field_name(field_name)
    if normalised in FIELD_CLASSES:
        return FIELD_CLASSES[normalised]

    for fragment in _CREDENTIAL_FRAGMENTS:
        if fragment in normalised:
            return DataClass.CREDENTIAL
    for fragment in _HEALTH_FRAGMENTS:
        if fragment in normalised:
            return DataClass.HEALTH
    for fragment in _WORKOUT_FRAGMENTS:
        if fragment in normalised:
            return DataClass.WORKOUT
    for fragment in _PROMPT_FRAGMENTS:
        if fragment in normalised:
            return DataClass.PROMPT
    for fragment in _IDENTIFIER_FRAGMENTS:
        if fragment in normalised:
            return DataClass.IDENTIFIER
    for fragment in _ANALYTICS_FRAGMENTS:
        if fragment in normalised:
            return DataClass.ANALYTICS
    return DataClass.INTERNAL


def safe_log_value(field_name: str, value: Any) -> Any:
    """Return a logging-safe representation for one classified value."""
    classification = classify_field(field_name)
    if classification is DataClass.PUBLIC:
        return value
    if classification is DataClass.CREDENTIAL:
        return "[REDACTED:CREDENTIAL]"
    if classification in {
        DataClass.IDENTIFIER,
        DataClass.HEALTH,
        DataClass.WORKOUT,
        DataClass.PROMPT,
    }:
        return f"[REDACTED:{classification.value.upper()}]"
    return value


def redact_for_log(value: Any, *, field_name: str | None = None) -> Any:
    """Recursively redact a structure before it is emitted to logs.

    Mapping keys drive classification.  Sequence contents inherit the parent
    field's classification so a list of tokens or health samples cannot leak.
    """
    if field_name is not None:
        classification = classify_field(field_name)
        if classification in {
            DataClass.CREDENTIAL,
            DataClass.IDENTIFIER,
            DataClass.HEALTH,
            DataClass.WORKOUT,
            DataClass.PROMPT,
        }:
            return safe_log_value(field_name, value)

    if isinstance(value, Mapping):
        return {
            str(key): redact_for_log(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_for_log(item, field_name=field_name) for item in value]
    return value


def sanitize_for_export(value: Any, *, field_name: str | None = None) -> Any:
    """Return an export-safe copy, dropping classes that must never be exported.

    Credential and internal fields are removed from mappings.  Public,
    identifier, health, workout, prompt, and analytics data remain eligible
    for a user's own authenticated export; callers still need tenant checks.
    """
    if isinstance(value, Mapping):
        exported: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            policy = POLICIES[classify_field(key_text)]
            if not policy.exportable:
                continue
            exported[key_text] = sanitize_for_export(item, field_name=key_text)
        return exported

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if field_name is not None and not POLICIES[classify_field(field_name)].exportable:
            return []
        return [sanitize_for_export(item, field_name=field_name) for item in value]

    if field_name is not None and not POLICIES[classify_field(field_name)].exportable:
        return None
    return value


def policy_for(field_name: str) -> HandlingPolicy:
    """Return the handling policy for a field name."""
    return POLICIES[classify_field(field_name)]
