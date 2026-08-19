# Canonical health data model

Workout Agent must store health information independently of the provider that supplied it. Provider payloads are normalized at the ingestion boundary, while raw source identifiers and provenance are retained for audit, deduplication and reprocessing.

## Goals

- support multiple health providers per user
- allow the same metric to arrive from more than one source
- preserve provider provenance and provider-specific metrics
- make synchronization idempotent and retry-safe
- support per-user deletion, export and connector revocation
- provide stable inputs to analytics, readiness and adaptive training
- avoid coupling AI prompts or UI components to vendor payloads

## Core entities

### `HealthConnection`

Represents one user's authorization or device-sync relationship with a provider.

Suggested fields:

```text
id
user_id
provider
external_user_id
status
scopes
capabilities
authorised_at
last_sync_at
last_success_at
last_error_code
sync_cursor
token_secret_reference
metadata_json
created_at
updated_at
```

OAuth access tokens, refresh tokens and client secrets must use the existing encrypted secret-storage service rather than plaintext columns. `token_secret_reference` is conceptual and may be represented by the existing user credential model.

### `HealthMetric`

Stores a normalized measurement or interval.

```text
id
user_id
connection_id
provider
metric_type
start_at
end_at
value_numeric
value_text
unit
source_record_id
source_device_id
source_app_id
quality
metadata_json
recorded_at
updated_at
```

`metric_type` should be an application-owned vocabulary, for example:

```text
body_weight
body_fat_percentage
resting_heart_rate
heart_rate
hrv_rmssd
steps
sleep_duration
sleep_score
spo2
respiration_rate
stress_score
body_battery
readiness_score
active_energy
```

Provider-specific metrics are allowed, but their semantics must remain explicit. For example, `body_battery` should remain Garmin-sourced rather than being renamed to generic `readiness_score`.

### `SleepSession`

Sleep data is sufficiently structured to deserve a dedicated entity rather than flattening every field into daily metrics.

```text
id
user_id
connection_id
provider
source_record_id
start_at
end_at
time_in_bed_seconds
sleep_seconds
awake_seconds
light_seconds
deep_seconds
rem_seconds
score
metadata_json
```

Detailed stage samples may be stored separately when a provider exposes them and when retention requirements justify keeping them.

### `WorkoutActivity`

Represents wearable-recorded activities independently of Hevy strength-log sessions.

```text
id
user_id
connection_id
provider
source_activity_id
activity_type
start_at
end_at
duration_seconds
distance_m
active_energy_kcal
average_hr
max_hr
training_load
source_file_type
source_file_reference
metadata_json
```

A future reconciliation layer can associate a wearable `WorkoutActivity` with a Hevy workout without merging away either source record.

### `DailyHealthSummary`

A derived cache for fast coaching and dashboard reads. It is recomputable from normalized source records and therefore must not be the sole copy of imported information.

```text
user_id
date
sleep_seconds
resting_hr
hrv_rmssd
steps
active_energy_kcal
weight_kg
body_fat_pct
spo2_avg
readiness_score
readiness_source
recovery_confidence
computed_at
```

The exact schema can evolve as the canonical vocabulary is implemented.

### `HealthSyncRun`

Tracks connector execution and provides operational visibility without logging sensitive payloads.

```text
id
user_id
connection_id
provider
started_at
finished_at
status
sync_mode
cursor_before
cursor_after
records_received
records_created
records_updated
records_skipped
error_code
retry_count
```

## Provenance and deduplication

Every normalized record should retain:

- provider
- connection identifier
- upstream record identifier when available
- source device/application when provided
- source timestamps
- normalization version

Primary deduplication should use `(connection_id, provider, source_record_id)` where the upstream API provides a stable ID. Providers without stable IDs need a deterministic provider-specific fingerprint derived from immutable record properties.

Do not deduplicate different providers merely because their timestamps and values match. Health Connect may aggregate records originally written by other apps, so the source application/data origin should be retained to avoid double counting when direct and aggregated connectors are both enabled.

## Source precedence

Workout Agent needs an explicit source policy for derived summaries. A user may connect Garmin directly and also receive Garmin-originated data through Health Connect.

Rules should be configurable by metric family and initially follow these principles:

1. Prefer a direct first-party provider record over the same data re-exported through an aggregator when the records can be identified as duplicates.
2. Preserve all non-duplicate source records even when only one source is selected for daily summaries.
3. Never average incompatible vendor scores such as Garmin Body Battery and Oura Readiness.
4. Let users inspect which provider supplied a displayed metric.
5. Record the selected source and rule version on derived summaries.

## Baselines and readiness features

Recovery features should use personal rolling baselines rather than global thresholds where possible.

Candidate derived features include:

```text
sleep_delta_vs_28d_baseline
resting_hr_delta_vs_28d_baseline
hrv_delta_pct_vs_28d_baseline
training_load_1d
training_load_7d
training_load_28d
hours_since_last_hard_session
subjective_fatigue
subjective_soreness
```

The readiness/adaptation layer must expose the inputs that influenced a recommendation. A user should be able to see that a reduced-volume recommendation came from, for example, poor sleep plus elevated resting heart rate rather than from an unexplained AI decision.

## Retention tiers

Implement retention separately for:

- encrypted connection credentials
- raw provider payloads/files
- normalized health records
- derived daily summaries
- AI prompt/context snapshots
- operational sync metadata

Raw payloads may have a shorter retention period than normalized records. Deleting a connector or account must follow the product's deletion policy and remove/revoke associated credentials and records as required.

## API boundary

Provider adapters should emit validated internal DTOs rather than writing arbitrary JSON directly into analytics tables. A conceptual interface is:

```python
class HealthProviderAdapter:
    def capabilities(self) -> set[str]: ...
    async def authorize(...): ...
    async def refresh(...): ...
    async def sync(...): ...
    def normalize(self, raw_record) -> list[CanonicalHealthRecord]: ...
    async def disconnect(...): ...
    async def purge(...): ...
```

This complements the provider-neutral connector registry tracked in issue #820. The final implementation should follow the repository's actual typed connector contract rather than introducing a parallel abstraction.

## Migration notes

The existing `body_metrics` and Google/Health Connect paths should be migrated incrementally. Do not destroy existing history merely to introduce the canonical schema. Recommended migration:

1. introduce canonical tables and normalization vocabulary
2. dual-write new connector data during migration
3. backfill existing body metrics with explicit legacy provenance
4. switch analytics/readiness reads to canonical summaries
5. verify parity
6. retire legacy writes only after rollback is no longer required
