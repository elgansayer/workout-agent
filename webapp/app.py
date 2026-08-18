"""Internal progressive-overload web app (FastAPI).

A self-hosted dashboard for the workout agent. It reads the same SQLite database
the agent writes and renders it as a rich control centre: today's session and
progressive-overload targets, server-rendered SVG charts of every lift and your
body composition, all-time personal records, training-load trends, a consistency
calendar, the full periodisation plan, and programme check-ins.

Google OAuth login is available when WEB_AUTH_SECRET is configured, providing
user-scoped data isolation. When auth is disabled, it is read-only and meant to
sit behind a reverse proxy on a trusted host (e.g. Apache -> Docker ->
gym.example.com). All motivation is automated; nothing here calls out to an API
on a page view.

Run locally:   uvicorn webapp.app:app --reload
In a container: see Dockerfile.web / the `web` service in docker-compose.yml
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import google.generativeai as genai
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

import analytics
import insights
import lifestyle
from ai_provider import PROVIDERS, AIProvider, available_providers, resolve_provider
from config import Config
from database import (
    clear_chat_messages,
    delete_user_api_key,
    get_active_programme,
    get_body_metrics,
    get_chat_messages,
    get_checkins,
    get_daily_logs,
    get_dashboard_insight,
    get_exercise_volumes,
    get_meta,
    get_or_create_user,
    get_personal_records,
    get_programme_start_date,
    get_programme_templates,
    get_progress_history,
    get_reasoning_log,
    get_recent_bests,
    get_session_volumes,
    get_user_api_keys,
    get_user_preferences,
    init_db,
    save_chat_message,
    save_reasoning_log,
    save_user_api_key,
    save_user_preferences,
    set_active_programme,
    set_meta,
)
from google_health_auth import build_authorize_url, exchange_code
from hevy_parser import normalise_name
from program import (
    BLOCK_WEEKS,
    BLOCKS,
    COACHING_RULES,
    CYCLE_WEEKS,
    SPLIT_NAME,
    block_for_week,
    day_exercises,
    day_focus,
    today_day,
    week_in_cycle,
)
from programme_inference import InferredProgramme
from webapp import ai_widgets, charts

DB_PATH = os.environ.get("DATABASE_PATH", "workout_agent.db").strip()
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)

def _resolve_provider_for_request(request: Request, config: Config) -> AIProvider:
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    return resolve_provider(
        user_id=user_id,
        fallback_api_key=config.gemini_api_key,
        fallback_model=config.gemini_model,
    )


def get_config():
    return Config.load()


_RATE_LIMITS: dict[str, list[float]] = {}


def _check_rate_limit(request: Request, limit: int = 10, window: int = 60) -> None:
    now = time.time()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    elif request.headers.get("x-real-ip"):
        ip = request.headers.get("x-real-ip").strip()  # type: ignore[union-attr]
    else:
        ip = request.client.host if request.client else "unknown"

    if ip not in _RATE_LIMITS:
        _RATE_LIMITS[ip] = []
    _RATE_LIMITS[ip] = [t for t in _RATE_LIMITS[ip] if now - t < window]
    if len(_RATE_LIMITS[ip]) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")



# Google Health linking is opt-in: set the OAuth client in the web service's
# environment to enable the "Connect Google Health" button on the Settings page.
GH_CLIENT_ID = os.environ.get("GOOGLE_HEALTH_CLIENT_ID", "").strip()
GH_CLIENT_SECRET = os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET", "").strip()
# Optional explicit redirect URI (recommended behind a reverse proxy so the
# scheme/host match exactly what is registered with Google). When unset it is
# derived from the incoming request.
GH_REDIRECT_URI = os.environ.get("GOOGLE_HEALTH_REDIRECT_URI", "").strip()
_GH_TOKEN_KEY = "google_health_refresh_token"
_GH_STATE_KEY = "google_health_oauth_state"

WEB_GOOGLE_CLIENT_ID = os.environ.get("WEB_GOOGLE_CLIENT_ID", "").strip()
WEB_GOOGLE_CLIENT_SECRET = os.environ.get("WEB_GOOGLE_CLIENT_SECRET", "").strip()
WEB_AUTH_SECRET = os.environ.get("WEB_AUTH_SECRET", "").strip()
ALLOWED_EMAILS = [
    e.strip() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
]

oauth = OAuth()
if WEB_GOOGLE_CLIENT_ID and WEB_GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=WEB_GOOGLE_CLIENT_ID,
        client_secret=WEB_GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

# Cache-busting for static assets. Cloudflare (and browsers) cache /static/*
# aggressively, so a plain "/static/style.css" can serve a stale copy for hours
# after a deploy. We append a short content hash (?v=...) so every change yields
# a brand-new URL the cache has never seen, while unchanged files stay cached.
_ASSET_VERSIONS: dict[str, str] = {}


def static_url(filename: str) -> str:
    version = _ASSET_VERSIONS.get(filename)
    if version is None:
        try:
            data = (_BASE_DIR / "static" / filename).read_bytes()
            version = hashlib.sha256(data).hexdigest()[:8]
        except OSError:
            version = "0"
        _ASSET_VERSIONS[filename] = version
    return f"/static/{filename}?v={version}"


templates.env.globals["static_url"] = static_url


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Safe even if the agent already created the database (CREATE IF NOT EXISTS).
    init_db(DB_PATH)
    yield


app = FastAPI(title="Workout Agent", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")
app.mount("/assets", StaticFiles(directory=str(_BASE_DIR.parent / "frontend/dist/frontend/browser")), name="angular_assets")

if WEB_AUTH_SECRET:

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            path = request.url.path
            # Allow static files and auth endpoints
            if path.startswith("/static") or path in [
                "/login",
                "/login/google",
                "/logout",
                "/auth",
                "/google-health/callback",
                "/favicon.ico",
                "/sw.js",
            ]:
                return await call_next(request)
            if not request.session.get("user"):
                # API routes: return 401 JSON instead of a redirect so fetch()
                # callers get a clear error instead of the login page HTML.
                if path.startswith("/api/"):
                    return HTMLResponse(
                        '{"detail":"Not authenticated"}',
                        status_code=401,
                        media_type="application/json",
                    )
                return RedirectResponse("/login")
            return await call_next(request)

    app.add_middleware(AuthMiddleware)

# Always mount SessionMiddleware so templates and routes can safely access
# request.session even when web auth is disabled. Without it, any access to
# request.session raises an AssertionError -> 500 Internal Server Error.
# When WEB_AUTH_SECRET is unset we fall back to a per-process random key; this
# is fine because there is nothing sensitive to protect when auth is off.
app.add_middleware(
    SessionMiddleware,
    secret_key=WEB_AUTH_SECRET or secrets.token_hex(32),
)


# @app.get("/login")
async def login(request: Request) -> Any:
    if not WEB_GOOGLE_CLIENT_ID:
        return HTMLResponse("Web auth is not configured.", status_code=500)
    if request.session.get("user"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "login.html")


@app.get("/login/google")
async def login_google(request: Request) -> Any:
    if not WEB_GOOGLE_CLIENT_ID:
        return HTMLResponse("Web auth is not configured.", status_code=500)
    redirect_uri = str(request.url_for("auth"))
    if "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/api/me")
async def get_current_user_api(request: Request) -> Any:
    user = request.session.get("user")
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({"user": user})


@app.get("/auth")
async def auth(request: Request) -> Any:
    if not WEB_GOOGLE_CLIENT_ID:
        return RedirectResponse("/")
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:  # noqa: BLE001
        logger.warning("OAuth token exchange failed: %s", e)
        return RedirectResponse("/login")
    user = token.get("userinfo")
    if user:
        email = user.get("email", "")
        if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
            return HTMLResponse(f"Unauthorized email: {email}", status_code=403)
        # Create or retrieve the user record on first login.
        user_record = get_or_create_user(
            email,
            display_name=user.get("name"),
            db_path=DB_PATH,
        )
        request.session["user"] = email
        request.session["user_id"] = user_record["id"]
    return RedirectResponse("/")


# Automated hype lines. One is shown per day, chosen deterministically from the
# date, so the dashboard always greets you without an API call or a button.
_QUOTES = [
    "Show up today and the bar gets lighter tomorrow. One more rep than last time.",
    "Strength is a skill you practise. Brace, pull, repeat.",
    "Small jumps, stacked weekly, become a competition physique. Keep stacking.",
    "Your future deadlift PR is built on the boring set you log today.",
    "Discipline now, six-pack later. Hit your protein and your top set.",
    "The last clean rep is where the growth lives. Earn it.",
    "Win the morning weigh-in by winning last night's sleep.",
    "Tight waist, wide back, heavy bar. Stay the course.",
    "You do not need motivation, you need the next set. Go.",
    "Consistency is the cheat code. You already have it.",
    "Add a kilo or add a rep. Either way, move forward.",
    "Recovery is training too. Eat, sleep, repeat.",
]


def _daily_quote(today: date | None = None) -> str:
    today = today or datetime.now(tz=timezone.utc).date()
    return _QUOTES[today.toordinal() % len(_QUOTES)]


def _epley_1rm(weight: float | None, reps: int | None) -> float | None:
    return analytics.epley_1rm(weight, reps)


def _weight_on_or_before(weights: list[tuple[str, float]], when: str) -> float | None:
    """Latest bodyweight recorded on or before a date (weights sorted ascending)."""
    result = None
    for when_w, value in weights:
        if when_w <= when:
            result = value
        else:
            break
    return result


def _find_lift_series(series: dict[str, Any], *keywords: str) -> tuple[str | None, list[Any]]:
    """Find an exercise whose name contains all (then any) of the keywords."""
    for name, entries in series.items():
        low = name.lower()
        if all(k in low for k in keywords):
            return name, entries
    for name, entries in series.items():
        low = name.lower()
        if any(k in low for k in keywords):
            return name, entries
    return None, []


def _rep_top(rep_range: str) -> int | None:
    digits = [int(n) for n in rep_range.replace("-", " ").split() if n.isdigit()]
    return digits[-1] if digits else None


def _overload_nudge(planned_rep_range: str, best: dict[str, Any] | None) -> str:
    """Suggest the next progressive-overload step for an exercise."""
    if not best or best.get("top_reps") is None:
        return "Log this lift to start tracking progress."
    top = _rep_top(planned_rep_range)
    reps = best["top_reps"]
    weight = best.get("top_weight_kg")
    if top is not None and reps >= top:
        if weight:
            return "You hit the top of the range. Add 2.5 kg next time."
        return "You hit the top of the range. Add a rep or a little load."
    if top is not None:
        return f"Chase {top} clean reps, then add load."
    return "Keep the reps strict and progress when it feels easy."


def _format_best(best: dict[str, Any] | None) -> str:
    if not best:
        return "No data yet"
    weight = best.get("top_weight_kg")
    reps = best.get("top_reps")
    if weight is not None and reps is not None:
        return f"{weight:g} kg x {reps}"
    if reps is not None:
        return f"{reps} reps"
    return "No data yet"


def _training_levels(user_id: str | None = None) -> dict[str, int]:
    """Map ISO dates to a calendar-heatmap intensity (0-4)."""
    levels: dict[str, int] = {}
    for log in get_daily_logs(limit=400, db_path=DB_PATH, user_id=user_id):
        levels[log["date"]] = 2 if log["day"] is not None else 1
    # A session with logged sets is the strongest signal of a completed workout.
    for session in get_session_volumes(db_path=DB_PATH, user_id=user_id):
        levels[session["date"]] = 4
    return levels


def _current_streak(levels: dict[str, int]) -> int:
    """Count consecutive recent days that had any logged activity."""
    streak = 0
    day = datetime.now(tz=timezone.utc).date()
    # Allow today to be empty (the morning run may not have happened yet).
    if levels.get(day.isoformat(), 0) == 0:
        day -= timedelta(days=1)
    while levels.get(day.isoformat(), 0) > 0:
        streak += 1
        day -= timedelta(days=1)
    return streak


def _dashboard_context(today: date | None = None, user_id: str | None = None) -> dict:
    if today is None:
        today = datetime.now(tz=timezone.utc).date()
    start = get_programme_start_date(DB_PATH)
    week = week_in_cycle(start, today)
    block = block_for_week(week)
    bests = get_recent_bests(DB_PATH, user_id=user_id)
    bests_norm = {normalise_name(name): best for name, best in bests.items()}

    # Check for a user-selected active programme.
    active = get_active_programme(user_id, db_path=DB_PATH) if user_id else None
    active_defn = active.get("definition") if active else None

    day = today_day(today)
    rows: list[dict[str, Any]] = []
    focus = "Rest & Recovery"

    if active_defn:
        days_data = active_defn.get("days", [])
        total_days = len(days_data) or 1
        current_day = ((today - start).days % total_days) + 1
        active_day = next(
            (d for d in days_data if d.get("number") == current_day), None
        )
        if active_day:
            focus = active_day.get("focus", "Training")
            for ex in active_day.get("exercises", []):
                best = bests_norm.get(normalise_name(ex.get("name", "")))
                rows.append(
                    {
                        "name": ex.get("name", "?"),
                        "planned": f"{ex.get('sets', 0)} x {ex.get('rep_range', '?')}",
                        "note": ex.get("note", ""),
                        "last": _format_best(best),
                        "nudge": _overload_nudge(ex.get("rep_range", ""), best),
                    }
                )
    elif day is not None:
        focus = day_focus(day)
        for ex in day_exercises(day, block):
            best = bests_norm.get(normalise_name(ex.name))
            rows.append(
                {
                    "name": ex.name,
                    "planned": f"{ex.sets} x {ex.rep_range}",
                    "note": ex.note,
                    "last": _format_best(best),
                    "nudge": _overload_nudge(ex.rep_range, best),
                },
            )

    metrics = get_body_metrics(db_path=DB_PATH, user_id=user_id)
    latest_weight = metrics[-1]["weight_kg"] if metrics else None
    recovery_like = {"weight_kg": latest_weight} if latest_weight else None
    guidance = lifestyle.daily_guidance(day, day is None, recovery_like)

    levels = _training_levels(user_id=user_id)
    streak = _current_streak(levels)
    week_in_block = ((week - 1) % BLOCK_WEEKS) + 1

    weight_spark = charts.sparkline([m["weight_kg"] for m in metrics if m["weight_kg"]])
    fat_spark = charts.sparkline(
        [m["body_fat_pct"] for m in metrics if m["body_fat_pct"]],
        colour=charts.WARN,
    )
    latest_fat = next(
        (m["body_fat_pct"] for m in reversed(metrics) if m["body_fat_pct"]),
        None,
    )

    review = insights.build_insights(
        get_progress_history(db_path=DB_PATH, user_id=user_id),
        metrics,
        None,
    )

    return {
        "active": "today",
        "quote": _daily_quote(today),
        "week": week,
        "cycle_weeks": CYCLE_WEEKS,
        "block": block,
        "week_in_block": week_in_block,
        "block_weeks": BLOCK_WEEKS,
        "focus": focus,
        "is_rest_day": day is None,
        "weekday": today.strftime("%A"),
        "rows": rows,
        "lifestyle": guidance.as_lines(),
        "cycle_ring": charts.progress_ring(
            week / CYCLE_WEEKS * 100,
            label=f"Wk {week}",
            sub=f"of {CYCLE_WEEKS}",
        ),
        "block_ring": charts.progress_ring(
            week_in_block / BLOCK_WEEKS * 100,
            label=block.name.split(" ")[0],
            sub=f"wk {week_in_block}/{BLOCK_WEEKS}",
            colour=charts.ACCENT_2,
        ),
        "streak": streak,
        "calendar": charts.calendar_heatmap(levels),
        "weight": f"{latest_weight:g} kg" if latest_weight else None,
        "weight_spark": weight_spark,
        "body_fat": f"{latest_fat:g} %" if latest_fat else None,
        "fat_spark": fat_spark,
        "review_headline": review.headline,
        "review_recovery": review.recovery.as_text(),
        "review_lifts": review.lifts,
        "dashboard_insight": get_dashboard_insight(db_path=DB_PATH),
    }


FRONTEND_DIST = _BASE_DIR.parent / "frontend/dist/frontend/browser"

@app.get("/")
def dashboard(request: Request):
    return FileResponse(FRONTEND_DIST / "index.html")

@app.get("/api/dashboard")
def api_dashboard(request: Request) -> JSONResponse:
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    from fastapi.encoders import jsonable_encoder
    ctx = _dashboard_context(user_id=user_id)
    return JSONResponse(content=jsonable_encoder(ctx))


# @app.get("/progress")
def progress(request: Request) -> Any:
    user_id = request.session.get("user_id")
    series = get_progress_history(db_path=DB_PATH, user_id=user_id)
    charts_data = []
    for name in sorted(series):
        entries = series[name]
        points = [
            {
                "date": e["date"][5:],
                "value": e["top_weight_kg"],
                "label": _format_best(e),
            }
            for e in entries
        ]
        e1rms = [_epley_1rm(e["top_weight_kg"], e["top_reps"]) for e in entries]
        e1rms_filtered = [v for v in e1rms if v is not None]
        best_e1rm: float | None = max(e1rms_filtered) if e1rms_filtered else None
        charts_data.append(
            {
                "name": name,
                "svg": charts.line_chart(points, unit="kg"),
                "best_e1rm": best_e1rm,
                "sessions": len(entries),
            },
        )
    return templates.TemplateResponse(
        request,
        "progress.html",
        {
            "active": "progress",
            "charts": charts_data,
            "body": _body_charts(user_id=user_id),
        },
    )


def _body_charts(*, user_id: str | None = None) -> dict[str, str | None]:
    readings = get_body_metrics(db_path=DB_PATH, user_id=user_id)

    def _series(key: str, unit: str, colour: str) -> str | None:
        points = [
            {"date": r["date"][5:], "value": r[key], "label": f"{r[key]:g} {unit}"}
            for r in readings
            if r[key] is not None
        ]
        if len(points) < 2:
            return None
        return charts.line_chart(points, unit=unit, colour=colour)

    return {
        "weight": _series("weight_kg", "kg", charts.ACCENT),
        "body_fat": _series("body_fat_pct", "%", charts.WARN),
        "muscle": _series("muscle_pct", "%", charts.ACCENT_2),
        "resting_hr": _series("resting_hr", "bpm", charts.PINK),
    }


# @app.get("/stats")
def stats(request: Request):
    user_id = request.session.get("user_id")
    volumes = get_session_volumes(db_path=DB_PATH)
    prs = get_personal_records(db_path=DB_PATH)
    logs = get_daily_logs(limit=400, db_path=DB_PATH, user_id=user_id)
    start = get_programme_start_date(DB_PATH)
    series = get_progress_history(db_path=DB_PATH)
    today = datetime.now(tz=timezone.utc).date()
    week = week_in_cycle(start, today)

    # Headline totals.
    total_sessions = len(volumes)
    total_volume = sum(v["volume"] for v in volumes)
    days_on_programme = (today - start).days

    # Training split distribution (donut) from the daily log.
    focus_counts: dict[str, int] = {}
    for log in logs:
        if log["day"] is not None:
            focus_counts[log["focus"]] = focus_counts.get(log["focus"], 0) + 1
    distribution = charts.donut(
        [{"label": k, "value": v} for k, v in sorted(focus_counts.items())],
    )

    # Volume broken down by muscle group.
    groups = analytics.group_volumes(
        get_exercise_volumes(db_path=DB_PATH, user_id=user_id),
    )
    muscle_donut = charts.donut(
        [
            {"label": g, "value": v}
            for g, v in sorted(groups.items(), key=lambda kv: -kv[1])
        ]
    )

    # Session-load trend over the most recent sessions.
    recent = volumes[-14:]
    volume_bars = charts.bar_chart(
        [
            {"label": v["date"][5:], "value": v["volume"], "caption": v["date"]}
            for v in recent
        ],
        unit="kg",
    )

    # Advanced AI Widgets
    biometrics = get_body_metrics(db_path=DB_PATH, user_id=user_id)
    block_phase_svg = ai_widgets.block_phase_tracker(volumes)
    recovery_grid_svg = ai_widgets.systemic_recovery_correlation(biometrics, volumes)
    vol_dist_svg = ai_widgets.volume_distribution(groups)

    # Strength score (DOTS) and strength-to-bodyweight ratio over time, built
    # from the deadlift e1RM against the bodyweight recorded at the time.
    body = get_body_metrics(db_path=DB_PATH, user_id=user_id)
    weights = [(m["date"], m["weight_kg"]) for m in body if m["weight_kg"]]
    _, dl_entries = _find_lift_series(series, "deadlift")
    dots_points, ratio_points = [], []
    for e in dl_entries:
        e1rm = analytics.epley_1rm(e["top_weight_kg"], e["top_reps"])
        bw = _weight_on_or_before(weights, e["date"])
        if not e1rm or not bw:
            continue
        score = analytics.dots_score(bw, e1rm)
        if score:
            dots_points.append(
                {"date": e["date"][5:], "value": score, "label": f"{score:g}"}
            )
        ratio_points.append(
            {
                "date": e["date"][5:],
                "value": round(e1rm / bw, 2),
                "label": f"{e1rm / bw:.2f}x",
            }
        )
    dots_chart = (
        charts.line_chart(dots_points, colour=charts.PURPLE)
        if len(dots_points) > 1
        else None
    )
    ratio_chart = (
        charts.line_chart(ratio_points, unit="x", colour=charts.ACCENT_2)
        if len(ratio_points) > 1
        else None
    )

    # Project the main lifts to the end of the current 12-week cycle.
    target_ordinal = today.toordinal() + max(0, (CYCLE_WEEKS - week)) * 7
    projections = [
        _project_lift("Deadlift", dl_entries, target_ordinal, metric="e1rm"),
        _project_lift("Pull-ups", _find_lift_series(series, "pull")[1], target_ordinal),
    ]
    projections = [p for p in projections if p]

    pr_rows = [
        {
            "exercise": pr["exercise"],
            "e1rm": round(pr["e1rm"], 1),
            "detail": f"{pr['weight_kg']:g} kg x {pr['reps']}",
            "date": pr["date"],
        }
        for pr in prs
    ]

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "active": "stats",
            "total_sessions": total_sessions,
            "total_volume": round(total_volume),
            "days_on_programme": days_on_programme,
            "exercises_tracked": len(prs),
            "distribution": distribution,
            "has_distribution": bool(focus_counts),
            "muscle_donut": muscle_donut,
            "has_muscle": bool(groups),
            "volume_bars": volume_bars,
            "has_volume": bool(recent),
            "dots_chart": dots_chart,
            "ratio_chart": ratio_chart,
            "projections": projections,
            "prs": pr_rows,
            "block_phase_svg": block_phase_svg,
            "recovery_grid_svg": recovery_grid_svg,
            "vol_dist_svg": vol_dist_svg,
        },
    )


def _project_lift(
    label: str, entries: list[Any], target_ordinal: int, *, metric: str = "auto"
) -> dict[str, Any] | None:
    """Build a projection card for a lift at the end of the cycle."""
    points: list[tuple[float, float]] = []
    unit = "kg"
    use_e1rm = metric == "e1rm"
    if metric == "auto":
        use_e1rm = any(e["top_weight_kg"] for e in entries)
    for e in entries:
        value = (
            analytics.epley_1rm(e["top_weight_kg"], e["top_reps"])
            if use_e1rm
            else e["top_reps"]
        )
        if value is None:
            continue
        points.append((float(date.fromisoformat(e["date"]).toordinal()), float(value)))
    if len(points) < 2:
        return None
    if not use_e1rm:
        unit = "reps"
    current = points[-1][1]
    projected = analytics.project(points, target_ordinal)
    if projected is None:
        return None
    projected = max(projected, current)  # never project a regression below today
    return {
        "label": label,
        "current": f"{current:g} {unit}",
        "projected": f"{projected:g} {unit}",
        "metric": "est. 1RM" if use_e1rm else "top reps",
        "gain": round(projected - current, 1),
    }


# @app.get("/plan")
def plan(request: Request):
    today = datetime.now(tz=timezone.utc).date()
    week = week_in_cycle(get_programme_start_date(DB_PATH), today)
    current_block = block_for_week(week)
    day = today_day(today)

    days = []
    for d in range(1, 7):
        days.append(
            {
                "number": d,
                "focus": day_focus(d),
                "is_today": d == day,
                "exercises": [
                    {
                        "name": ex.name,
                        "scheme": f"{ex.sets} x {ex.rep_range}",
                        "note": ex.note,
                    }
                    for ex in day_exercises(d, current_block)
                ],
            },
        )

    blocks = [
        {
            "number": b.number,
            "name": b.name,
            "weeks": b.weeks,
            "focus": b.focus,
            "deadlift": f"{b.deadlift.sets} x {b.deadlift.rep_range}",
            "pullups": f"{b.pullups.sets} x {b.pullups.rep_range}",
            "accessory": b.accessory_emphasis,
            "is_current": b.number == current_block.number,
        }
        for b in BLOCKS.values()
    ]

    return templates.TemplateResponse(
        request,
        "plan.html",
        {
            "active": "plan",
            "split_name": SPLIT_NAME,
            "week": week,
            "cycle_weeks": CYCLE_WEEKS,
            "current_block": current_block,
            "blocks": blocks,
            "days": days,
            "rules": COACHING_RULES,
        },
    )


def _render_active_plan(request: Request, active: dict, today: date):
    """Render /plan from a user's active programme definition (DB-stored)."""
    defn = active.get("definition", {})
    split_name = defn.get("name", "Active Programme")
    cycle_weeks = defn.get("cycle_weeks", 4)
    days_data = defn.get("days", [])
    blocks_data = defn.get("blocks", [])
    rules = defn.get("rules", [])

    # Determine today's day in the split (simple round-robin based on start date).
    start = get_programme_start_date(DB_PATH)
    # current_day is the 1-based day index in the split, wrapping
    total_days = len(days_data) or 1
    current_day = ((today - start).days % total_days) + 1

    days = []
    for d in days_data:
        day_num = d.get("number", 0)
        exercises = [
            {
                "name": ex.get("name", "?"),
                "scheme": f"{ex.get('sets', 0)} x {ex.get('rep_range', '?')}",
                "note": ex.get("note", ""),
            }
            for ex in d.get("exercises", [])
        ]
        days.append(
            {
                "number": day_num,
                "focus": d.get("focus", f"Day {day_num}"),
                "is_today": day_num == current_day,
                "exercises": exercises,
            }
        )

    blocks = []
    for b in blocks_data:
        blocks.append(
            {
                "number": b.get("number", 1),
                "name": b.get("name", "Block"),
                "weeks": b.get("weeks", "1-4"),
                "focus": b.get("focus", ""),
                "deadlift": _block_lift_str(b.get("deadlift")),
                "pullups": _block_lift_str(b.get("pullups")),
                "accessory": b.get("accessory_emphasis", ""),
                "is_current": True,
            }
        )

    return templates.TemplateResponse(
        request,
        "plan.html",
        {
            "active": "plan",
            "split_name": split_name,
            "week": current_day,
            "cycle_weeks": cycle_weeks,
            "current_block": None,
            "blocks": blocks,
            "days": days,
            "rules": rules,
        },
    )


def _block_lift_str(lift: dict | None) -> str:
    """Format a lift dict as 'sets x rep_range' or empty string."""
    if not lift:
        return ""
    sets = lift.get("sets", 0)
    rep_range = lift.get("rep_range", "")
    if not sets or not rep_range:
        return ""
    return f"{sets} x {rep_range}"


# @app.get("/programmes")
def programmes_page(request: Request) -> Any:
    """Page where users select or switch their workout programme."""
    user_id = request.session.get("user_id")
    templates_list = get_programme_templates()
    active = None
    if user_id:
        active = get_active_programme(user_id, db_path=DB_PATH)
    return templates.TemplateResponse(
        request,
        "programmes.html",
        {
            "active": "programmes",
            "templates": templates_list,
            "active_programme": active,
        },
    )


@app.post("/api/programmes/select")
async def select_programme(request: Request) -> dict[str, Any]:
    """Activate a programme template for the current user."""

    _check_rate_limit(request, limit=5)

    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    template_key = body.get("template_key", "").strip()
    if not template_key:
        raise HTTPException(status_code=400, detail="template_key is required")

    # Validate against known templates.
    known_keys = {t["key"] for t in get_programme_templates()}
    if template_key not in known_keys and template_key != "custom":
        raise HTTPException(
            status_code=400,
            detail=f"Unknown template '{template_key}'. Available: {', '.join(sorted(known_keys))}",
        )

    source = "template"
    definition = body.get("definition")

    if template_key == "infer_from_hevy":
        source = "inferred"
        try:
            definition = _run_hevy_inference(user_id)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Hevy inference failed for user %s.", user_id)
            raise HTTPException(
                status_code=500,
                detail="Could not infer programme from Hevy. Check your API key in Settings.",
            ) from exc
    elif template_key == "custom":
        source = "custom"

    set_active_programme(
        user_id,
        source=source,
        template_key=template_key,
        definition=definition,
        db_path=DB_PATH,
    )
    return {"status": "ok", "template_key": template_key}


def _build_inferred_definition(
    inferred: InferredProgramme,
) -> dict:
    """Build a programme definition dict from an InferredProgramme.

    Produces a structure compatible with the programme templates system
    so the programmes.html preview and /plan page render correctly.
    """
    days: list[dict] = []
    for i, day in enumerate(inferred.training_days):
        exercises = []
        for ex in day.exercises:
            exercises.append(
                {
                    "name": ex.title,
                    "sets": ex.sets,
                    "rep_range": f"{ex.target_reps or 8}-{ex.target_reps or 12}",
                    "note": ex.notes or "",
                    "template_id": ex.template_id,
                }
            )
        days.append(
            {
                "number": i + 1,
                "focus": day.focus_summary(),
                "exercises": exercises,
            }
        )

    # Build a simple block structure from the inferred data.
    blocks: list[dict] = [
        {
            "number": 1,
            "name": "Inferred",
            "weeks": f"1-{len(inferred.training_days) or 6}",
            "focus": (
                f"Data-driven {inferred.split_type.replace('_', ' ').title()} split "
                f"at {inferred.sessions_per_week} sessions/week."
            ),
            "deadlift": {"sets": 0, "rep_range": "", "note": "", "template_id": ""},
            "pullups": {"sets": 0, "rep_range": "", "note": "", "template_id": ""},
            "accessory_emphasis": "",
        }
    ]

    return {
        "name": f"Inferred: {inferred.split_type.replace('_', ' ').title()}",
        "cycle_weeks": 4,
        "total_days": len(inferred.training_days),
        "blocks": blocks,
        "days": days,
        "rules": [
            f"Inferred split type: {inferred.split_type.replace('_', ' ').title()}.",
            f"Training {inferred.sessions_per_week} sessions per week.",
            f"Next suggested routine: {inferred.next_routine.title if inferred.next_routine else 'none'}.",
            "Derived from your Hevy routine templates and workout history.",
        ],
    }


def _run_hevy_inference(user_id: str) -> dict:
    """Fetch Hevy data and infer the user's training programme.

    Raises HTTPException if the user has no Hevy API key configured or if
    the inference fails.
    """
    from hevy_reader import fetch_user_training
    from programme_inference import infer_programme

    keys = get_user_api_keys(user_id, db_path=DB_PATH)
    hevy_key = keys.get("hevy", {}).get("api_key", "").strip()
    if not hevy_key:
        raise HTTPException(
            status_code=400,
            detail="No Hevy API key configured. Add your key in Settings first.",
        )

    training_data = fetch_user_training(hevy_key)
    if not training_data.routines and not training_data.recent_workouts:
        raise HTTPException(
            status_code=400,
            detail="No routines or workouts found in your Hevy account. Create some routines first.",
        )

    inferred = infer_programme(training_data)
    return _build_inferred_definition(inferred)


# @app.get("/history")
def history(request: Request):
    user_id = request.session.get("user_id")
    logs = get_daily_logs(limit=60, db_path=DB_PATH, user_id=user_id)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "active": "history",
            "calendar": charts.calendar_heatmap(_training_levels(user_id=user_id)),
            "logs": logs,
        },
    )


# @app.get("/checkins")
def checkins(request: Request) -> Any:
    user_id = request.session.get("user_id")
    checkins_list = get_checkins(db_path=DB_PATH, user_id=user_id)
    total_sessions = len(get_session_volumes(db_path=DB_PATH, user_id=user_id))
    sessions_since = total_sessions % 24
    sessions_left = 24 - sessions_since
    progress_pct = (sessions_since / 24) * 100

    loading_svg = f'''
    <svg class="svg-chart" width="100%" height="24" style="background: var(--panel-2); border-radius: 12px; margin-top: 15px;">
        <rect width="{progress_pct}%" height="100%" fill="var(--accent)" rx="12"></rect>
        <text x="50%" y="16" fill="#000" font-size="12" font-weight="600" text-anchor="middle">{sessions_left} sessions remaining until next analysis</text>
    </svg>
    '''
    return templates.TemplateResponse(
        request,
        "checkins.html",
        {"active": "checkins", "checkins": checkins_list, "loading_svg": loading_svg},
    )


@app.get("/api/xai_reasoning/{context_id}")
def xai_reasoning(context_id: str, request: Request) -> dict[str, Any]:
    _check_rate_limit(request)
    user_id = request.session.get("user_id")
    # context_id is expected to be {date}_{exercise_name}
    user_id = request.session.get("user_id")

    existing = get_reasoning_log(context_id, db_path=DB_PATH, user_id=user_id)
    if existing:
        return {"reasoning": existing}

    parts = context_id.split("_", 1)
    if len(parts) != 2:
        return {"reasoning": "Invalid context ID"}

    when, ex_name = parts

    config = get_config()
    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(config.gemini_model)

    history = get_progress_history(db_path=DB_PATH).get(ex_name, [])

    prompt = f"Why did my volume/performance change for {ex_name} around {when}? Here is my history: {json.dumps(history)}. Provide a clear causal explanation in a few sentences."
    response = model.generate_content(prompt)
    reasoning = (response.text or "Could not determine reasoning.").strip()

    save_reasoning_log(context_id, ex_name, reasoning, db_path=DB_PATH, user_id=user_id)
    return {"reasoning": reasoning}


@app.get("/api/project_peak")
def project_peak(request: Request) -> dict[str, Any]:
    _check_rate_limit(request, limit=5)
    request.session.get("user_id")
    config = get_config()
    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(config.gemini_model)

    series = get_progress_history(db_path=DB_PATH)
    dl_entries = series.get("Deadlift", [])
    pu_entries = series.get("Pull-ups", [])

    prompt = f"Analyze this historical progression for Deadlift: {json.dumps(dl_entries)} and Pull-ups: {json.dumps(pu_entries)}. Project the estimated 1RM at the end of the 12-week peaking phase. Adjust the forecast curve if recent sessions look 'bad'. Return JSON: {{'Deadlift_Projected': float, 'Pullups_Projected': float, 'Validation': 'string explanation'}}"
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        text = text.removeprefix("```json")
        text = text.removesuffix("```")
        return json.loads(text.strip())
    except Exception:  # noqa: BLE001
        return {"error": "Failed to project peak."}


# @app.get("/chat")
def chat_page(request: Request):
    user_id = request.session.get("user_id")
    messages = get_chat_messages(limit=50, db_path=DB_PATH, user_id=user_id)
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"active": "chat", "messages": messages},
    )


@app.get("/api/chat/history")
def chat_history(request: Request):
    user_id = request.session.get("user_id")
    return get_chat_messages(limit=50, db_path=DB_PATH, user_id=user_id)


@app.post("/api/chat/clear")
def chat_clear(request: Request):
    user_id = request.session.get("user_id")
    clear_chat_messages(db_path=DB_PATH, user_id=user_id)
    return {"status": "ok"}


@app.get("/api/rag_search", response_model=None)
def rag_search(request: Request, q: str = Query(...)) -> Any:
    _check_rate_limit(request, limit=15)
    user_id = request.session.get("user_id")
    config = get_config()
    provider = _resolve_provider_for_request(request, config)

    # Gather training context
    user_id = request.session.get("user_id")
    logs = get_daily_logs(limit=30, db_path=DB_PATH, user_id=user_id)
    history = get_progress_history(db_path=DB_PATH)
    biometrics = get_body_metrics(db_path=DB_PATH)
    prs = get_personal_records(db_path=DB_PATH)

    context = json.dumps(
        {
            "logs": logs,
            "history": history,
            "biometrics": biometrics[-10:],
            "prs": prs[:10],
        },
        default=str,
    )

    # Build multi-turn conversation from chat history
    chat_history_msgs = get_chat_messages(limit=20, db_path=DB_PATH, user_id=user_id)
    conversation_lines = []
    for msg in chat_history_msgs:
        role_label = "User" if msg["role"] == "user" else "Coach"
        conversation_lines.append(f"{role_label}: {msg['content']}")
    conversation_text = (
        "\n".join(conversation_lines) if conversation_lines else "No previous messages."
    )

    # Save user message
    save_chat_message("user", q, db_path=DB_PATH, user_id=user_id)

    prompt = f"""You are Coach, an elite powerbuilding AI coach embedded in Elgan's training dashboard.
You have full access to his training logs, body composition data, personal records, and programme history.

Your personality:
- Knowledgeable, direct, and encouraging. Like a trusted coach who knows the data.
- Reference specific numbers, dates, and exercises from the context when relevant.
- Keep answers concise but insightful. You can use **bold** for emphasis and bullet lists for structure.
- Use British English.

Conversation so far:
{conversation_text}

Training data context (recent logs, progress history, biometrics, PRs):
{context[:25000]}

User's new message: {q}

Respond naturally as Coach. If the question is about their training data, reference the actual numbers. If it is a general fitness question, answer from expertise but relate it back to their programme where possible."""

    def generate():
        collected: list[str] = []
        try:
            response = provider.generate(prompt, stream=True)
            for chunk in response:
                if chunk.text:
                    collected.append(chunk.text)
                    yield chunk.text
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error during Gemini streaming: {e}")
            error_msg = "Sorry, Coach is currently unavailable or encountered an error. Please try again."
            collected.append(error_msg)
            yield error_msg
        finally:
            # Save the full assistant response
            full_response = "".join(collected)
            if full_response:
                save_chat_message("assistant", full_response, db_path=DB_PATH, user_id=user_id)

    return StreamingResponse(generate(), media_type="text/plain")


def _gh_redirect_uri(request: Request) -> str:
    """The OAuth redirect URI, explicit env override or derived from the request."""
    return GH_REDIRECT_URI or str(request.url_for("google_health_callback"))


# @app.get("/settings")
def settings(request: Request) -> Any:
    user_id = request.session.get("user_id")
    user_keys: dict[str, Any] = {}
    user_prefs: dict[str, Any] = {}
    if user_id:
        user_keys = get_user_api_keys(user_id, db_path=DB_PATH)
        user_prefs = get_user_preferences(user_id, db_path=DB_PATH)

    # Mask keys for display (show last 4 chars only).
    masked_keys: dict[str, Any] = {}
    for provider, data in user_keys.items():
        key = data.get("api_key", "")
        masked_keys[provider] = {
            "masked": f"{'●' * 12}{key[-4:]}"
            if len(key) > 4
            else ("●" * len(key) if key else ""),
            "has_key": bool(key),
            "updated_at": data.get("updated_at", ""),
        }

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "gh_configured": bool(GH_CLIENT_ID and GH_CLIENT_SECRET),
            "gh_connected": bool(get_meta(_GH_TOKEN_KEY, DB_PATH, user_id=user_id)),
            "gh_status": request.query_params.get("gh"),
            "user_keys": masked_keys,
            "user_prefs": user_prefs,
            "ai_providers": available_providers(),
            "key_status": request.query_params.get("key_status"),
            "pref_status": request.query_params.get("pref_status"),
        },
    )


@app.post("/api/settings/key")
async def save_api_key(request: Request) -> dict[str, str]:
    """Save or update an API key for the current user."""
    _check_rate_limit(request, limit=5)
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    provider = body.get("provider", "").strip().lower()
    api_key = body.get("api_key", "").strip()

    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="Provider and api_key are required")

    valid_providers = {"hevy", "gemini", "claude", "openai", "deepseek"}
    if provider not in valid_providers:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider '{provider}'. Use: {', '.join(valid_providers)}",
        )

    # Optional model override for AI providers.
    extra = {}
    model = body.get("model", "").strip()
    if model:
        extra["model"] = model

    save_user_api_key(
        user_id,
        provider,
        api_key,
        extra=extra or None,
        db_path=DB_PATH,
    )
    return {"status": "ok", "provider": provider}


@app.post("/api/settings/key/delete")
async def remove_api_key(request: Request) -> dict[str, str]:
    """Remove a stored API key for the current user."""
    _check_rate_limit(request, limit=5)
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    provider = body.get("provider", "").strip().lower()
    if not provider:
        raise HTTPException(status_code=400, detail="Provider is required")

    delete_user_api_key(user_id, provider, db_path=DB_PATH)
    return {"status": "ok", "provider": provider}


@app.post("/api/settings/verify-hevy")
async def verify_hevy_key(request: Request) -> dict[str, Any]:
    """Test a Hevy API key by calling /v1/workouts/count."""
    _check_rate_limit(request, limit=5)
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    api_key = body.get("api_key", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")

    from hevy_client import get_workout_count

    count = get_workout_count(api_key)
    if count is not None:
        return {"status": "ok", "workout_count": count}
    return {
        "status": "error",
        "detail": "Could not connect to Hevy. Check the API key.",
    }


@app.post("/api/settings/preferences")
async def save_preferences(request: Request) -> dict[str, str]:
    """Save user training preferences."""
    _check_rate_limit(request, limit=5)
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    save_user_preferences(
        user_id,
        goals=body.get("goals"),
        constraints=body.get("constraints"),
        experience_level=body.get("experience_level"),
        coaching_style=body.get("coaching_style"),
        preferred_ai=body.get("preferred_ai"),
        ai_model=body.get("ai_model"),
        custom_rules=body.get("custom_rules"),
        db_path=DB_PATH,
    )
    return {"status": "ok"}


@app.get("/google-health/connect")
def google_health_connect(request: Request) -> RedirectResponse:
    """Start the Google Health OAuth flow and redirect to the consent screen."""
    _check_rate_limit(request, limit=5)
    if not (GH_CLIENT_ID and GH_CLIENT_SECRET):
        return RedirectResponse("/settings?gh=unconfigured", status_code=303)
    user_id = request.session.get("user_id")
    state = secrets.token_urlsafe(16)
    set_meta(_GH_STATE_KEY, state, DB_PATH, user_id=user_id)
    url = build_authorize_url(
        GH_CLIENT_ID,
        state,
        redirect_uri=_gh_redirect_uri(request),
    )
    return RedirectResponse(url, status_code=303)


@app.get("/google-health/callback", name="google_health_callback")
def google_health_callback(request: Request) -> RedirectResponse:
    """Receive Google's redirect, swap the code for a refresh token, store it."""
    if not (GH_CLIENT_ID and GH_CLIENT_SECRET):
        return RedirectResponse("/settings?gh=unconfigured", status_code=303)
    if request.query_params.get("error"):
        return RedirectResponse("/settings?gh=denied", status_code=303)
    user_id = request.session.get("user_id")
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    expected = get_meta(_GH_STATE_KEY, DB_PATH, user_id=user_id)
    set_meta(_GH_STATE_KEY, "", DB_PATH, user_id=user_id)  # one-time use
    if not code or not state or not expected or state != expected:
        return RedirectResponse("/settings?gh=error", status_code=303)
    tokens = exchange_code(
        GH_CLIENT_ID,
        GH_CLIENT_SECRET,
        code,
        redirect_uri=_gh_redirect_uri(request),
    )
    if not tokens or not tokens.get("refresh_token"):
        return RedirectResponse("/settings?gh=error", status_code=303)
    set_meta(_GH_TOKEN_KEY, tokens["refresh_token"], DB_PATH, user_id=user_id)
    return RedirectResponse("/settings?gh=connected", status_code=303)


@app.post("/google-health/disconnect")
def google_health_disconnect(request: Request) -> RedirectResponse:
    """Forget the stored refresh token so the agent stops syncing."""
    _check_rate_limit(request, limit=5)
    user_id = request.session.get("user_id")
    set_meta(_GH_TOKEN_KEY, "", DB_PATH, user_id=user_id)
    return RedirectResponse("/settings?gh=disconnected", status_code=303)


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """Serve the service worker from the root so it can control every page.

    A worker only controls URLs within its own path, so it must be served from
    "/" (with the Service-Worker-Allowed header) rather than from /static.
    """
    return FileResponse(
        _BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


# --- AUTO-GENERATED API ROUTES FOR ANGULAR ---
from fastapi.encoders import jsonable_encoder


def _check_api_auth(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return uid

@app.get("/api/progress")
def api_progress(request: Request):
    user_id = _check_api_auth(request)
    series = get_progress_history(db_path=DB_PATH, user_id=user_id)
    charts_data = []
    for name in sorted(series):
        entries = series[name]
        points = [{"date": e["date"][5:], "value": e["top_weight_kg"], "label": _format_best(e)} for e in entries]
        e1rms = [_epley_1rm(e["top_weight_kg"], e["top_reps"]) for e in entries]
        e1rms_filtered = [v for v in e1rms if v is not None]
        best_e1rm = max(e1rms_filtered) if e1rms_filtered else None
        charts_data.append({
            "name": name,
            "svg": charts.line_chart(points, unit="kg"),
            "best_e1rm": best_e1rm,
            "sessions": len(entries),
        })
    return JSONResponse(jsonable_encoder({
        "charts": charts_data,
        "body": _body_charts(user_id=user_id)
    }))

@app.get("/api/stats")
def api_stats(request: Request):
    user_id = _check_api_auth(request)
    volumes = get_session_volumes(db_path=DB_PATH)
    prs = get_personal_records(db_path=DB_PATH)
    logs = get_daily_logs(limit=400, db_path=DB_PATH, user_id=user_id)
    start = get_programme_start_date(DB_PATH)
    get_progress_history(db_path=DB_PATH)
    today = datetime.now(tz=timezone.utc).date()
    week_in_cycle(start, today)
    
    total_sessions = len(volumes)
    total_volume = sum(v["volume"] for v in volumes)
    days_on_programme = (today - start).days
    
    focus_counts = {}
    for log in logs:
        focus_counts[log.focus] = focus_counts.get(log.focus, 0) + 1
    
    return JSONResponse(jsonable_encoder({
        "total_sessions": total_sessions,
        "total_volume": total_volume,
        "days_on_programme": days_on_programme,
        "prs": len(prs),
        "donut": charts.donut_chart(focus_counts) if focus_counts else None,
        "heatmap": charts.calendar_heatmap(_training_levels(user_id=user_id)),
        "volume_chart": charts.bar_chart([{"label": v["date"][5:], "value": v["volume"]} for v in volumes[-30:]]) if volumes else None,
    }))

@app.get("/api/history")
def api_history(request: Request):
    user_id = _check_api_auth(request)
    logs = get_daily_logs(limit=30, db_path=DB_PATH, user_id=user_id)
    return JSONResponse(jsonable_encoder({"logs": logs}))

@app.get("/api/plan")
def api_plan(request: Request):
    user_id = _check_api_auth(request)
    today = datetime.now(tz=timezone.utc).date()
    
    active = get_active_programme(user_id, db_path=DB_PATH) if user_id else None
    
    if active and active.get("definition"):
        defn = active["definition"]
        return JSONResponse(jsonable_encoder({
            "is_active_programme": True,
            "split_name": defn.get("split_name", active.get("name")),
            "days": defn.get("schedule", [])
        }))
        
    week = week_in_cycle(get_programme_start_date(DB_PATH), today)
    current_block = block_for_week(week)
    day = today_day(today)

    days = []
    for d in range(1, 7):
        days.append({
            "number": d,
            "focus": day_focus(d),
            "is_today": d == day,
            "exercises": [
                {
                    "name": ex.name,
                    "scheme": f"{ex.sets} x {ex.rep_range}",
                    "note": ex.note,
                }
                for ex in day_exercises(d, current_block)
            ],
        })

    blocks = [
        {
            "number": b.number,
            "name": b.name,
            "weeks": b.weeks,
            "focus": b.focus,
            "deadlift": f"{b.deadlift.sets} x {b.deadlift.rep_range}",
            "pullups": f"{b.pullups.sets} x {b.pullups.rep_range}",
            "accessory": b.accessory_emphasis,
            "is_current": b.number == current_block.number,
        }
        for b in BLOCKS.values()
    ]
    
    return JSONResponse(jsonable_encoder({
        "is_active_programme": False,
        "split_name": SPLIT_NAME,
        "current_week": week,
        "current_block": current_block.name,
        "days": days,
        "blocks": blocks
    }))


@app.get("/api/programmes")
def api_programmes(request: Request):
    user_id = _check_api_auth(request)
    return JSONResponse(jsonable_encoder({
        "current_state": get_active_programme(user_id, db_path=DB_PATH),
        "templates": [
            {"id": "base", "name": "Standard PPL", "desc": "6-day Push/Pull/Legs split"}
        ]
    }))

@app.get("/api/settings")
def api_settings(request: Request):
    user_id = _check_api_auth(request)
    
    prefs = get_user_preferences(user_id, DB_PATH)
    keys = get_user_api_keys(user_id, DB_PATH)
    providers = list(PROVIDERS.keys()) if 'ai_provider' in globals() else ["gemini", "claude", "openai", "deepseek"]
    return JSONResponse(jsonable_encoder({
        "prefs": prefs,
        "keys": [{"provider": k["provider"], "masked_key": k["masked_key"]} for k in keys],
        "providers": providers
    }))

@app.get("/api/chat-history")
def api_chat_history(request: Request):
    user_id = _check_api_auth(request)
    return JSONResponse(jsonable_encoder({
        "messages": get_chat_messages(db_path=DB_PATH, user_id=user_id)
    }))

@app.get("/{full_path:path}")
async def catch_all(full_path: str, request: Request):
    if full_path.startswith(("api/", "static/", "login/google", "auth", "google-health/")):
        raise HTTPException(status_code=404, detail="Not found")
    
    if full_path:
        file_path = FRONTEND_DIST / full_path
        if file_path.is_file():
            return FileResponse(file_path)
            
    return FileResponse(FRONTEND_DIST / "index.html")
