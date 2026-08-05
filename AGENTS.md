# AGENTS.md (The Engineering Constitution)

This is the authoritative rulebook for any human or AI agent (Claude, DeepSeek,
Gemini/Antigravity, OpenHands, GitHub Copilot, or otherwise) working on this
repository. Read this before touching code. Every rule below is enforced mechanically
where noted — do not weaken, bypass, or "temporarily" disable a gate to get a
task to complete. We use OpenHands for autonomous tasks, driven entirely by GitHub Issues.

## 1. Technology Stack Mandate

Do not substitute these without an explicit human decision recorded in a
commit message or task file:

- **Language:** Python 3.12+ (the deployed containers use `python:3.12-slim`;
  local dev may run newer interpreters — code must not depend on syntax/stdlib
  newer than 3.12).
- **Core agent runtime:** plain Python, no async framework, no heavyweight
  orchestration library. This project is deliberately "a script on a
  schedule," not a framework — keep it that way.
- **Database:** SQLite (`database.py`, stdlib `sqlite3`, WAL mode). Do not
  introduce an ORM or switch engines (e.g. to Postgres) without an explicit
  task that says so — see §2 for why this matters more than it used to.
- **Web app:** FastAPI + Jinja2 + server-rendered inline SVG for charts
  (`webapp/`). No client-side charting library, no SPA framework. Keep pages
  working with JavaScript disabled where reasonably possible (progressive
  enhancement, PWA-friendly).
- **AI providers:** must go through the `ai_provider.py` abstraction
  (`AIProvider` ABC + `get_provider()` factory). Never call a provider SDK
  directly from feature code — see §3.
- **Connectors:** Hevy (REST, personal API key), Google Health (OAuth2
  polling), Android Health Connect (local JSON file), Open-Meteo (keyless),
  Telegram Bot API. New connectors follow the `connector-integration` skill.
- **Lint/type/test tooling:** `ruff` (lint + format check), `mypy` (typing,
  advisory until stricter — see §5), `pytest`.

## 2. Multi-Tenancy Mandate (Critical)

This codebase currently has **partial** multi-user support: `users`,
`user_api_keys`, and `user_preferences` tables exist and are wired to a real
Google OAuth login (`webapp/app.py`), but every domain table
(`workout_history`, `programme_state`, `exercise_progress`, `body_metrics`,
`daily_log`, `check_ins`, `chat_messages`, `dashboard_insights`,
`deep_correlations`) has **no `user_id` column** — one shared programme,
history, and chat for every logged-in account. This is the single biggest
gap between "personal script" and "public product" and must be treated as
such:

- **Any new table you add MUST have a `user_id TEXT NOT NULL REFERENCES
  users(id)` column from day one**, even if the feature initially only runs
  for one user. Retrofitting is far more expensive than doing it up front.
- **Any function you touch that reads/writes an existing domain table MUST be
  migrated to accept and filter by `user_id`** as part of that change — do
  not add a new caller of an unscoped function; extend the function's
  signature instead. Partial migrations (some call sites scoped, some not)
  are worse than no migration because they hide the bug.
- Migrations must be additive and idempotent (`ALTER TABLE ... ADD COLUMN`
  guarded by a check against `PRAGMA table_info`, matching the existing
  pattern in `init_db()` for the `hrv` column). Never destructively rewrite a
  table in place without a backfill path for existing single-tenant data
  (treat existing rows as belonging to a synthesised "legacy" user).
- See the `multi-tenant-migration` skill before starting this work.

## 3. AI Provider Mandate ("Any AI")

Users must be able to bring their own key for **any** supported provider and
have it actually used — not just stored. `ai_provider.py` defines the
contract:

- `AIProvider.generate(prompt, *, stream=False)` and `.name()`, with a
  `PROVIDERS` registry and `get_provider(provider_name, api_key, model)`
  factory. Supported providers: `gemini`, `claude`, `openai`, `deepseek`
  (DeepSeek uses an OpenAI-compatible endpoint —
  `base_url="https://api.deepseek.com"` — so it can reuse the `openai` SDK
  client rather than needing a new dependency).
- **Every call site that generates AI text for a user-facing feature**
  (`gemini_engine.py`'s prompt functions, `insight_cron.py`, and
  `webapp/app.py`'s chat/RAG/XAI endpoints) **must resolve the provider via
  `database.get_user_preferences(user_id)` → `preferred_ai`/`ai_model` →
  `database.get_user_api_key(user_id, provider)` → `ai_provider.get_provider()`**,
  falling back to the server's own `GEMINI_API_KEY` only when the user has no
  key of their own configured. Hardcoding `import google.generativeai as
  genai` in new code is a constitution violation.
- Never log, echo, or persist a raw API key outside `user_api_keys` (which is
  Fernet-encrypted via `encryption.py`). Settings endpoints must return
  masked values (last 4 characters) after save, never the full key.
- New provider SDKs go in `requirements.txt` (not `requirements-web.txt`) if
  the core agent needs them too (e.g. daily plan generation), otherwise the
  narrowest requirements file that actually imports them.

## 4. Autonomous Execution Protocol

- **A failing verification gate must never reach `main`.** The agent (OpenHands) must run the full gate (`ruff check`, `mypy`
  advisory, `pytest`, an import-sanity check for `webapp.app` and `main`)
  before committing any task. If a task cannot
  pass verification, it is not done.
- **Before wiring to something outside the file you're editing** (a new pip
  dependency, a new route, a new DB column, a new env var) confirm it
  actually exists: is the package in `requirements*.txt` *and* installed in
  the venv? Is the table/column actually migrated in `init_db()`? Is the
  route registered on the right `app.py` (`main` vs `webapp/app.py` are
  different processes)? Assuming these are wired because the surrounding
  code implies they should be is exactly how half-finished features break
  production here.
- **Before starting any task, check for existing/overlapping work.** Read
  the GitHub Issues queue and skim recent `git log` for the area you're about
  to touch. If an issue describes something
  already partially implemented (e.g. `hevy_reader.py` /
  `programme_inference.py` already exist but are unwired, per §8), extend or
  wire up the existing implementation rather than writing a second one.
- **Never leave dead orphaned modules.** If you write a module intended to
  replace another (e.g. a data-driven programme inference replacing the
  static split), the task is not complete until the old path is either
  removed or the new path is actually called from `main.py`/`webapp/app.py`.
  An unwired module sitting in the repo is exactly as unfinished as no
  module at all — see §8 for two current examples of this trap.

## 5. Python Code Standards

- Type hints on all new function signatures (parameters and return type).
  `from __future__ import annotations` at the top of new modules so newer
  union syntax (`str | None`) stays valid on 3.12.
- `ruff check .` must be clean (zero warnings) for any file you touch, even
  if pre-existing warnings remain elsewhere in the repo — fix what you touch,
  don't expand the blast radius of unrelated files.
- Prefer stdlib + the packages already in `requirements*.txt` over adding new
  dependencies. If a new dependency is genuinely needed, add it with a
  version floor (`>=x.y`), not unpinned.
- No bare `except:` — catch specific exceptions. Network/connector code
  (Hevy, Google Health, Telegram) must not let a single external-API failure
  crash the whole run; catch, log, and degrade gracefully (this already
  matches the existing style in `hevy_client.py`/`telegram_notifier.py` —
  keep it).
- Never build SQL by string-concatenating user input — always use
  parameterised `?` placeholders (matches existing `database.py` style).
- Secrets (API keys, tokens, `WEB_AUTH_SECRET`, `ENCRYPTION_KEY`) are read
  from environment variables only, never hardcoded, never committed. Any new
  env var must be added to `.env.example` with a comment explaining it.

## 6. Public-Facing Product Principles

The end goal is a product where a user can, entirely from their own account:

1. Sign in (Google OAuth today; keep the door open for other providers).
2. Configure their **profile** (goals, experience level, constraints,
   coaching style) and **settings** (which AI provider/key/model to use,
   which connectors to link) — the Settings UI shell for this already
   exists; §3 and §2 are what make it actually take effect.
3. **Select or build their workout programme** from the app rather than
   inheriting a hardcoded split — this UI does not exist yet (only read-only
   rendering of the fixed split does); building it is a first-class product
   feature, not a stretch goal. `programme_inference.py`/`hevy_reader.py`
   (currently unwired, §8) are the natural foundation for an "infer from my
   Hevy history" option alongside manually-authored templates.
4. Have the agent **continuously track, adjust, and improve** that
   programme from connector data (Hevy sessions, body metrics, recovery)
   without further manual input, on their own schedule/timezone.

Every feature task should be evaluated against which of these four it moves
forward. Cosmetic/dashboard work is welcome but should not crowd out §2/§3/§4
— those are the actual gap between "personal script" and "public product."

## 7. Known Issues / Audit Findings (Last audited 2026-08-05)

- **No real data isolation between users** (§2). Logging in as a different
  Google account today shares the exact same programme/history/chat as
  everyone else. This is the top-priority backlog item.
- **`ai_provider.py`'s multi-provider abstraction is built but unwired.**
  `get_provider()` is never called anywhere outside `ai_provider.py` itself;
  every actual generation call in `gemini_engine.py`, `insight_cron.py`, and
  `webapp/app.py` hardcodes the Gemini SDK against one shared server key,
  regardless of what a user configures in Settings. `anthropic`/`openai`
  packages are also missing from `requirements*.txt`. No `DeepSeekProvider`
  exists yet.
- **Two orphaned modules**: `programme_inference.py` and `hevy_reader.py`
  implement a data-driven "infer the user's real training split from their
  Hevy history" path but are never imported by `main.py` or
  `webapp/app.py`. Tracked by GitHub Issue #37
  ("[TASK] Programme Inference: Wire hevy_reader.py into the app") —
  deliberate wiring belongs in a dedicated task, not a drive-by sweep.
- **No workout-programme selection UI.** `/plan` only renders the fixed
  split read-only. No route lets a user choose a template or build a custom
  one.
- **Two independent, hand-rolled scheduling loops in one container**
  (`docker-entrypoint.sh`'s bash sleep-loop and `insight_scheduler.py`'s
  Python sleep-loop), each single-timezone/single-recipient by construction.
  Needs consolidating into one scheduler that can support per-user run times
  once multi-tenancy lands.
- **Docs drift from code**: README.md claims the dashboard "has no login" —
  it has a working Google OAuth login (`webapp/app.py`). README's documented
  web port (8088/8080/8770 appear inconsistently across README and the two
  compose files) needs reconciling. `SWARM_RELAXED_TESTS`-style "aspirational
  comment" drift is exactly the kind of thing `task-daily-documentation-sync`
  (§9) exists to catch — don't let it recur here.
- **Zero test coverage** on the newest/most product-relevant modules:
  `ai_provider.py`, `gemini_engine.py`, `programme_inference.py`,
  `hevy_reader.py`, `insight_cron.py`, `insight_scheduler.py`, `main.py`,
  `encryption.py`, `sync_history.py`, `ai_widgets.py`, `config.py`,
  `weather.py`. Any task that touches these should add tests as part of the
  same change, not as a follow-up.
- **In-process-only rate limiting and OAuth state** in `webapp/app.py` — fine
  for a single replica, will silently break correctness (not just
  performance) the moment the web app runs as more than one instance behind
  a load balancer. Flag before deploying multi-replica.

## 8. Skills System

Domain-specific workflows for recurring work on this codebase are documented
under `.agents/skills/<name>/SKILL.md`. Consult (or extend) these before
re-deriving conventions from scratch:

- `multi-tenant-migration` — adding `user_id` scoping to a table and every
  function that touches it, per §2.
- `ai-provider-wiring` — resolving a user's preferred AI provider/key and
  calling it from a generation call site, per §3.
- `fastapi-route` — adding a new page or `/api/*` endpoint to `webapp/app.py`
  following the existing auth-middleware, session, and rate-limit patterns.
- `connector-integration` — adding a new external data source (wearables,
  nutrition apps, other workout trackers) following the existing
  Hevy/Google-Health/Health-Connect patterns.
- `programme-builder-ui` — building the workout programme selection/creation
  UI described in §6.3.
- `scheduler-job` — adding or consolidating a periodic job (replacing the
  dual sleep-loops described in §7).
- `secrets-and-encryption` — storing/reading anything through
  `encryption.py`/`user_api_keys` safely.
- `verification-gate` — the exact command sequence and pass criteria to run
  before considering any task complete. OpenHands must execute this checklist
  successfully before marking an issue complete or pushing code.

## 9. Continuous AI Development (OpenHands + GitHub Issues)

We use OpenHands to autonomously build and maintain this project, controlled entirely via GitHub Issues. 
Instead of local task files or a bespoke daemon, OpenHands reads issues directly from the GitHub repository, plans a solution, implements the code, runs the verification gate (linting and testing), and pushes the verified commits. 
Treat every GitHub issue as a direct instruction to the AI that will be worked on and shipped unattended. When you encounter a bug or need a feature, open a GitHub issue.
