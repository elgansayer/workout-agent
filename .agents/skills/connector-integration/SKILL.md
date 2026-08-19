---
name: connector-integration
description: 'Add a new external data source (wearable, nutrition app, workout tracker, or device health store) following the provider-neutral connector architecture. Use when integrating a third-party API, OAuth flow, Android Health Connect, webhook, or local import.'
---

# Connector Integration

## When to Use

Use this skill when adding any external data source such as a workout tracker,
nutrition app, sleep tracker, wearable, scale, or health aggregator.

Read `docs/HEALTH_INTEGRATIONS.md` and `docs/HEALTH_DATA_MODEL.md` before adding a
health provider.

## Integration patterns

Choose the closest transport/authentication pattern, but keep provider-specific
payloads behind the common connector and normalization boundaries.

- **Personal API key, pull-based** (`hevy_client.py`) - a thin REST wrapper with
  a per-user credential. Store the key through the encrypted user credential
  storage. Never use a process environment variable for a public-product user's
  connector secret.
- **OAuth2 cloud provider** - appropriate for services such as Fitbit, Oura,
  Polar, and approved Garmin Connect APIs. Store per-user tokens encrypted,
  implement refresh/revocation, validate `state`, request minimum scopes, and
  isolate provider errors. The existing `google_health_auth.py` and
  `google_health_client.py` are migration references, not the target abstraction.
- **Android Health Connect** - Health Connect is an on-device Android data store,
  not a server-side Google account API. A product integration requires an
  Android client/companion to request permissions, read records locally, retain
  data-origin provenance, and upload normalized or validated records through an
  authenticated Workout Agent ingestion API.
- **Local file import** (`health_connect.py`) - the existing JSON import is a
  fallback/bridge for user-synced files. Do not describe it as a native Health
  Connect integration.
- **Webhook/push provider** - validate signatures, map the event to the correct
  tenant/connection, enqueue idempotent processing, and never trust a provider
  payload to select an arbitrary `user_id`.

## Provider-neutral requirements

New providers must integrate with the connector registry/contract tracked in
issue #820 rather than introducing another one-off lifecycle. The common
lifecycle covers:

- authorize/connect
- capability discovery
- connection test
- initial backfill
- incremental sync
- token refresh where applicable
- status/freshness
- disconnect/revoke
- purge/delete
- normalized error mapping

Health providers must normalize into the canonical model documented in
`docs/HEALTH_DATA_MODEL.md`. Analytics, readiness logic, AI prompts, and UI code
must not depend directly on Garmin/Fitbit/Oura/etc. response shapes.

## Procedure

1. **Research the current official contract.** Verify API version, application
   approval, OAuth/scopes, rate limits, webhook/push options, commercial terms,
   data retention requirements, and revocation behaviour from first-party docs.
2. **Implement the connector adapter.** Keep transport/authentication separate
   from parsing and normalization. Prefer an official SDK only when it provides a
   material security, protocol, or maintenance advantage over the repository's
   existing HTTP stack.
3. **Normalize data.** Convert provider records to canonical DTOs while
   preserving provider, upstream record ID, source device/app, timestamps,
   normalization version, and provider-specific metadata needed for audit.
4. **Write through tenant-scoped storage.** Every connection, cursor, raw record,
   normalized record, and sync run belongs to an authenticated `user_id`.
5. **Make sync idempotent.** Use stable upstream IDs or deterministic
   provider-specific fingerprints. Retries and backfills must not duplicate
   metrics or activities.
6. **Add failure isolation.** A timeout, quota error, expired token, malformed
   record, or provider outage must not crash synchronization for other users or
   providers.
7. **Expose lifecycle UI.** Settings should show connect, permissions, status,
   freshness, test, resync/backfill when safe, reconnect, and disconnect without
   displaying secrets.
8. **Schedule responsibly.** Use the unified scheduler for polling providers;
   prefer push/webhooks where officially supported and operationally reliable.
9. **Integrate privacy lifecycle.** Consent, deletion/export, retention, cache
   controls, and log redaction must follow the existing privacy/security backlog.

## Health-provider-specific rules

- **Garmin:** treat Health, Activity, and Training APIs as separate capabilities.
  Do not assume production access until the Garmin Connect Developer Program has
  approved the integration. Commercial Health API use has licensing implications.
- **Health Connect:** retain `DataOrigin`/source information so direct-provider
  and aggregator records can be deduplicated without incorrectly merging
  unrelated measurements.
- **Fitbit:** validate the current Fitbit Web API contract at implementation time;
  do not repeat unsupported deprecation/migration claims from older repository
  documentation.
- **Oura:** use API v2 and OAuth2 for multi-user product access.
- **Polar:** target the current AccessLink Dynamic API contract rather than a
  historical version when starting new work.
- **Withings:** preserve measurement provenance and units carefully because body
  composition is a primary use case.

## Verification

Run the `verification-gate` skill. Every connector needs deterministic tests for:

- authorization/callback security where applicable
- token refresh and revocation
- tenant ownership
- pagination/backfill/incremental cursors
- idempotency and deduplication
- normalization fixtures
- rate-limit and retry behaviour
- malformed/partial provider responses
- disconnect and purge
- no-network unit/contract tests using mocks or recorded synthetic fixtures

Never commit real user credentials or personal health payloads as fixtures.

## Gotchas

- Never hardcode a user's credentials, chat ID, webhook, provider identity, or
  source path in production connector code.
- Never log OAuth codes, access/refresh tokens, authorization headers, raw health
  payloads, or AI prompts containing health records.
- Provider-native scores are not interchangeable. Garmin Body Battery, Oura
  Readiness, and future vendor scores must retain their provider semantics.
- When the same underlying data arrives through both a direct connector and an
  aggregator such as Health Connect, preserve both provenance paths and apply an
  explicit source-precedence/deduplication policy.
- Do not let a connector write directly into AI context. Normalize and validate
  first, then derive bounded readiness/adaptive-training features.
