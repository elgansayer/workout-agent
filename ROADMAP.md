# Workout Agent roadmap

Last reviewed: 2026-09-22 (weekly architecture pass)

This file records the current implementation boundary and the next engineering priorities. `AGENTS.md` remains the contribution and security contract. `CONTINUE.md` is an older technology evaluation; its broad framework and package swaps are not commitments unless they are promoted here with an implementation boundary.

## Product direction

- Keep the product multi-user and fail closed for authentication, tenant ownership, credential storage and health-data handling.
- Keep programmes Hevy-first: imported routines are source material, while Workout Agent owns versioned prescriptions, adaptations and conflict resolution.
- Resolve AI providers, models and credentials per authenticated user through the provider abstraction.
- Bring health providers through one tenant-scoped connector, normalization, provenance and persistence path before using their data for coaching.
- Prefer deterministic, versioned domain decisions; AI may explain validated output but may not author unchecked programme or safety state.

## Current implementation snapshot

| Area | Implemented now | Important remaining boundary |
| --- | --- | --- |
| Web security | The production ASGI entrypoint applies Google session authentication, an explicit anonymous allowlist, mutation ownership checks, CSRF protection, private cache policy, proxy validation and security headers. | AI-generating GET routes still cause external calls and persistence outside the mutation/CSRF contract. |
| Core tenancy | Users and the main workout, progress, body-metric, programme, preference, notification, check-in, chat and insight records have tenant columns or tenant-aware accessors. | Several production callers omit `user_id`, and optional unscoped accessor modes can aggregate or select another tenant's data. |
| Hevy programme builder | The Angular builder reads live Hevy routines, accepts ordered selections and goals, creates a deterministic source-bound preview, activates a Hevy programme and renders that programme in Plan and Dashboard. Static template selection is retired in these routes. | The daily agent, check-ins and routine sync still execute the static `program.py` split; active source snapshots are not reconciled after a Hevy edit or deletion. |
| AI coaching | Gemini, Claude, OpenAI and DeepSeek share a provider abstraction; per-user preferences and keys are available; Coach history and streamed responses are persisted. | The deployed scheduler does not reliably dispatch per user, Coach context has an unscoped personal-record read, and some prompts/output contracts remain hard-coded or unvalidated. |
| Credential handling | Per-user API-key records and a Fernet helper exist, and Settings masks stored keys. | Encryption deliberately falls back to plaintext, while Google Health refresh tokens still use generic metadata and can be printed by the authorization helper. |
| Health platform | Provider-neutral contracts, canonical models, provenance, normalization helpers, readiness logic and many focused tests exist. The legacy Google Health client can fetch a small body-metric set. | The canonical schema is not part of application initialization, uses integer user IDs instead of the application's UUID strings, leaves sleep/activity repository writes as no-ops, and is not connected to a production connector lifecycle. |
| Scheduling and delivery | Docker runs one Python scheduler; coaching can save in-app notifications and send tenant-owned Web Push subscriptions. | The container enters the legacy global schedule loop. The unused per-user loop invokes unsupported CLI arguments, so timezone-aware multi-user dispatch is not operational. |
| Angular client | Dashboard, Coach, Check-ins, History, Plan, Programmes, Progress, Settings and Stats are routed and backed by APIs. | Stored-key Hevy verification sends a sentinel value as if it were a real credential; critical authenticated flows have little component/browser coverage. |

## Next priorities

### P0 — restore security and runtime correctness

1. Route the production scheduler through one tenant-aware dispatcher, including per-user insight jobs and restart-safe de-duplication.
2. Make the daily agent execute the active Hevy-native programme and remove production dependencies on the retired static split.
3. Close the known dashboard and Coach tenant-read omissions with two-user regression tests.
4. Move AI generation to validated POST contracts protected by the existing CSRF boundary.
5. Make credential encryption fail closed and move Google Health OAuth tokens into encrypted per-user storage.

### P1 — complete the health and programme vertical slices

1. Integrate canonical health tables and repository operations with the application database and UUID user model.
2. Implement Google Health through the provider-neutral connector, normalization and sync-run path.
3. Detect Hevy source drift for active programmes and provide explicit, versioned reconciliation actions without mutating the active snapshot silently.

### P2 — finish setup reliability and verification

1. Make the Hevy connection test use the authenticated user's stored credential when no replacement key is entered.
2. Add deterministic Angular component/browser coverage for sign-in boundaries, credential setup, programme preview/activation and CSRF-protected mutations after the P0 API changes settle.

Broad ORM, CSS-framework, chart-library and authentication-provider swaps from `CONTINUE.md` remain deferred. They should not displace the tenant, programme-runtime, secret-storage and health-ingestion boundaries above without a separate decision.
