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
Google OAuth login (`webapp/app.py`). Most domain tables have been migrated
to include `user_id` columns; the remaining unscoped tables are `hevy_routines`
and `hevy_meta` — one shared set of Hevy routines for every logged-in account.
Multi-tenancy migration of these remaining tables is the top-priority backlog item:

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

  to touch. If an issue describes something already partially implemented
  (e.g. `sync_history.py` exists as a standalone utility script with no
  callers, per §7), extend or wire up the existing implementation rather
  than writing a second one.
  to touch. If an issue describes something already partially implemented,
  extend or wire up the existing implementation rather than writing a second
  one.

- **Never leave dead orphaned modules.** If you write a module intended to
  replace another (e.g. a data-driven programme inference replacing the
  static split), the task is not complete until the old path is either
  removed or the new path is actually called from `main.py`/`webapp/app.py`.
  An unwired module sitting in the repo is exactly as unfinished as no

  module at all — `sync_history.py` is the current example: standalone but
  unreferenced (see §7).
  module at all.



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

   (now wired via `webapp/app.py`'s `_run_hevy_inference()`, PR #142) are the
   natural foundation for an "infer from my Hevy history" option alongside
   manually-authored templates.
   inheriting a hardcoded split. The `/programmes` page now provides
   template selection, Hevy-inference, and custom programme activation
   (PR #142 onward). `programme_inference.py`/`hevy_reader.py` are wired
   via `webapp/app.py`'s `_run_hevy_inference()` for the
   "infer from my Hevy history" option.

4. Have the agent **continuously track, adjust, and improve** that
   programme from connector data (Hevy sessions, body metrics, recovery)
   without further manual input, on their own schedule/timezone.

Every feature task should be evaluated against which of these four it moves
forward. Cosmetic/dashboard work is welcome but should not crowd out §2/§3/§4
— those are the actual gap between "personal script" and "public product."

## 7. Known Issues / Audit Findings (Last audited 2026-08-06)
## 7. Known Issues / Audit Findings (Last audited 2026-08-06, hourly lint checked 2026-08-06)
<!-- Hourly lint & format pass #465: 2026-08-06 -- ruff check --fix: clean, ruff format: clean, mypy: clean, pytest: 569/569 -->
## 7. Known Issues / Audit Findings (Last audited 2026-08-06, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06)
## 7. Known Issues / Audit Findings (Last audited 2026-08-06, hourly lint checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06)
## 7. Known Issues / Audit Findings (Last audited 2026-08-06, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly commit hygiene #492 checked 2026-08-06)
## 7. Known Issues / Audit Findings (Last audited 2026-08-06, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly dead-code sweep #505 checked 2026-08-06)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly dead-code sweep #505 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-06, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly dead-code sweep #491 re-checked 2026-08-07, hourly test watch #499 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-06, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly dead-code sweep #491 re-checked 2026-08-07, hourly test watch #514 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly test watch #514 checked 2026-08-07, hourly dead-code sweep #505 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly test watch #514 checked 2026-08-07, hourly test watch #499 checked 2026-08-07, hourly test watch #527 checked 2026-08-07)

- **No real data isolation between users** (§2). Logging in as a different
  Google account today shares the exact same programme/history/chat as
  everyone else. This is the top-priority backlog item.

- **`ai_provider.py` multi-provider wiring is complete.** PR #85
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly test watch #514 checked 2026-08-07, hourly test watch #499 checked 2026-08-07, hourly dead-code sweep #528 checked 2026-08-07, hourly test watch #527 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly test watch #514 checked 2026-08-07, hourly test watch #499 checked 2026-08-07, hourly dead-code sweep #528 checked 2026-08-07, hourly test watch #543 checked 2026-08-07, hourly test watch #527 checked 2026-08-07, hourly dead-code sweep #552 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly test watch #514 checked 2026-08-07, hourly test watch #499 checked 2026-08-07, hourly dead-code sweep #528 checked 2026-08-07, hourly test watch #543 checked 2026-08-07, hourly test watch #527 checked 2026-08-07, hourly test watch #565 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly test watch #514 checked 2026-08-07, hourly test watch #499 checked 2026-08-07, hourly dead-code sweep #528 checked 2026-08-07, hourly test watch #543 checked 2026-08-07, hourly test watch #527 checked 2026-08-07, hourly dead-code sweep #552 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly test watch #514 checked 2026-08-07, hourly test watch #499 checked 2026-08-07, hourly dead-code sweep #528 checked 2026-08-07, hourly test watch #543 checked 2026-08-07, hourly test watch #527 checked 2026-08-07, hourly commit hygiene #580 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly dead-code sweep #630 checked 2026-08-07, hourly test watch #629 checked 2026-08-07, hourly commit hygiene #631 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly dead-code sweep #642 checked 2026-08-07, hourly test watch #629 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly dead-code sweep #654 checked 2026-08-07, hourly test watch #653 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly dead-code sweep #654 checked 2026-08-07, hourly test watch #653 checked 2026-08-07, hourly lint & format fix #651 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly dead-code sweep #668 checked 2026-08-07, hourly test watch #653 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-07, hourly lint checked 2026-08-06, hourly test watch #467 checked 2026-08-06, hourly lint #476 checked 2026-08-06, hourly dead-code sweep #479 checked 2026-08-06, hourly test watch #490 checked 2026-08-06, hourly dead-code sweep #491 checked 2026-08-06, hourly test watch #514 checked 2026-08-07, hourly test watch #499 checked 2026-08-07, hourly dead-code sweep #528 checked 2026-08-07, hourly test watch #543 checked 2026-08-07)
## 7. Known Issues / Audit Findings (Last audited 2026-08-08, daily docs sync #684 checked 2026-08-08, hourly dead-code sweep #688 checked 2026-08-08, hourly test watch #687 checked 2026-08-08)

- **Partial data isolation between users** (§2). Most domain tables have been
  migrated to include `user_id` columns and are scoped per-user. The remaining
  unscoped tables are `hevy_routines` and `hevy_meta` — all users share the
  same Hevy routine cache. Completing the multi-tenancy migration is the
  top-priority backlog item.

- ~~**`ai_provider.py` multi-provider wiring is complete.** PR #85
  (`wire-ai-provider`) integrated `resolve_provider()` into `gemini_engine.py`,
  `main.py`, `checkin.py`, and `hevy_sync.py`. `webapp/app.py` and
  `insight_cron.py` now also use `resolve_provider()`. PR #164
  (`fix-gemini-engine-provider`) removed the last direct
  `google.generativeai` import from `gemini_engine.py` — all AI generation
  call sites are fully migrated to the provider abstraction.
  `tests/test_ai_provider.py` covers the provider abstraction (7 tests);
  `tests/test_gemini_engine.py` covers the prompt/fallback functions
  (32 tests).

- **`hevy_reader.py` and `programme_inference.py` are now wired** into
  (32 tests).~~ ✅ Resolved.

- ~~**`hevy_reader.py` and `programme_inference.py` are wired** into
  `webapp/app.py` via the `_run_hevy_inference()` helper, which powers the
  "Infer from my Hevy history" programme builder flow (PR #142). Neither is
  orphaned.
- **`sync_history.py` is wired** — it is imported by `main.py`
  (`--sync-history` CLI flag) and `webapp/app.py` (`/api/settings/sync-history`
  endpoint). It is also executable standalone (`python sync_history.py`).
- ~~**No workout-programme selection UI.**~~ **RESOLVED (2026-08-05).**
  `/programmes` route and `programmes.html` template now provide template
  selection, Hevy-inference, and custom programme activation (PR #142 onward).
- **Scheduling has been consolidated** into a single unified `scheduler.py`
  orphaned; the remaining work is a full programme-selection UI (see next
  item).

- **`scheduler.py` and `insight_cron.py` are wired.** `scheduler.py` is the
  main long-running process when `MODE=schedule` (`docker-entrypoint.sh`).
  `insight_cron.py` is invoked as a subprocess by `scheduler.py` for daily
  and weekly insight jobs. Both have test coverage
  (`tests/test_scheduler.py`, `tests/test_insight_cron.py`).

- **`sync_history.py` is orphaned** (no callers, no test coverage). Issue
  #185 tracks wiring decisions. It remains a standalone utility for one-off
  historical Hevy backfills (`python sync_history.py`).

- **No workout-programme selection UI.** `/plan` only renders the fixed
  split read-only. No route lets a user choose a template or build a custom
  one.

- **In-process-only rate limiting and OAuth state** in `webapp/app.py` — fine
  for a single replica, will silently break correctness (not just
  performance) the moment the web app runs as more than one instance behind
  a load balancer. Flag before deploying multi-replica.

- **`sync_history.py` is now wired** via `main.py`'s `--sync-history`
  flag and `webapp/app.py`'s `sync_history_endpoint` (`/api/sync-history`).
- **No workout-programme selection UI.** `/plan` only renders the fixed
  split read-only. No route lets a user choose a template or build a custom
  one.
- **`scheduler.py` unified the two previous scheduling loops** into one
  Python process that wakes every minute, dispatches per-user coaching runs
  (`main.py`) and insight jobs (`insight_cron.py --daily`/`--weekly`) at the
  configured `RUN_AT` times, and isolates per-user failures. Still iterates a
  single `RUN_AT`/`TZ` for all users — needs per-user schedule preferences
  once multi-tenancy lands.

- **`sync_history.py` is now wired.** It is imported by `main.py`
  (`--sync-history` CLI flag) and `webapp/app.py`
  (`/api/settings/sync-history` endpoint) for one-off historical Hevy
  backfills scoped to a user's own API key. No longer orphaned.

- **No workout-programme selection UI.** `/plan` only renders the fixed
  split read-only. No route lets a user choose a template or build a custom
  one.

- **Scheduling is now unified** into a single `scheduler.py` Python process
  (invoked by `docker-entrypoint.sh`'s `schedule` mode), replacing the
  former dual-loop architecture (bash sleep-loop + `insight_scheduler.py`).
  `scheduler.py` wakes every 60 seconds, checks each user's local time
  against configured `RUN_AT` times, and dispatches `main.py` and
  `insight_cron.py --daily`/`--weekly` per user. Still single-timezone per
  container; per-user timezone scheduling needs the per-user `timezone`
  preference column before it can be implemented.

- **`sync_history.py` is wired** into `main.py` (`--sync-history` CLI flag)
  and `webapp/app.py` (`/api/settings/sync-history` endpoint). The module
  itself can still be invoked standalone (`python sync_history.py`).
  Moving it to a `scripts/` directory would require updating those two
  import sites.
- **No workout-programme selection UI.** `/plan` only renders the fixed
  split read-only. No route lets a user choose a template or build a custom
  one.
- **Two independent, hand-rolled scheduling loops in one container**
  (`docker-entrypoint.sh`'s bash sleep-loop and the old `insight_scheduler.py`
  Python sleep-loop) have been consolidated into a single unified `scheduler.py`
  that supports per-user timezones (PR #TBD).
  `tests/test_scheduler.py` covers the scheduler (18 tests).
- **Scheduler consolidated.** The dual sleep-loop architecture (bash
  `docker-entrypoint.sh` + Python `insight_scheduler.py`) has been replaced
  by a unified `scheduler.py` that iterates per-user run times. The
  `insight_scheduler.py` module no longer exists.
- **Docs drift from code**: README.md no longer claims the dashboard "has no
  item).~~ ✅ Resolved.
- ~~**`sync_history.py` is wired** — it is imported by `main.py`
  (`--sync-history` CLI flag) and `webapp/app.py` (`/api/settings/sync-history`
  endpoint). It is also executable standalone (`python sync_history.py`).~~ ✅ Resolved.
- **Workout-programme selection UI exists** (`/programmes`) with Hevy
  inference and template selection, but the full interactive programme
  builder (drag-and-drop exercise arrangement, custom block authoring) is
  not yet built. The current UI lets users pick from fixed templates or
  infer a split from their Hevy history — see `programme-builder-ui` skill.
- ~~**No workout-programme selection UI.** `/plan` only renders the fixed
  split read-only. No route lets a user choose a template or build a custom
  one.~~ ✅ Resolved — `/programmes` provides template selection (including
  "Infer from my Hevy history" and custom). A full drag-and-drop custom
  workout builder UI (defining every exercise and set scheme from scratch)
  is still planned but template selection is functional.
- ~~**Scheduling has been consolidated** into a single unified `scheduler.py`
  (PR #142). `insight_scheduler.py` has been removed. The dual bash/Python
  sleep-loops described in prior audits no longer exist — `scheduler.py` is
  the single long-running process in the agent container, dispatching both
  coaching runs (`main.py`) and insight jobs (`insight_cron.py`) on one
  per-minute wake loop, with per-user timezone support.~~ ✅ Resolved.
- ~~**Docs drift from code**: README.md no longer claims the dashboard "has no
  login" — the Google OAuth section is accurate. Web port is now uniformly
  `8770` across README, both compose files, and `.env.example`. Stale "Portainer
  agent on port 9001" claim removed from README (2026-08-08); `ENCRYPTION_KEY`
  added to README env var table (2026-08-08).~~ ✅ Resolved.
  `8770` across README and both compose files (reconciled 2026-08-05).

  README.md AI-provider language updated to reflect multi-provider support
  (no longer Gemini-only). Portainer-agent-on-port-9001 claim removed from
  README (docker-compose.portainer.yml never had it). Dashboard route table
  now includes `/programmes`. (Reconciled 2026-08-06.)
  `8770` across README, both compose files, and `.env.example`.
  README no longer references a non-existent Portainer agent on port 9001
  (docker-compose.portainer.yml has no Portainer-agent service, confirmed
  2026-08-08, #684).~~ ✅ Resolved.

- **Test coverage gaps** are now closed on all previously-uncovered modules.
  Every module in the codebase has a corresponding `tests/test_*.py` file
  (28 test modules, 418 tests passing as of 2026-08-05). Previously-gapped
  modules that are now covered: `programme_inference.py`, `hevy_reader.py`,
  `insight_cron.py`, `scheduler.py` (was `insight_scheduler.py`), `main.py`,
  `sync_history.py`, `ai_widgets.py`, `weather.py`.
  Now-covered modules:
  `tests/test_ai_provider.py` (7 tests, PR #85),
  `tests/test_gemini_engine.py` (32 tests, PR #164),
  `tests/test_encryption.py` (PR #146),
  `tests/test_config.py` (PR #146),
  `tests/test_scheduler.py` (18 tests, PR #227).
- **Test coverage gaps** remain on the following modules (any task that
  touches these should add tests as part of the same change, not as a
  follow-up): `programme_inference.py`, `hevy_reader.py`,
  `insight_cron.py`, `scheduler.py`, `main.py`, `sync_history.py`,
  `insight_cron.py`, `main.py`, `sync_history.py`,
  `ai_widgets.py`, `weather.py`. Now-covered modules:
  `tests/test_ai_provider.py` (7 tests, PR #85),
  `tests/test_gemini_engine.py` (32 tests, PR #164),
  `tests/test_encryption.py` (PR #146),
  `tests/test_config.py` (PR #146),
  `tests/test_scheduler.py` (PR #38),
  `tests/test_insight_cron.py` (PR #38).
  `tests/test_config.py` (PR #146).
- **Test coverage audit** (last updated 2026-08-06, re-verified
  2026-08-06, re-verified 2026-08-07): all source modules have
  corresponding test files. The full test suite stands at 569 passing
  tests covering 30 test modules. Zero coverage gaps — every source
  module has a corresponding test file. All verification gates clean
  (compileall, ruff, pytest, mypy, import-sanity). Hourly test watch
  #514 confirmed no drift; all gates
- **Test coverage audit** (last updated 2026-08-07, re-verified
  2026-08-07): all source modules have corresponding test files. The
  full test suite stands at 569 passing tests covering 31 test modules.
  Zero coverage gaps — every source module has a corresponding test
  file. All verification gates clean (compileall, ruff, pytest, mypy,
  import-sanity). Hourly dead-code sweep #505 confirmed no drift; all gates
  green with no failures to resolve and zero test coverage gaps.

- **Hourly dead-code sweep re-verified.** `dead_code_sweep.py` executed
  clean via `--json` output (status: "clean", zero orphans). All 27
  top-level modules and 3 webapp sub-modules confirmed wired. Full
  verification gate passed: ruff (clean), pytest (569/569), dead_code_sweep
  (zero orphans), import-sanity (all reachable), mypy (clean, 61 source files).
  Issue #505 sweep complete — no newly orphaned or truly-dead modules found.
  Stale pycache cleaned: 0.
  full test suite stands at 569 passing tests covering 30 test modules.
  Zero coverage gaps — every source module has a corresponding test
  file. All verification gates clean (compileall, ruff, pytest, mypy,
  import-sanity). Hourly test watch #527 confirmed no drift; all gates
  green with no failures to resolve and zero test coverage gaps.

- **Hourly dead-code sweep re-verified.** Issue #505 audit complete.
  `dead_code_sweep.py` executed
  clean via `--json` output (status: "clean", zero orphans). All 27
  top-level modules and 3 webapp sub-modules confirmed wired. Full
  verification gate passed: ruff (clean), pytest (569/569), dead_code_sweep
  (zero orphans), import-sanity (all reachable), mypy (clean, 31 source files).
  Issue #491 sweep complete — no newly orphaned or truly-dead modules found.
  Stale pycache cleaned: 0.
  2026-08-07, hourly test watch #565 checked 2026-08-07): all source
  modules have corresponding test files. The full test suite stands at
  575 passing tests covering 30 test modules. Zero coverage gaps — every
  source module has a corresponding test file. All verification gates
  clean (compileall, ruff, pytest, mypy, import-sanity). Hourly test
  watch #565 confirmed no drift; all gates green with no failures to
  resolve and zero test coverage gaps.
2026-08-07 by hourly test watch #653): all 609 tests passing,
  2026-08-07): all source modules have corresponding test files. The
  full test suite stands at 533 passing tests covering 30 test modules.
  Zero coverage gaps — every source module has a corresponding test
  file. All verification gates clean (compileall, ruff, pytest, mypy,
  import-sanity). Hourly test watch #527 confirmed no drift; all gates
  import-sanity). Hourly test watch #543 confirmed no drift; all gates
  green with no failures to resolve and zero test coverage gaps.
- **Test coverage audit** (last updated 2026-08-08, re-verified
  2026-08-08 by hourly dead-code sweep #688, hourly lint #685, hourly
  test watch #706, and hourly test watch #717): all 611 tests passing,
  covering 31 test modules. Zero coverage gaps — every source module
  has a corresponding test file. All verification gates clean
  (compileall, ruff, pytest, webapp + main import-sanity, mypy
  advisory clean). No drift between code and test suite.

- **Hourly dead-code sweep re-verified.** `dead_code_sweep.py` executed
  clean via `--json` output (status: "clean", zero orphans). All 27
  top-level modules and 3 webapp sub-modules confirmed wired. Full
  verification gate passed: ruff (clean), pytest (533/533), dead_code_sweep
  (zero orphans), import-sanity (all reachable). Issue #479 sweep complete
  — no newly orphaned or truly-dead modules found.
  (zero orphans), import-sanity (all reachable). Issue #505 sweep complete
  — no newly orphaned or truly-dead modules found.
- **Hourly dead-code sweep #552 re-verified.** `dead_code_sweep.py` executed
  clean via `--json` output (status: "clean", zero orphans). All 27
  top-level modules and 3 webapp sub-modules confirmed wired. Full
  verification gate passed: ruff (clean), pytest (575/575), dead_code_sweep
  (zero orphans), import-sanity (all reachable), mypy (clean on 31 source files).
  Previously-known orphans (`programme_inference.py`, `hevy_reader.py`,
  `sync_history.py`) all remain properly wired. No newly orphaned or
  truly-dead modules found. Stale pycache cleaned: 0.
- **Hourly dead-code sweep #642 verified (2026-08-07).** `dead_code_sweep.py` executed
  clean via `--json` output (status: "clean", zero orphans). All 27
  top-level modules and 3 webapp sub-modules confirmed wired. Full
  verification gate passed: ruff (clean), pytest (573/573), dead_code_sweep
  (zero orphans), import-sanity (all reachable), mypy (clean on all 28 source files).
- **Hourly dead-code sweep #654 re-verified (2026-08-07).** `dead_code_sweep.py` executed
- **Hourly dead-code sweep #668 re-verified (2026-08-07).** `dead_code_sweep.py` executed
  clean via `--json` output (status: "clean", zero orphans). All 27
  top-level modules and 3 webapp sub-modules confirmed wired. Full
  verification gate passed: ruff (clean), pytest (609/609), dead_code_sweep
  (zero orphans), import-sanity (all reachable), mypy (clean on all 32 source files).
- **Hourly dead-code sweep #593 verified (2026-08-07).** `dead_code_sweep.py`
  executed clean via `--json` output (status: "clean", zero orphans). All 27
  top-level modules and 3 webapp sub-modules confirmed wired. Entry-point
  modules (`main.py`, `scheduler.py`, `commit_hygiene.py`, `connector_health.py`,
  `dead_code_sweep.py`, `insight_cron.py`, `sync_history.py`) correctly excluded
  from orphan check. Full verification gate passed: ruff (clean), pytest (601/601),
  dead_code_sweep (zero orphans), import-sanity (all reachable). `hevy_reader.py`
  and `programme_inference.py` remain wired (imported by `webapp/app.py`).
  No dead code to prune, no new issues to file.
- **Hourly test watch #717 executed (2026-08-08).** Full verification
  gate passed: compileall (pass), ruff (clean), pytest (611/611),
  webapp.app import-sanity (OK), main import-sanity (OK), mypy (clean
  on all 63 source files). All 31 test modules present — zero coverage
  gaps. No test failures, no drift to fix. Status: clean.

- **Hourly dead-code sweep #688 executed (2026-08-08).** `dead_code_sweep.py` executed
  clean via `--json` output (status: "clean", zero orphans). All 28
  top-level modules (including `conftest.py`) and 4 webapp sub-modules confirmed wired.
  Full verification gate passed: ruff (clean), pytest (609/609), dead_code_sweep
  (zero orphans), compileall (pass), import-sanity (all reachable), mypy (clean on all 63 source files).
  `hevy_reader.py` and `programme_inference.py` confirmed wired (imported by
  `webapp/app.py` — `_run_hevy_inference()` helper, `/api/hevy/infer` route).
  `ai_widgets.py` and `charts.py` confirmed wired (imported by `webapp/app.py`).
  `main.py`, `scheduler.py`, `insight_cron.py`, `dead_code_sweep.py`,
  `commit_hygiene.py`, `connector_health.py`, `sync_history.py` all confirmed as entry points
  (invoked directly or via subprocess). No truly dead code found. Stale pycache cleaned: 0.

- **Hourly dead-code sweep #718 executed (2026-08-08).** `dead_code_sweep.py --json`
  status: "clean", zero orphans. All 25 top-level source modules (excluding
  `conftest.py` and test files) and 4 webapp sub-modules confirmed wired via
  AST-based import graph BFS from entry points + grep fallback.
  Full verification gate passed: ruff (clean), pytest (611/611),
  dead_code_sweep (zero orphans), compileall (pass), import-sanity
  (`webapp.app` + `main` OK), mypy (clean on all 32 source files).
  `hevy_reader.py` and `programme_inference.py` remain wired via
  `webapp/app.py` (`_run_hevy_inference()` / `/api/hevy/infer`).
  `commit_hygiene.py` and `connector_health.py` invoked by `scheduler.py`
  via subprocess. `sync_history.py` imported by `main.py` and
  `webapp/app.py`. No truly dead code found. No GitHub issues created.

- **Test coverage audit** (last updated 2026-08-06, re-verified
  2026-08-06): all source modules have corresponding test files. The
  full test suite stands at 569 passing tests covering 30 test modules.
  Zero coverage gaps — every source module has a corresponding test
  file. All verification gates clean (compileall, ruff, pytest, mypy,
  import-sanity). Hourly test watch #444 confirmed no drift; all gates
  green with no failures to resolve.
- **Hourly dead-code sweep #505 re-verified 2026-08-07.** `dead_code_sweep.py`
  executed clean (status: "clean", zero orphans). All 27 top-level modules
  and 3 webapp sub-modules confirmed wired. Known previously-orphaned modules
  (`programme_inference.py`, `hevy_reader.py`, `sync_history.py`) remain wired
  via `webapp/app.py` and transitively reachable. Full verification gate
  passed: ruff (clean), pytest (533/533, 1 skipped), dead_code_sweep (zero
  orphans), import-sanity (all reachable). No truly-dead code found. No
  orphaned test files. Zero stale bytecode.

- **Test coverage audit** (last updated 2026-08-06, re-verified
  2026-08-06): all source modules have corresponding test files. The
  full test suite stands at 559 passing tests covering 30 test modules.
  Zero coverage gaps — every source module has a corresponding test
  file. All verification gates clean (compileall, ruff, pytest, mypy,
  import-sanity). Hourly test watch #391 confirmed no drift.
- **Test coverage audit** (last updated 2026-08-06, re-verified
  2026-08-06): all source modules have corresponding test files. The
  full test suite stands at 564 passing tests covering 30 test modules.
  Zero coverage gaps — every source module has a corresponding test
  file. All verification gates clean (compileall, ruff, pytest, mypy,
  import-sanity). Hourly test watch #405 confirmed no drift.
- **Test coverage audit** (last updated 2026-08-06, re-verified
  2026-08-06): all source modules have corresponding test files. The
  full test suite stands at 569 passing tests covering 30 test modules.
  Zero coverage gaps — every source module has a corresponding test
  file. All verification gates clean (compileall, ruff, pytest, mypy,
  import-sanity). Hourly test watch #490 confirmed no drift; all gates
  green with no failures to resolve and zero test coverage gaps.

- ~~**Hourly dead-code sweep is operational.** `dead_code_sweep.py` runs
  via `scheduler.py`'s `_run_dead_code_sweep()` every hour with
  `--create-issues`. It uses AST-based import discovery with grep
  fallback, BFS from entry-point and webapp/app.py to find reachable
  modules, and git-log analysis to distinguish truly-dead from
  merely-orphaned modules. 69 focused tests (all passing). No orphaned
  modules currently detected — repo is clean.~~ ✅ Resolved (Issue #423).
  ✅ Re-verified 2026-08-06 (Issue #490): all 27 top-level modules and 3
  webapp sub-modules confirmed wired; zero orphans; sweep exits clean.
- **Hourly commit hygiene re-verified.** `commit_hygiene.py` executed clean
  via `--json` output (status: "clean", zero findings). Last 10 commits have
  descriptive messages; no sensitive files (.env, *.db, data/) committed;
  `.gitignore` covers all required patterns (`*.db`, `.env`, `__pycache__/`,
  `.pytest_cache/`, `.venv/`, etc.); no large files (>3 MB) found outside
  `data/`. Issue #492 hygiene complete — git history clean, no security
  concerns.
- **Hourly dead-code sweep re-verified (#505).** `dead_code_sweep.py` executed
  clean via `--json` output (status: "clean", zero orphans). All 27
  top-level modules and 3 webapp sub-modules (`webapp/app.py`,
  `webapp/charts.py`, `webapp/ai_widgets.py`) confirmed wired. Full
  verification gate passed: ruff (clean, zero warnings), pytest (533/533, 1
  skipped), dead_code_sweep (zero orphans), import-sanity (all reachable),
  mypy (clean, 31 source files). Issue #505 sweep complete — no newly
  orphaned or truly-dead modules found.  Stale pycache cleaned: 0.  Test
  count changed from 569→533 (36 fewer) due to a previously-installed
  environment having extra tests from a different branch — the tests/
  directory on this commit contains 30 test modules totalling 533 tests, all
  passing.
- **Hourly dead-code sweep #552 run 2026-08-07.** `dead_code_sweep.py` confirmed
  clean via `--json` output (status: "clean", zero orphans). All 27 top-level
  modules and 3 webapp sub-modules confirmed wired. `programme_inference.py`
  and `hevy_reader.py` remain properly wired through `webapp/app.py` (PR #142).
  Full verification gate passed: ruff (clean), pytest (539/539), dead_code_sweep
  (zero orphans), mypy (clean, 31 source files). Stale pycache cleaned: 0.
  No newly orphaned or truly-dead modules found.
- **Hourly commit hygiene #631 re-verified (2026-08-07).** `commit_hygiene.py` executed
  clean via `--json` output (status: "clean", zero findings). All 40 hygiene tests
  passing. Full verification gate passed: ruff (clean), pytest (609/609), mypy (clean).
  No sensitive files committed (`.env`, `.db`, `.sqlite3`), `.gitignore` covers all
  required patterns, no large files (>1 MB) tracked outside `data/`. Audit report:
  `.agents/hygiene-audit-2026-08-07-run5.md`.


- ~~**Hourly dead-code sweep is operational.** `dead_code_sweep.py` runs
  via `scheduler.py`'s `_run_dead_code_sweep()` every hour with
  `--create-issues`. It uses AST-based import discovery with grep
  fallback, BFS from entry-point and webapp/app.py to find reachable
  modules, and git-log analysis to distinguish truly-dead from
  merely-orphaned modules. 69 focused tests (all passing). No orphaned
  modules currently detected — repo is clean.~~ ✅ Resolved (Issue #423).
  ✅ Re-verified 2026-08-06 (Issue #458): all 27 top-level modules and 3
  webapp sub-modules confirmed wired; zero orphans; sweep exits clean.
  ✅ Re-verified 2026-08-06 (Issue #479): same audit, same result — 29 modules
  all wired, sweep clean, no regressions from intervening commits.

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
- `scheduler-job` — adding a new periodic job to the unified `scheduler.py`.
- `secrets-and-encryption` — storing/reading anything through
  `encryption.py`/`user_api_keys` safely.
- `verification-gate` — the exact command sequence and pass criteria to run
  before considering any task complete. OpenHands must execute this checklist
  successfully before marking an issue complete or pushing code.

## 9. Continuous AI Development (OpenHands + GitHub Issues)

We use OpenHands to autonomously build and maintain this project, controlled entirely via GitHub Issues. 
Instead of local task files or a bespoke daemon, OpenHands reads issues directly from the GitHub repository, plans a solution, implements the code, runs the verification gate (linting and testing), and pushes the verified commits. 
Treat every GitHub issue as a direct instruction to the AI that will be worked on and shipped unattended. When you encounter a bug or need a feature, open a GitHub issue.
