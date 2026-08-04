---
name: connector-integration
description: 'Add a new external data source (wearable, nutrition app, other workout tracker) following the existing Hevy/Google-Health/Health-Connect connector patterns. Use when integrating any new third-party API, OAuth flow, or local data import into workout-agent.'
---

# Connector Integration

## When to Use

Adding any new source of external data: another workout tracker, a nutrition
app, a sleep tracker, a new wearable, etc.

## Existing Patterns to Follow (pick the closest match)

- **Personal API key, pull-based** (`hevy_client.py`) — a thin REST wrapper
  class, auth via a static per-user key header, no OAuth. Good fit for
  services with simple API-key auth (like Hevy, Strava with a personal
  token, etc). Store the key via `database.save_user_api_key(user_id,
  provider, api_key=...)` (Fernet-encrypted, see `secrets-and-encryption`
  skill) — never a bare env var for a per-user connector.
- **OAuth2 polling** (`google_health_auth.py` + `google_health_client.py`) —
  one-time authorize-code-grant flow to get a refresh token, then poll on
  each scheduled run/dashboard visit, refreshing the access token as needed.
  Good fit for anything with a real OAuth API (Fitbit, Garmin, Strava OAuth,
  Whoop). Store `client_id`/`client_secret`/`refresh_token` via
  `save_user_api_key` (it has dedicated `client_secret`/`refresh_token`
  fields for exactly this).
- **Local file import** (`health_connect.py`) — no network call at all, reads
  a JSON file the user syncs onto the host themselves (e.g. via Tasker/Health
  Sync on Android). Good fit for platforms with no public API but an
  exportable local sync path.

## Procedure

1. **New module**, `<service>_client.py` at repo root (matches existing flat
   layout — this project does not use a `connectors/` package). Keep it
   dependency-light: `requests`/stdlib only unless the service's official
   SDK is materially better.
2. **Parsing** stays separate from **fetching**, matching `hevy_client.py`
   (raw API) vs `hevy_parser.py` (turns raw payloads into typed summaries).
   Don't merge network I/O and data-shaping into one function — it makes the
   parser untestable without mocking HTTP.
3. **Writes go through `database.py`**, scoped by `user_id` (see
   `multi-tenant-migration` — a new connector must never write to an
   unscoped table).
4. **Failure isolation**: a connector failure (timeout, 401, malformed
   response) must not crash the scheduled run for other connectors/users —
   catch, log via the standard `logging` module, and continue. Match
   `hevy_client.py`'s existing exception handling.
5. **Settings UI**: add a card to `webapp/templates/settings.html` (key
   entry + a "Verify" button hitting a new `/api/settings/verify-<service>`
   endpoint, matching `/api/settings/verify-hevy`) — see `fastapi-route`
   skill.
6. **Scheduling**: if the connector needs periodic polling independent of
   the main daily run, use the `scheduler-job` skill rather than adding a
   third hand-rolled sleep loop.

## Verification

Run the `verification-gate` skill's steps. Add a test module,
`tests/test_<service>_client.py`, mocking HTTP calls (`requests-mock` or
manual `unittest.mock.patch`) — no test may make a real network call (matches
existing project convention, see `AGENTS.md` §7 test-coverage notes and the
current test suite's no-network-access design).

## Gotchas

- Never hardcode a single user's credentials/chat-id/webhook the way
  `telegram_notifier.py` currently does (`TELEGRAM_CHAT_ID` env var, one
  recipient) — a public-product connector must read per-user credentials
  from `user_api_keys`, not process env, unless it's explicitly a
  server-wide admin integration.
- Respect published rate limits — Hevy, Google Health, and most fitness APIs
  have low per-key request budgets; don't poll more often than the scheduled
  run actually needs.
