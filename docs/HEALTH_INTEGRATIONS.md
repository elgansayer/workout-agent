# Health integrations architecture

This document defines the target health-data integration architecture for Workout Agent. The goal is to ingest health and workout data from multiple providers without coupling coaching logic to any provider-specific payload.

## Principles

1. Treat every health integration as a per-user connector with explicit consent, minimum scopes, encrypted credentials, revocation, sync state, and provenance.
2. Normalize provider payloads into a canonical health model before they reach analytics, AI prompts, readiness scoring, or adaptive training.
3. Preserve source provenance and raw provider identifiers so duplicate records can be detected and provider-specific values can be audited.
4. Prefer incremental synchronization, idempotent upserts, backfill windows, bounded retries, and provider-aware rate limiting.
5. Never make medical diagnoses. Health signals can inform training recommendations, confidence, and recovery guidance only.
6. Health Connect is device-centric. A server cannot query a user's Health Connect store directly; an Android client or companion app must read authorized records on-device and securely sync them to Workout Agent.
7. Google Health API v4 is the long-term cloud API for Fitbit/Pixel health data. The legacy Fitbit Web API is migration-only and must not be used for new connections.

## Current repository state

The repository already contains three health-related paths:

- `backend/google_health_auth.py`: OAuth helper for the existing Google Health cloud integration.
- `backend/google_health_client.py`: Google Health API v4 cloud client used by the existing application.
- `backend/health_connect.py`: local JSON-file import path rather than a native Android Health Connect integration.

The provider-neutral connector layer is the long-term abstraction. In particular, Android Health Connect must not be described as a server-side Google account API, and the legacy Fitbit connector must not become a new production authorization path.

The accepted legacy-Fitbit cutover decision, identity mapping, consent flow, duplicate prevention and rollback policy are documented in [`FITBIT_GOOGLE_HEALTH_MIGRATION.md`](FITBIT_GOOGLE_HEALTH_MIGRATION.md).

## Provider strategy

| Provider | Integration model | Primary value | Target priority |
| --- | --- | --- | --- |
| Google Health | Google Health API v4 + Google OAuth 2.0 | Fitbit/Pixel cloud health, activity, sleep and measurements | P0 |
| Garmin | Garmin Connect cloud APIs | Sleep, HR, stress, Body Battery, activities, workout delivery | P0 |
| Health Connect | Android on-device SDK + companion sync | Aggregator for Android health/fitness apps and devices | P0 |
| Fitbit (legacy) | Retiring Fitbit Web API | Migration bridge for already-linked users only | Migration only |
| Oura | OAuth2 Cloud API v2 | Sleep, readiness, HR/HRV, workouts, SpO2 | P1 |
| Polar | OAuth2 AccessLink Dynamic API v4 | Training and daily activity data | P2 |
| Withings | Cloud API | Weight, body composition and health measurements | P2 |

### Google Health API

Google Health API is the target cloud integration for Fitbit and Pixel health data. Google describes it as the successor to the Fitbit Web API, using Google OAuth 2.0 and a v4 data-type model. Existing Fitbit OAuth tokens cannot be transferred, so users migrating from the legacy API must re-consent.

The current repository client already targets `https://health.googleapis.com/v4/users/me` and uses the read-only health metrics and measurements scope. Additional read scopes must be requested only when their corresponding product features ship. Write scopes remain disabled unless a concrete user-visible write feature requires them.

For migrated users, `users.getIdentity` is the authoritative bridge between the legacy Fitbit user ID and the Google Health `healthUserId`. Tenant records must never be joined by email or display name.

### Garmin

Garmin is a high-value integration because its Connect Developer Program exposes both ingestion and delivery APIs:

- Health API for all-day metrics such as steps, heart rate, sleep, stress, Pulse Ox, Body Battery, respiration and body composition.
- Activity API for detailed activities, including strength training, with FIT/GPX/TCX activity files.
- Training API for publishing workouts and training plans to Garmin Connect so compatible devices can receive them.

Garmin requires application approval. Commercial use of the Health API requires a licence fee, so commercial feasibility and approval are explicit project tasks rather than hidden implementation assumptions.

### Android Health Connect

Health Connect is an Android, on-device health data store. It is built into Android 14+ and is available on supported older Android versions through Google Play. Apps read and write standardized records only after the user grants the relevant permissions.

Target architecture:

```text
Wearables / health apps
        |
        v
Android Health Connect
        |
        v
Workout Agent Android companion
        |
        | authenticated, encrypted sync
        v
Workout Agent ingestion API
        |
        v
Canonical health model
```

The existing JSON import can remain as a fallback/manual bridge, but it is not the intended multi-user product integration.

### Fitbit Web API (legacy)

Google states that the legacy Fitbit Web API will be turned down in September 2026. Workout Agent therefore treats this connector as a temporary migration sentinel only:

- no new Fitbit Web API authorization;
- existing users retain their working legacy connection until Google Health OAuth, identity mapping and a representative read have succeeded;
- after successful cutover, legacy credentials are revoked/deleted where possible;
- after shutdown, unmigrated users enter a reconnect-required state rather than silently failing or receiving fabricated data.

The exact step-up and rollback flow is defined in [`FITBIT_GOOGLE_HEALTH_MIGRATION.md`](FITBIT_GOOGLE_HEALTH_MIGRATION.md).

### Oura

Oura API v2 is the supported cloud integration point. Multi-user applications use OAuth2. Useful scopes include daily summaries, heart rate, workouts and SpO2. Oura's daily sleep/readiness data makes it particularly useful for recovery and readiness features.

### Polar

Polar AccessLink Dynamic API v4 uses OAuth2 and exposes user training and activity data. It should be implemented behind the same connector contract and canonical normalization layer as the other cloud providers.

### Withings

Withings is primarily valuable for body weight, body composition and other health measurements. It complements wearable recovery/activity providers rather than replacing them.

## Target system architecture

```text
Google Health ------\
Garmin --------------\
Oura -----------------+--> Provider adapters --> Normalization --> Canonical store
Polar ----------------/                              |                 |
Withings ------------/                               |                 +--> analytics
Health Connect companion ----------------------------/                 +--> readiness
                                                                      +--> AI context
                                                                      +--> adaptive training

Legacy Fitbit --> migration/identity bridge --> Google Health
Workout plan --------------------------------------------------------------> Garmin Training API
```

Provider adapters should implement the provider-neutral connector contract tracked in issue #820. The contract needs authorization, capability discovery, connection testing, synchronization, refresh, status, disconnect, purge and normalized error handling.

## Synchronization lifecycle

Each supported provider sync follows the same lifecycle:

1. User initiates connection.
2. Workout Agent explains requested permissions and purpose.
3. Provider authorization or device permission is granted.
4. Credentials/tokens are encrypted and associated with the authenticated user.
5. Initial bounded backfill runs.
6. Incremental sync uses provider cursors, timestamps, notifications or webhooks where supported.
7. Raw records are validated and normalized.
8. Canonical records are idempotently upserted with source provenance.
9. Derived daily summaries and readiness inputs are recomputed only for affected dates.
10. Disconnect revokes upstream access where supported and stops future sync jobs.
11. Purge and account deletion follow the product data-retention policy.

Legacy Fitbit differs only during migration: the old connection remains intact until the replacement Google Health connection has been verified and switched atomically.

## Capability model

Providers do not expose identical metrics. Code must check capabilities instead of assuming fields exist.

Example capability families:

- sleep summary and sleep stages
- resting heart rate
- heart-rate time series
- HRV / recovery signals
- steps and activity
- calories / energy
- stress / readiness scores
- SpO2
- respiration
- body weight and body composition
- workouts / activity sessions
- training load
- structured workout export

A provider-specific score such as Garmin Body Battery or Oura Readiness remains a sourced metric. It must not be silently mapped to a universal readiness score without preserving its original source and semantics.

For Fitbit-to-Google-Health migration, each required legacy feature must be checked against Google's official parity tool before activation. Missing parity degrades only the affected capability; it is not a reason to reopen new legacy Fitbit onboarding.

## Adaptive training boundary

Health data should feed a transparent training decision layer rather than being inserted directly into an opaque prompt. The initial adaptive-training engine should calculate feature inputs such as:

- sleep duration and deviation from personal baseline
- resting-heart-rate deviation from baseline
- HRV deviation from baseline when available
- recent training volume and intensity
- time since last high-load session
- subjective check-in / soreness / fatigue
- provider-specific recovery signals, tagged with source

The engine then emits a bounded recommendation such as proceed, reduce volume, reduce intensity, substitute recovery work, or request user confirmation. Every recommendation must retain the contributing signals and confidence so the UI and AI coach can explain why it changed the plan.

## Privacy and safety requirements

Health data is sensitive product data. Implementation must integrate with the existing privacy/security backlog, especially authentication, data classification, consent ledger, deletion/export, no-store caching, log redaction and tenant isolation.

Minimum requirements include:

- least-privilege scopes and permission explanations
- encryption of OAuth secrets and refresh tokens
- strict `user_id` ownership on connections, raw records and normalized records
- no health payloads or secrets in application logs
- no cross-user cache keys
- revocation and deletion workflows
- retention rules for raw payloads versus normalized metrics
- source and consent provenance for every imported record
- stable external identity mapping during provider migrations

## Rollout order

1. Finish provider-neutral connector contract and canonical health data model.
2. Complete the Fitbit-to-Google-Health migration policy and prevent new legacy Fitbit authorization.
3. Validate the existing Google Health OAuth/API v4 implementation against official contracts (#821), including identity mapping and required parity fixtures.
4. Migrate already-linked Fitbit users through step-up Google consent before the September 2026 legacy shutdown.
5. Build the Android Health Connect companion and ingestion API.
6. Apply for Garmin access while implementing provider-independent storage and sync infrastructure.
7. Implement Garmin Health + Activity ingestion, then Garmin Training export.
8. Add Oura, then Polar and Withings cloud connectors.
9. Build readiness feature extraction, baseline calculations and explainable adaptive-training rules.
10. Add provider status, freshness, reconnect and permission controls to Settings plus production observability, contract tests and deletion/export verification.

## Official references

- Google Health API: https://developers.google.com/health
- Google Health migration overview: https://developers.google.com/health/migration
- Google Health migration API specifications: https://developers.google.com/health/migration/api-specifications
- Google Health Fitbit parity tool: https://developers.google.com/health/migration/parity-tool
- Google Health `users.getIdentity`: https://developers.google.com/health/reference/rest/v4/users/getIdentity
- Google Health legacy shutdown background: https://developers.google.com/health/about
- Android Health Connect: https://developer.android.com/health-and-fitness/health-connect
- Android Health Connect availability: https://developer.android.com/health-and-fitness/health-connect/availability
- Garmin Connect Developer Program: https://developer.garmin.com/gc-developer-program/overview/
- Garmin Health API: https://developer.garmin.com/gc-developer-program/health-api/
- Garmin Activity API: https://developer.garmin.com/gc-developer-program/activity-api/
- Garmin Training API: https://developer.garmin.com/gc-developer-program/training-api/
- Oura API v2: https://cloud.ouraring.com/v2/docs
- Polar AccessLink Dynamic API v4: https://www.polar.com/polar-api-v4/
- Withings developer platform: https://developer.withings.com/
