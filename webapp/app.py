"""Internal progressive-overload web app (FastAPI).

A self-hosted dashboard for the workout agent. It reads the same SQLite database
the agent writes and renders it as a rich, read-only control centre: today's
session and progressive-overload targets, server-rendered SVG charts of every
lift and your body composition, all-time personal records, training-load trends,
a consistency calendar, the full periodisation plan, and programme check-ins.

There is no login: it is read-only and meant to sit behind a reverse proxy on a
trusted host (e.g. Apache -> Docker -> gym.example.com). All motivation is
automated; nothing here calls out to an API on a page view.

Run locally:   uvicorn webapp.app:app --reload
In a container: see Dockerfile.web / the `web` service in docker-compose.yml
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from authlib.integrations.starlette_client import OAuth

from database import (
    get_body_metrics,
    get_checkins,
    get_daily_logs,
    get_exercise_volumes,
    get_meta,
    get_personal_records,
    get_programme_start_date,
    get_progress_history,
    get_recent_bests,
    get_session_volumes,
    get_dashboard_insight,
    get_reasoning_log,
    save_reasoning_log,
    init_db,
    set_meta,
)
from google_health_auth import build_authorize_url, exchange_code
import google.generativeai as genai
from config import Config
import json
from fastapi import Query
from fastapi.responses import StreamingResponse
import analytics
import insights
import lifestyle
from webapp import charts
from hevy_parser import normalise_name
from program import (
    BLOCKS,
    BLOCK_WEEKS,
    COACHING_RULES,
    CYCLE_WEEKS,
    SPLIT_NAME,
    block_for_week,
    day_exercises,
    day_focus,
    today_day,
    week_in_cycle,
)

DB_PATH = os.environ.get("DATABASE_PATH", "workout_agent.db").strip()

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
ALLOWED_EMAILS = [e.strip() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()]

oauth = OAuth()
if WEB_GOOGLE_CLIENT_ID and WEB_GOOGLE_CLIENT_SECRET:
    oauth.register(
        name='google',
        client_id=WEB_GOOGLE_CLIENT_ID,
        client_secret=WEB_GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'}
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
async def _lifespan(_app: FastAPI):
    # Safe even if the agent already created the database (CREATE IF NOT EXISTS).
    init_db(DB_PATH)
    yield


app = FastAPI(title="Workout Agent", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")

if WEB_AUTH_SECRET:
    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
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

@app.get("/login")
async def login(request: Request):
    if not WEB_GOOGLE_CLIENT_ID:
        return HTMLResponse("Web auth is not configured.", status_code=500)
    if request.session.get("user"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "login.html")

@app.get("/login/google")
async def login_google(request: Request):
    if not WEB_GOOGLE_CLIENT_ID:
        return HTMLResponse("Web auth is not configured.", status_code=500)
    redirect_uri = str(request.url_for("auth"))
    if "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://")
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")

@app.get("/auth")
async def auth(request: Request):
    print("DEBUG AUTH: session =", dict(request.session))
    print("DEBUG AUTH: query_params =", dict(request.query_params))
    if not WEB_GOOGLE_CLIENT_ID:
        return RedirectResponse("/")
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        print("DEBUG AUTH ERROR:", type(e), str(e))
        raise e
    user = token.get("userinfo")
    if user:
        email = user.get("email", "")
        if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
            return HTMLResponse(f"Unauthorized email: {email}", status_code=403)
        request.session["user"] = email
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
    today = today or date.today()
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


def _find_lift_series(series: dict, *keywords: str) -> tuple[str | None, list]:
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


def _overload_nudge(planned_rep_range: str, best: dict | None) -> str:
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


def _format_best(best: dict | None) -> str:
    if not best:
        return "No data yet"
    weight = best.get("top_weight_kg")
    reps = best.get("top_reps")
    if weight is not None and reps is not None:
        return f"{weight:g} kg x {reps}"
    if reps is not None:
        return f"{reps} reps"
    return "No data yet"


def _training_levels() -> dict[str, int]:
    """Map ISO dates to a calendar-heatmap intensity (0-4)."""
    levels: dict[str, int] = {}
    for log in get_daily_logs(limit=400, db_path=DB_PATH):
        levels[log["date"]] = 2 if log["day"] is not None else 1
    # A session with logged sets is the strongest signal of a completed workout.
    for session in get_session_volumes(db_path=DB_PATH):
        levels[session["date"]] = 4
    return levels


def _current_streak(levels: dict[str, int]) -> int:
    """Count consecutive recent days that had any logged activity."""
    streak = 0
    day = date.today()
    # Allow today to be empty (the morning run may not have happened yet).
    if levels.get(day.isoformat(), 0) == 0:
        day -= timedelta(days=1)
    while levels.get(day.isoformat(), 0) > 0:
        streak += 1
        day -= timedelta(days=1)
    return streak


def _dashboard_context(today: date | None = None) -> dict:
    if today is None:
        today = date.today()
    start = get_programme_start_date(DB_PATH)
    week = week_in_cycle(start, today)
    block = block_for_week(week)
    bests = get_recent_bests(DB_PATH)
    bests_norm = {normalise_name(name): best for name, best in bests.items()}

    day = today_day(today)
    rows: list[dict] = []
    focus = "Rest & Recovery"
    if day is not None:
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
                }
            )

    metrics = get_body_metrics(db_path=DB_PATH)
    latest_weight = metrics[-1]["weight_kg"] if metrics else None
    recovery_like = {"weight_kg": latest_weight} if latest_weight else None
    guidance = lifestyle.daily_guidance(day, day is None, recovery_like)

    levels = _training_levels()
    streak = _current_streak(levels)
    week_in_block = ((week - 1) % BLOCK_WEEKS) + 1

    weight_spark = charts.sparkline([m["weight_kg"] for m in metrics if m["weight_kg"]])
    fat_spark = charts.sparkline(
        [m["body_fat_pct"] for m in metrics if m["body_fat_pct"]], colour=charts.WARN
    )
    latest_fat = next(
        (m["body_fat_pct"] for m in reversed(metrics) if m["body_fat_pct"]), None
    )

    review = insights.build_insights(
        get_progress_history(db_path=DB_PATH), metrics, None
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
            week / CYCLE_WEEKS * 100, label=f"Wk {week}", sub=f"of {CYCLE_WEEKS}"
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
        "dashboard_insight": get_dashboard_insight(db_path=DB_PATH)
    }


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", _dashboard_context())


@app.get("/progress")
def progress(request: Request):
    series = get_progress_history(db_path=DB_PATH)
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
        e1rms = [v for v in e1rms if v]
        best_e1rm = max(e1rms) if e1rms else None
        charts_data.append(
            {
                "name": name,
                "svg": charts.line_chart(points, unit="kg"),
                "best_e1rm": best_e1rm,
                "sessions": len(entries),
            }
        )
    return templates.TemplateResponse(
        request,
        "progress.html",
        {"active": "progress", "charts": charts_data, "body": _body_charts()},
    )


def _body_charts() -> dict:
    readings = get_body_metrics(db_path=DB_PATH)

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


@app.get("/stats")
def stats(request: Request):
    volumes = get_session_volumes(db_path=DB_PATH)
    prs = get_personal_records(db_path=DB_PATH)
    logs = get_daily_logs(limit=400, db_path=DB_PATH)
    start = get_programme_start_date(DB_PATH)
    series = get_progress_history(db_path=DB_PATH)
    today = date.today()
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
        [{"label": k, "value": v} for k, v in sorted(focus_counts.items())]
    )

    # Volume broken down by muscle group.
    groups = analytics.group_volumes(get_exercise_volumes(db_path=DB_PATH))
    muscle_donut = charts.donut(
        [{"label": g, "value": v} for g, v in sorted(groups.items(), key=lambda kv: -kv[1])]
    )

    # Session-load trend over the most recent sessions.
    recent = volumes[-14:]
    volume_bars = charts.bar_chart(
        [{"label": v["date"][5:], "value": v["volume"], "caption": v["date"]} for v in recent],
        unit="kg",
    )

    # Strength score (DOTS) and strength-to-bodyweight ratio over time, built
    # from the deadlift e1RM against the bodyweight recorded at the time.
    body = get_body_metrics(db_path=DB_PATH)
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
            dots_points.append({"date": e["date"][5:], "value": score, "label": f"{score:g}"})
        ratio_points.append(
            {"date": e["date"][5:], "value": round(e1rm / bw, 2), "label": f"{e1rm / bw:.2f}x"}
        )
    dots_chart = charts.line_chart(dots_points, colour=charts.PURPLE) if len(dots_points) > 1 else None
    ratio_chart = charts.line_chart(ratio_points, unit="x", colour=charts.ACCENT_2) if len(ratio_points) > 1 else None

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
        },
    )


def _project_lift(
    label: str, entries: list, target_ordinal: int, *, metric: str = "auto"
) -> dict | None:
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



@app.get("/plan")
def plan(request: Request):
    today = date.today()
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
                    {"name": ex.name, "scheme": f"{ex.sets} x {ex.rep_range}", "note": ex.note}
                    for ex in day_exercises(d, current_block)
                ],
            }
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


@app.get("/history")
def history(request: Request):
    logs = get_daily_logs(limit=60, db_path=DB_PATH)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "active": "history",
            "calendar": charts.calendar_heatmap(_training_levels()),
            "logs": logs,
        },
    )


@app.get("/checkins")
def checkins(request: Request):
    return templates.TemplateResponse(
        request,
        "checkins.html",
        {"active": "checkins", "checkins": get_checkins(db_path=DB_PATH)},
    )

@app.get("/api/xai_reasoning/{context_id}")
def xai_reasoning(context_id: str):
    # context_id is expected to be {date}_{exercise_name}
    existing = get_reasoning_log(context_id, db_path=DB_PATH)
    if existing:
        return {"reasoning": existing}
    
    parts = context_id.split("_", 1)
    if len(parts) != 2:
        return {"reasoning": "Invalid context ID"}
    
    when, ex_name = parts
    
    config = Config.load()
    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(config.gemini_model)
    
    history = get_progress_history(db_path=DB_PATH).get(ex_name, [])
    
    prompt = f"Why did my volume/performance change for {ex_name} around {when}? Here is my history: {json.dumps(history)}. Provide a clear causal explanation in a few sentences."
    response = model.generate_content(prompt)
    reasoning = (response.text or "Could not determine reasoning.").strip()
    
    save_reasoning_log(context_id, ex_name, reasoning, db_path=DB_PATH)
    return {"reasoning": reasoning}

@app.get("/api/project_peak")
def project_peak():
    config = Config.load()
    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(config.gemini_model)
    
    series = get_progress_history(db_path=DB_PATH)
    dl_entries = series.get("Deadlift", [])
    pu_entries = series.get("Pull-ups", [])
    
    prompt = f"Analyze this historical progression for Deadlift: {json.dumps(dl_entries)} and Pull-ups: {json.dumps(pu_entries)}. Project the estimated 1RM at the end of the 12-week peaking phase. Adjust the forecast curve if recent sessions look 'bad'. Return JSON: {{'Deadlift_Projected': float, 'Pullups_Projected': float, 'Validation': 'string explanation'}}"
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip())
    except:
        return {"error": "Failed to project peak."}

@app.get("/api/rag_search")
def rag_search(q: str = Query(...)):
    config = Config.load()
    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(config.gemini_model)
    
    logs = get_daily_logs(limit=30, db_path=DB_PATH)
    history = get_progress_history(db_path=DB_PATH)
    
    context = json.dumps({"logs": logs, "history": history})
    
    prompt = f"You are a Log Investigator. Based on the following SQLite log context, answer the user's query: '{q}'. Reference specific dates or sessions. Context: {context[:30000]}"
    
    def generate():
        response = model.generate_content(prompt, stream=True)
        for chunk in response:
            if chunk.text:
                yield chunk.text

    return StreamingResponse(generate(), media_type="text/plain")


def _gh_redirect_uri(request: Request) -> str:
    """The OAuth redirect URI, explicit env override or derived from the request."""
    return GH_REDIRECT_URI or str(request.url_for("google_health_callback"))


@app.get("/settings")
def settings(request: Request):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "gh_configured": bool(GH_CLIENT_ID and GH_CLIENT_SECRET),
            "gh_connected": bool(get_meta(_GH_TOKEN_KEY, DB_PATH)),
            "gh_status": request.query_params.get("gh"),
        },
    )


@app.get("/google-health/connect")
def google_health_connect(request: Request):
    """Start the Google Health OAuth flow and redirect to the consent screen."""
    if not (GH_CLIENT_ID and GH_CLIENT_SECRET):
        return RedirectResponse("/settings?gh=unconfigured", status_code=303)
    state = secrets.token_urlsafe(16)
    set_meta(_GH_STATE_KEY, state, DB_PATH)
    url = build_authorize_url(
        GH_CLIENT_ID, state, redirect_uri=_gh_redirect_uri(request)
    )
    return RedirectResponse(url, status_code=303)


@app.get("/google-health/callback", name="google_health_callback")
def google_health_callback(request: Request):
    """Receive Google's redirect, swap the code for a refresh token, store it."""
    if not (GH_CLIENT_ID and GH_CLIENT_SECRET):
        return RedirectResponse("/settings?gh=unconfigured", status_code=303)
    if request.query_params.get("error"):
        return RedirectResponse("/settings?gh=denied", status_code=303)
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    expected = get_meta(_GH_STATE_KEY, DB_PATH)
    set_meta(_GH_STATE_KEY, "", DB_PATH)  # one-time use, regardless of outcome
    if not code or not state or not expected or state != expected:
        return RedirectResponse("/settings?gh=error", status_code=303)
    tokens = exchange_code(
        GH_CLIENT_ID, GH_CLIENT_SECRET, code, redirect_uri=_gh_redirect_uri(request)
    )
    if not tokens or not tokens.get("refresh_token"):
        return RedirectResponse("/settings?gh=error", status_code=303)
    set_meta(_GH_TOKEN_KEY, tokens["refresh_token"], DB_PATH)
    return RedirectResponse("/settings?gh=connected", status_code=303)


@app.post("/google-health/disconnect")
def google_health_disconnect():
    """Forget the stored refresh token so the agent stops syncing."""
    set_meta(_GH_TOKEN_KEY, "", DB_PATH)
    return RedirectResponse("/settings?gh=disconnected", status_code=303)


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Serve the service worker from the root so it can control every page.

    A worker only controls URLs within its own path, so it must be served from
    "/" (with the Service-Worker-Allowed header) rather than from /static.
    """
    return FileResponse(
        _BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )
