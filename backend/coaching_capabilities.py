"""Coaching capability and prerequisite evaluation for truthful AI coaching.

Enforces that data-grounded coaching queries are only answered when required
data sources are connected, populated, and fresh, preventing fabricated user
history or hallucinated training data.  A provider whose last connection test
failed, or a history that is being rebuilt by a running sync, disables the
capabilities that depend on them rather than letting Coach answer anyway.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from ai_resolver import resolve_provider
from config import Config
from connector_operations import read_connector_status
from database import (
    DEFAULT_DB_PATH,
    _connect,
    get_active_programme,
    get_body_metrics,
    get_daily_logs,
    get_meta,
    get_personal_records,
    get_progress_history,
    get_recent_hevy_logs,
)
from setup_state import ProviderState, ai_provider_status

_GH_TOKEN_KEY = "google_health_token"

SUGGESTED_PROMPT_CONFIGS: list[dict[str, str]] = [
    {
        "text": "How is my deadlift progressing?",
        "capability": "workout_analysis",
        "icon": "📊",
    },
    {
        "text": "Am I recovering well enough?",
        "capability": "recovery_analysis",
        "icon": "💤",
    },
    {
        "text": "Which lifts are stalling?",
        "capability": "workout_analysis",
        "icon": "📉",
    },
    {
        "text": "Should I deload this week?",
        "capability": "workout_analysis",
        "icon": "🔄",
    },
    {
        "text": "How is my body composition trending?",
        "capability": "body_composition",
        "icon": "⚖️",
    },
    {
        "text": "What should I focus on this block?",
        "capability": "programme_guidance",
        "icon": "🎯",
    },
]


@dataclass(frozen=True, slots=True)
class CapabilityItem:
    enabled: bool
    # "ready", "missing_provider", "provider_failed", "syncing", "missing_data",
    # "missing_source", "missing_programme", "stale_data"
    status: str
    explanation: str


@dataclass(frozen=True, slots=True)
class SuggestedPromptState:
    text: str
    capability: str
    icon: str
    enabled: bool
    explanation: str


@dataclass(frozen=True, slots=True)
class CoachingCapabilityState:
    provider_ready: bool
    provider_name: str | None
    capabilities: dict[str, CapabilityItem]
    suggested_prompts: list[SuggestedPromptState]
    overall_status: str  # "setup_required", "educational_only", "partially_grounded", "fully_grounded"
    disclaimer: str
    welcome_message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_ready": self.provider_ready,
            "provider_name": self.provider_name,
            "capabilities": {
                k: {
                    "enabled": v.enabled,
                    "status": v.status,
                    "explanation": v.explanation,
                }
                for k, v in self.capabilities.items()
            },
            "suggested_prompts": [
                {
                    "text": p.text,
                    "capability": p.capability,
                    "icon": p.icon,
                    "enabled": p.enabled,
                    "explanation": p.explanation,
                }
                for p in self.suggested_prompts
            ],
            "overall_status": self.overall_status,
            "disclaimer": self.disclaimer,
            "welcome_message": self.welcome_message,
        }


def _parse_entry_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    val_str = str(value).strip()
    if len(val_str) >= 10:
        try:
            return date.fromisoformat(val_str[:10])
        except ValueError:
            pass
    return None


def get_coaching_capabilities(
    user_id: str | None,
    *,
    db_path: str = DEFAULT_DB_PATH,
    config: Config | None = None,
    now: date | None = None,
    provider: Any | None = None,
) -> CoachingCapabilityState:
    """Evaluate all prerequisites for AI coaching surfaces."""
    current_date = now or datetime.now(timezone.utc).date()
    cfg = config or Config.load()

    provider_ready = False
    provider_name: str | None = None

    def _extract_name(p: Any) -> str:
        name_val = getattr(p, "name", None)
        if callable(name_val):
            try:
                return str(name_val())
            except Exception:  # noqa: BLE001
                return "configured"
        if isinstance(name_val, str) and name_val.strip():
            return name_val.strip()
        return "gemini"

    if provider is not None:
        provider_ready = True
        provider_name = _extract_name(provider)
    else:
        try:
            prov = resolve_provider(
                user_id=user_id,
                server_gemini_key=cfg.gemini_api_key,
                server_gemini_model=cfg.gemini_model,
                db_path=db_path,
            )
            provider_ready = True
            provider_name = _extract_name(prov)
        except Exception:  # noqa: BLE001
            provider_ready = False

    # A provider whose last connection test failed is known not to work, so
    # Coach fails closed instead of claiming it can answer.  Unreadable evidence
    # is never turned into a claimed failure.
    provider_failed = False
    if user_id is not None:
        try:
            provider_failed = (
                ai_provider_status(
                    user_id=user_id,
                    db_path=db_path,
                    server_api_key=cfg.gemini_api_key,
                ).state
                is ProviderState.FAILED
            )
        except Exception:  # noqa: BLE001 - evidence is advisory, never a crash
            provider_failed = False
    if provider_failed:
        provider_ready = False

    # While a connect or sync is running the history is being rebuilt, so any
    # answer grounded in it could be built from partial data.
    rebuilding = False
    if user_id is not None:
        try:
            rebuilding = (
                read_connector_status(user_id, db_path=db_path).operation is not None
            )
        except Exception:  # noqa: BLE001 - evidence is advisory, never a crash
            rebuilding = False

    def _provider_blocked(feature: str) -> CapabilityItem:
        if provider_failed:
            return CapabilityItem(
                enabled=False,
                status="provider_failed",
                explanation=(
                    "Your AI provider's last connection test failed. Test it again "
                    f"or replace the key in Settings to enable {feature}."
                ),
            )
        return CapabilityItem(
            enabled=False,
            status="missing_provider",
            explanation=f"Configure an AI provider in Settings to enable {feature}.",
        )

    def _syncing_blocked(feature: str) -> CapabilityItem:
        return CapabilityItem(
            enabled=False,
            status="syncing",
            explanation=(
                f"A Hevy sync is running, so {feature} would use incomplete history. "
                "Try again when it finishes."
            ),
        )

    # Educational capability
    if not provider_ready:
        educational = (
            _provider_blocked("Coach")
            if provider_failed
            else CapabilityItem(
                enabled=False,
                status="missing_provider",
                explanation="No AI provider configured. Add an API key in Settings -> AI Providers to enable Coach.",
            )
        )
    else:
        educational = CapabilityItem(
            enabled=True,
            status="ready",
            explanation="General training and fitness education is available.",
        )

    # Workout analysis capability (training logs, PRs, workout history)
    daily_logs = get_daily_logs(limit=40, db_path=db_path, user_id=user_id)
    hevy_logs = get_recent_hevy_logs(limit=30, db_path=db_path, user_id=user_id)
    history = get_progress_history(db_path=db_path, user_id=user_id)
    prs = get_personal_records(db_path=db_path, user_id=user_id)

    workout_dates: list[date] = []
    if isinstance(daily_logs, list):
        for item in daily_logs:
            if isinstance(item, dict):
                d = _parse_entry_date(item.get("date"))
                if d:
                    workout_dates.append(d)
    if isinstance(hevy_logs, list):
        for item in hevy_logs:
            if isinstance(item, dict):
                d = _parse_entry_date(item.get("start_time") or item.get("date"))
                if d:
                    workout_dates.append(d)
    if isinstance(history, dict):
        for exercise_sets in history.values():
            if isinstance(exercise_sets, list):
                for item in exercise_sets:
                    if isinstance(item, dict):
                        d = _parse_entry_date(item.get("date"))
                        if d:
                            workout_dates.append(d)
    elif isinstance(history, list):
        for item in history:
            if isinstance(item, dict):
                d = _parse_entry_date(item.get("date"))
                if d:
                    workout_dates.append(d)
    if isinstance(prs, list):
        for item in prs:
            if isinstance(item, dict):
                d = _parse_entry_date(item.get("date"))
                if d:
                    workout_dates.append(d)

    if not provider_ready:
        workout_analysis = _provider_blocked("workout analysis")
    elif rebuilding:
        workout_analysis = _syncing_blocked("workout analysis")
    elif not workout_dates:
        workout_analysis = CapabilityItem(
            enabled=False,
            status="missing_data",
            explanation="No workout logs found. Connect Hevy in Settings and sync your workout history to enable lift progression analysis.",
        )
    else:
        latest_workout = max(workout_dates)
        age_days = (current_date - latest_workout).days
        if age_days > 21:
            workout_analysis = CapabilityItem(
                enabled=False,
                status="stale_data",
                explanation=f"Your workout logs are stale (last recorded workout was {latest_workout.isoformat()}). Sync your latest workouts from Hevy to enable current lift analysis.",
            )
        else:
            workout_analysis = CapabilityItem(
                enabled=True,
                status="ready",
                explanation="Workout logs and personal records are connected and current.",
            )

    # Recovery analysis capability (health connections, readiness, HRV, resting HR)
    metrics = get_body_metrics(limit=60, db_path=db_path, user_id=user_id)
    recovery_dates: list[date] = []
    for r in metrics:
        if r.get("resting_hr") is not None or r.get("hrv") is not None:
            d = _parse_entry_date(r.get("date"))
            if d:
                recovery_dates.append(d)

    # Check for connected health sources or health_metrics table
    has_connected_source = False
    gh_token = get_meta(_GH_TOKEN_KEY, db_path, user_id=user_id) if user_id else None
    if gh_token:
        has_connected_source = True

    try:
        with _connect(db_path) as conn:
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='health_connections'"
            ).fetchone()
            if table_check and user_id is not None:
                # check integer or string user_id
                rows = conn.execute(
                    "SELECT state, last_sync_at FROM health_connections WHERE user_id = ? AND state = 'connected'",
                    (user_id,),
                ).fetchall()
                if rows:
                    has_connected_source = True
                    for row in rows:
                        d = _parse_entry_date(row[1])
                        if d:
                            recovery_dates.append(d)
            metric_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='health_metrics'"
            ).fetchone()
            if metric_check and user_id is not None:
                m_rows = conn.execute(
                    "SELECT observed_at FROM health_metrics WHERE user_id = ? ORDER BY observed_at DESC LIMIT 10",
                    (user_id,),
                ).fetchall()
                if m_rows:
                    has_connected_source = True
                    for row in m_rows:
                        d = _parse_entry_date(row[0])
                        if d:
                            recovery_dates.append(d)
    except sqlite3.Error:
        pass

    if not provider_ready:
        recovery_analysis = _provider_blocked("recovery analysis")
    elif not has_connected_source and not recovery_dates:
        recovery_analysis = CapabilityItem(
            enabled=False,
            status="missing_source",
            explanation="Connect a health source in Settings to enable recovery-aware coaching.",
        )
    elif not recovery_dates:
        recovery_analysis = CapabilityItem(
            enabled=False,
            status="stale_data",
            explanation="Your health source is connected but does not have fresh data yet. Sync recent recovery data to assess readiness.",
        )
    else:
        latest_recovery = max(recovery_dates)
        age_days = (current_date - latest_recovery).days
        if age_days > 3:  # 72 hours
            recovery_analysis = CapabilityItem(
                enabled=False,
                status="stale_data",
                explanation=f"Your health data is stale (last recorded reading was {latest_recovery.isoformat()}). Sync recent recovery data to assess readiness.",
            )
        else:
            recovery_analysis = CapabilityItem(
                enabled=True,
                status="ready",
                explanation="Recovery-aware coaching has fresh connected health data.",
            )

    # Body composition capability
    body_dates: list[date] = []
    for r in metrics:
        if r.get("weight_kg") is not None or r.get("body_fat_pct") is not None:
            d = _parse_entry_date(r.get("date"))
            if d:
                body_dates.append(d)

    if not provider_ready:
        body_composition = _provider_blocked("body composition analysis")
    elif not body_dates:
        body_composition = CapabilityItem(
            enabled=False,
            status="missing_data",
            explanation="No body composition metrics found. Log your weight or body composition in Check-ins to enable trend analysis.",
        )
    else:
        latest_body = max(body_dates)
        age_days = (current_date - latest_body).days
        if age_days > 30:
            body_composition = CapabilityItem(
                enabled=False,
                status="stale_data",
                explanation=f"Your body composition data is stale (last recorded {latest_body.isoformat()}). Log a new check-in to analyse trends.",
            )
        else:
            body_composition = CapabilityItem(
                enabled=True,
                status="ready",
                explanation="Body composition data is up to date.",
            )

    # Programme guidance capability
    active_prog = get_active_programme(user_id=user_id, db_path=db_path) if user_id else None
    has_active_prog = bool(active_prog and active_prog.get("definition"))
    if active_prog and active_prog.get("status") == "setup_required":
        has_active_prog = False

    if not provider_ready:
        programme_guidance = _provider_blocked("programme guidance")
    elif rebuilding:
        programme_guidance = _syncing_blocked("programme guidance")
    elif not has_active_prog:
        programme_guidance = CapabilityItem(
            enabled=False,
            status="missing_programme",
            explanation="No active programme found. Visit Programmes to select routines and activate a training programme.",
        )
    else:
        prog_name = str((active_prog or {}).get("name") or "Active programme")
        programme_guidance = CapabilityItem(
            enabled=True,
            status="ready",
            explanation=f"Active programme '{prog_name}' is loaded.",
        )

    capabilities: dict[str, CapabilityItem] = {
        "educational": educational,
        "workout_analysis": workout_analysis,
        "recovery_analysis": recovery_analysis,
        "body_composition": body_composition,
        "programme_guidance": programme_guidance,
    }

    # Evaluate suggested prompt states
    suggested_prompts: list[SuggestedPromptState] = []
    for cfg_prompt in SUGGESTED_PROMPT_CONFIGS:
        cap_key = cfg_prompt["capability"]
        cap_item = capabilities.get(cap_key)
        enabled = cap_item.enabled if cap_item else False
        explanation = cap_item.explanation if cap_item else "Feature unavailable."
        suggested_prompts.append(
            SuggestedPromptState(
                text=cfg_prompt["text"],
                capability=cap_key,
                icon=cfg_prompt["icon"],
                enabled=enabled,
                explanation=explanation,
            )
        )

    # Derive overall status
    data_capabilities = [workout_analysis, recovery_analysis, body_composition, programme_guidance]
    data_enabled_count = sum(1 for c in data_capabilities if c.enabled)

    if provider_failed:
        overall_status = "setup_required"
        welcome_message = (
            "Coach is paused because your AI provider's last connection test failed. "
            "Test it again or replace the key in Settings."
        )
        disclaimer = "Coach is offline until your AI provider passes a connection test."
    elif not provider_ready:
        overall_status = "setup_required"
        welcome_message = "Coach requires an AI provider key to get started. Add your API key in Settings -> AI Providers."
        disclaimer = "Coach is offline until an AI provider is configured in Settings."
    elif data_enabled_count == 0:
        overall_status = "educational_only"
        welcome_message = "Coach is in educational mode. Ask any general training, technique, or programming question. Connect your workout and health data in Settings for personalised analysis."
        disclaimer = "Coach provides general educational guidance until training and recovery sources are connected."
    elif data_enabled_count == len(data_capabilities):
        overall_status = "fully_grounded"
        welcome_message = "Coach has access to your training logs, recovery data, body composition, and programme."
        disclaimer = "Coach answers based on your connected training logs, recovery data, and programme."
    else:
        overall_status = "partially_grounded"
        enabled_names = []
        if workout_analysis.enabled:
            enabled_names.append("training logs")
        if recovery_analysis.enabled:
            enabled_names.append("recovery data")
        if body_composition.enabled:
            enabled_names.append("body composition")
        if programme_guidance.enabled:
            enabled_names.append("active programme")
        welcome_message = f"Coach has access to your {', '.join(enabled_names)}. Connect additional sources in Settings for comprehensive coaching."
        disclaimer = f"Coach answers based on your connected {', '.join(enabled_names)}."

    return CoachingCapabilityState(
        provider_ready=provider_ready,
        provider_name=provider_name,
        capabilities=capabilities,
        suggested_prompts=suggested_prompts,
        overall_status=overall_status,
        disclaimer=disclaimer,
        welcome_message=welcome_message,
    )


def classify_coaching_query(query: str) -> tuple[str, str | None]:
    """Classify a coaching query into query mode and required capability.

    Returns:
        (mode, capability_key)
        mode is 'data_grounded' or 'educational'.
        capability_key is the name of the prerequisite capability, or None if educational.
    """
    q = query.strip().lower()

    # Exact matches for suggested prompts
    if "deadlift progress" in q or q == "how is my deadlift progressing?":
        return ("data_grounded", "workout_analysis")
    if "recovering well" in q or q == "am i recovering well enough?":
        return ("data_grounded", "recovery_analysis")
    if "lifts are stalling" in q or q == "which lifts are stalling?":
        return ("data_grounded", "workout_analysis")
    if "should i deload" in q or q == "should i deload this week?":
        return ("data_grounded", "workout_analysis")
    if "body composition trending" in q or q == "how is my body composition trending?":
        return ("data_grounded", "body_composition")
    if "focus on this block" in q or q == "what should i focus on this block?":
        return ("data_grounded", "programme_guidance")

    # Generic educational question pattern checks (questions defining concepts or techniques)
    # Examples: "what is progressive overload", "how to perform a deadlift", "what are good protein sources"
    educational_starters = (
        "what is ",
        "what are ",
        "how does ",
        "how do i perform",
        "how to perform",
        "how to do",
        "how to execute",
        "explain ",
        "define ",
        "tell me about",
        "give me an overview",
        "difference between",
        "what does rpe mean",
        "what does rir mean",
    )
    if any(q.startswith(starter) for starter in educational_starters) and not re.search(
        r"\b(my|mine|this block|last workout|my workouts|my logs)\b", q
    ):
        return ("educational", None)

    # Personal recovery patterns
    if re.search(r"\b(my|i|me|am i)\b", q) and re.search(r"\b(recovery|readiness|sleep|hrv|resting hr|fatigued|exhausted)\b", q):
        return ("data_grounded", "recovery_analysis")

    # Personal body composition patterns
    if re.search(r"\b(my|i|me|am i)\b", q) and re.search(r"\b(body composition|weight trend|body fat|muscle mass|weigh in)\b", q):
        return ("data_grounded", "body_composition")

    # Personal programme / block patterns
    if re.search(r"\b(my|this|current)\b", q) and re.search(r"\b(block|programme|program|split|workout today|routine)\b", q):
        return ("data_grounded", "programme_guidance")

    # Personal workout / lift progression patterns
    # e.g. "how is my deadlift", "how are my lifts", "which lifts are stalling", "am I getting stronger", "my bench"
    if re.search(r"\b(my|i|me|am i|should i|did i)\b", q) and re.search(
        r"\b(deadlift|squat|bench|overhead press|press|lift|lifts|stalling|progress|progressing|pr|1rm|deload|volume|sets|reps|workout)\b",
        q,
    ):
        return ("data_grounded", "workout_analysis")

    # General / educational by default
    return ("educational", None)
