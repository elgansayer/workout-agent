---
name: fastapi-route
description: 'Add a new page or /api/* endpoint to webapp/app.py following the existing auth-middleware, session, rate-limit, and Jinja2 template conventions. Use when adding any new webapp route, page, or JSON API.'
---

# FastAPI Route

## When to Use

- Adding a new dashboard page (template-rendered).
- Adding a new `/api/*` JSON endpoint (settings mutation, AJAX data, SSE
  streaming).
- Adding a new OAuth-style connector flow (modelled on `/google-health/*`).

## Reference Layout

Everything lives in the single `webapp/app.py` (1000+ lines) — this project
does not split routers into separate files. Templates live in
`webapp/templates/*.html` (Jinja2, extending `base.html`). Server-rendered
charts come from `webapp/charts.py` (inline SVG, no JS chart library). AI
widget formatting comes from `webapp/ai_widgets.py`.

## Procedure

1. **Auth is enforced by middleware, not per-route decorators.** The
   `BaseHTTPMiddleware` in `webapp/app.py` already redirects unauthenticated
   page requests to `/login` and returns 401 JSON for `/api/*` paths, for
   every route except an explicit allow-list (static/auth/health-callback).
   A new route is protected automatically — do not add your own duplicate
   auth check unless the route genuinely needs to be public (in which case,
   add it to the middleware's allow-list explicitly and justify why in a
   comment).

2. **Get the current user from the session**, not a global: the session
   (signed cookie via `itsdangerous`/Starlette `SessionMiddleware`) carries
   `user_id`/`email` set at login (`get_or_create_user()`). Pull it the same
   way existing routes do — grep `request.session` usage in `webapp/app.py`
   for the pattern — rather than inventing a second lookup mechanism.

3. **Page routes** return `templates.TemplateResponse(request, "name.html",
   {...context})`. New templates extend `base.html` and follow the existing
   card/section markup in `dashboard.html`/`settings.html` — don't introduce
   a new CSS framework; extend `webapp/static/style.css`.

4. **API routes** (`/api/...`) return JSON via FastAPI's normal return-value
   serialization or `JSONResponse` for custom status codes. Validate request
   bodies with Pydantic models or explicit manual checks matching the
   existing `/api/settings/*` handlers — don't accept an untyped dict and
   trust its shape.

5. **Rate limiting**: any endpoint that calls out to an AI provider or an
   external API (matching `/api/xai_reasoning`, `/api/project_peak`,
   `/api/rag_search`, `/api/settings/verify-hevy`) must go through the
   existing `_check_rate_limit()` per-IP helper. Note (per `AGENTS.md` §7)
   this is in-process-only — fine today, but don't build new features that
   assume it's distributed/durable.

6. **Streaming endpoints** — model on `/api/rag_search`: use
   `StreamingResponse` with a generator that yields text chunks from an
   `AIProvider.generate(..., stream=True)` call (see `ai-provider-wiring`
   skill).

7. **Tests** — add to `tests/test_webapp.py` using the existing
   `TestClient`/fixture pattern: happy path, unauthenticated request gets
   redirected/401'd, and at least one validation/error path.

## Verification

Run the `verification-gate` skill's steps. Manually load the route in a
browser via `uvicorn webapp.app:app --reload` for anything template-rendered
— a route that returns 200 in a test can still render visibly broken HTML if
the template references a context key that doesn't exist (Jinja2 silently
renders `Undefined` as empty string by default).

## Gotchas

- `webapp/app.py` and `main.py` are **separate processes** (web dashboard vs.
  scheduled agent, see `docker-compose.yml`) — a route handler cannot assume
  anything `main.py` computed in-memory is available; it must read from the
  database.
- Don't add a new top-level dependency to `requirements.txt` for a
  webapp-only feature — use `requirements-web.txt` so the (much smaller)
  `agent` container image doesn't grow.
