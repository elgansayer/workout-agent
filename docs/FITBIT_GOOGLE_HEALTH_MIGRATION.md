# Fitbit Web API to Google Health API migration

**Status:** Accepted  
**Decision date:** 2026-08-20  
**Owner:** Workout Agent health integrations  
**Tracks:** #825

## Decision

Workout Agent will use **Google Health API v4 with Google OAuth 2.0 as the long-term cloud API for Fitbit and Pixel health data**. The legacy Fitbit Web API is a migration-only path for accounts that were already linked before this decision. New users must not be authorized against the legacy Fitbit Web API.

Google states that the legacy Fitbit Web API will be turned down in **September 2026** and existing integrations must migrate before then. Google has not published a specific shutdown day on the referenced migration pages, so Workout Agent uses **2026-09-01 as an internal completion target**, not as a claim about Google's exact shutdown date.

This decision does not replace Android Health Connect. Health Connect remains an on-device Android data source. Google Health API is the cloud API that succeeds the Fitbit Web API.

## Why this is the target

Google describes Google Health API as the next-generation replacement for the Fitbit Web API, rebuilt on Google infrastructure with Google OAuth 2.0, a v4 resource model, consolidated data-type endpoints, and a new response schema. The migration is not token-compatible: legacy Fitbit access and refresh tokens cannot be transferred to Google Health API, so existing users must explicitly re-consent.

The repository already contains `backend/google_health_auth.py` and `backend/google_health_client.py`, with the latter calling `https://health.googleapis.com/v4/users/me`. Those modules are therefore the migration target. Issue #821 owns end-to-end validation of the live Google Health v4 contracts; this decision resolves which platform future implementation should target.

## Platform comparison

| Concern | Legacy Fitbit Web API | Google Health API v4 | Workout Agent policy |
| --- | --- | --- | --- |
| Product role | Existing Fitbit integration | Successor cloud health API | Google Health is canonical for new work |
| Authentication | Fitbit OAuth / Fitbit-issued tokens | Google OAuth 2.0 | New users use Google OAuth |
| Token migration | Existing Fitbit tokens | Different token family | Tokens cannot be copied; require re-consent |
| User identity | Fitbit encoded user ID | `healthUserId`, with migration mapping from `legacyUserId` | Store both IDs during migration |
| Identity bridge | Legacy user ID only | `users.getIdentity` returns both IDs | Use this mapping, never email/display name, to join accounts |
| API shape | Many legacy endpoint families | v4 data-type resources, rollups and list methods | New parsers target v4 contracts |
| Permissions | Legacy Fitbit scopes | `googlehealth.*` OAuth scopes | Request only feature-required read scopes |
| New connections | Retiring platform | Supported platform | Legacy authorization disabled in code |
| Shutdown | Google says September 2026 | Long-term target | Complete cutover before September |
| Feature parity | Existing Fitbit endpoint behavior | Still evolving | Validate each required feature with Google's parity tool |

## Least-privilege scope plan

Scopes must be derived from enabled product features instead of requesting a broad bundle up front.

| Workout Agent feature | Google Health scope | Rule |
| --- | --- | --- |
| Weight, body fat, resting HR, HRV and related measurements | `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly` | Current cloud body/recovery metric path |
| Activity and workout reads | `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly` | Add only when the feature ships |
| Sleep reads | `https://www.googleapis.com/auth/googlehealth.sleep.readonly` | Add only when sleep ingestion ships |
| Profile reads | `https://www.googleapis.com/auth/googlehealth.profile.readonly` | Request only when identity/profile data beyond `users.getIdentity` is required |
| Settings reads | `https://www.googleapis.com/auth/googlehealth.settings.readonly` | Request only when units/time-zone settings are consumed |

Write scopes are out of scope until Workout Agent ships a user-visible feature that actually writes that data. Partial consent must degrade only the dependent feature.

## Existing-user migration flow

The migration is a tenant-scoped step-up flow. A failed migration must not destroy the working legacy connection before the replacement is proven.

1. Detect a connection whose stored provider/auth type is legacy Fitbit and mark it `attention` with a migration action. Do not offer legacy Fitbit authorization to a new account.
2. Start a fresh Google OAuth 2.0 consent flow for the authenticated Workout Agent owner. Keep the existing Fitbit credentials unchanged while this is in progress.
3. Exchange and verify the new Google Health credentials without logging or echoing them.
4. Call `GET https://health.googleapis.com/v4/users/me/identity` with the new credential.
5. Compare the returned `legacyUserId` with the external Fitbit identity already attached to the local connection. Store the returned `healthUserId` alongside the legacy ID. **Do not merge by email, display name, or an arbitrary client-supplied ID.**
6. If the identity mapping is missing or conflicts with the existing owner, stop the cutover and enter an explicit conflict/review state. Never attach the newly authorized source to another local user's records.
7. Run a bounded overlap/backfill against only the data types used by Workout Agent. Upserts must use provider record identity plus provenance so records already imported from Fitbit are not duplicated merely because the transport moved to Google Health.
8. Verify the required capabilities and a representative read before changing the active auth/provider state.
9. Atomically make Google Health the active connection. Only after that succeeds may the legacy Fitbit token be revoked and deleted, where upstream revocation is still available.
10. Record the migration outcome, both external IDs, consent version, scopes, cutover time, and a safe correlation ID in the connector/audit state. Never record raw access or refresh tokens in audit data.

### Retry and idempotency

A repeated migration request for the same local owner and the same `healthUserId` must converge on the same connection. Replaying the finalization step must not create a second connector, duplicate health records, or revoke the active Google credential.

## Duplicate prevention and provenance

The API migration must not be treated as a new person or a brand-new health history.

- `users.getIdentity` is the identity bridge between the legacy Fitbit user ID and the Google Health user ID.
- Existing canonical records retain their original source/provenance and external IDs.
- A migration backfill may intentionally overlap the legacy import window, but ingestion must be idempotent.
- If Google Health presents the same underlying observation under a different record identity, reconciliation must use the repository's canonical deduplication/conflict policy rather than silently inserting both.
- Source timestamps and ingestion timestamps remain distinct so recomputation can explain why a record changed.

## Data and capability gaps

Google Health API is actively evolving. Before enabling a Fitbit-derived feature on Google Health, implementation must compare its legacy endpoint/functionality in Google's official parity tool and pin deterministic contract fixtures.

A missing capability must be visible as unavailable or degraded. Workout Agent must not fabricate an equivalent metric or silently substitute a different measurement. Provider-specific scores retain their source semantics.

Google's current data-type documentation should be treated as the authority for supported records. Any device- or data-type limitation discovered by the parity tool belongs in the connector capability metadata and user-facing status rather than being hidden.

## Rollback and fallback

### Before successful cutover

The legacy Fitbit connection remains the fallback for an already-linked user while Google OAuth, identity validation, and the first Google Health read are incomplete. If any step fails, leave the legacy connection intact, record a safe failure category, and allow the user to retry Google consent.

### After successful cutover

Once Google Health credentials are verified, the identity mapping is accepted, the active connection is switched, and the legacy Fitbit token is revoked/deleted, **do not silently reactivate Fitbit OAuth**. Recovery is a Google Health reconnect/reauthorization flow. Historical records remain available under their preserved provenance.

### After the September 2026 legacy shutdown

There is no legacy network fallback. An unmigrated user enters an `attention`/reconnect-required state. Existing imported history remains readable, but new health sync is disabled until Google Health authorization succeeds.

### Feature parity fallback

If one required legacy feature has no acceptable Google Health equivalent, disable only that dependent feature and preserve the rest of the migrated connection. Track the gap explicitly with an issue and Google's parity result. Do not keep onboarding users to the retiring API to mask the gap.

## Repository enforcement

`backend/connectors/fitbit.py` is intentionally retained as a **migration sentinel**:

- `authorize`, `sync`, `refresh`, backfill, webhooks and writes are not advertised as capabilities.
- status is `attention`, with machine-readable metadata naming `google_health` as the target and marking re-consent as required.
- data operations fail with the stable `legacy_provider_migration_required` error instead of suggesting that legacy production activation is pending.

This prevents future feature work from accidentally treating the legacy Fitbit connector as a supported new integration while preserving a registry entry that can identify existing Fitbit-backed accounts.

`backend/google_health_auth.py` and `backend/google_health_client.py` remain the current target implementation. Issue #821 should harden those paths against the official v4 contracts rather than adding new legacy Fitbit network code.

## Exit criteria

The migration is complete when all of the following are true:

- no new legacy Fitbit OAuth authorization is reachable;
- all active Fitbit-backed users have either migrated or have an explicit reconnect-required state;
- Google Health identities are mapped using `users.getIdentity` and tenant ownership is verified;
- required data types have parity/contract coverage;
- duplicate-safe backfill and cutover tests pass;
- legacy Fitbit credentials have been revoked/deleted after successful cutover;
- monitoring contains no production calls to the legacy Fitbit API after the internal 2026-09-01 target.

## Official sources

Verified on 2026-08-20:

- Google Health API overview: https://developers.google.com/health
- Google Health API migration guide: https://developers.google.com/health/migration
- Migration API specifications: https://developers.google.com/health/migration/api-specifications
- Migration authorization/data access: https://developers.google.com/health/migration/data-access
- Migration example implementation: https://developers.google.com/health/migration/example-implementation
- Fitbit/Google Health parity tool: https://developers.google.com/health/migration/parity-tool
- `users.getIdentity` reference: https://developers.google.com/health/reference/rest/v4/users/getIdentity
- Google Health developer checklist: https://developers.google.com/health/developer-checklist
- Google Health release notes: https://developers.google.com/health/release-notes
- Google Health API background and legacy shutdown statement: https://developers.google.com/health/about
