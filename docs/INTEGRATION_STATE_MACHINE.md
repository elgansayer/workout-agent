# Integration state machine

One state model explains whether an account, and each connector it depends on,
is usable. Settings, the dashboard, the plan, the programme builder, Coach,
notifications and the API all render the same derived status. A binary
"connected / not connected" flag is never the source of truth.

Code: `backend/integration_state.py` (pure derivation),
`backend/integration_status.py` (tenant-scoped facts, run recording, alerts),
`backend/database.py` (`connector_runs`). Tests:
`test_integration_state.py`, `test_integration_status.py`,
`test_connector_runs.py`, `test_webapp_integration_status.py`,
`test_main_integration_status.py`.

## Principles

1. **Derived, never stored.** State is a pure function of persisted facts and an
   explicit clock: `derive_integration_state(facts, now=...)`. The same facts at
   the same `now` always give the same state, reason and next action.
2. **Facts are persisted and tenant-scoped.** Every read and write filters by the
   authenticated user. Nothing trusts a client-supplied owner id.
3. **Evidence belongs to a credential.** Runs recorded before the credential on
   file was last changed are ignored, so a new key or re-authorisation starts
   clean.
4. **Absence of evidence is not readiness.** A saved key is `connecting` until a
   verification or sync succeeds. Staleness is derived from time, so an
   unobserved failure still ends in `stale`.
5. **Safe copy only.** Messages are built from the state, never from provider
   text, so secrets and raw errors cannot reach a screen, log or prompt.

## States

| State | Meaning | Typical user action |
| --- | --- | --- |
| `setup_required` | Nothing usable is configured: no credential, a rejected credential that never worked, an unavailable connector or no programme. | Add or fix credentials, build a programme. |
| `connecting` | A credential exists but has not been proven: authorising, verifying, or waiting for the first successful sync. | Wait, verify, or run the first sync. |
| `syncing` | A sync is running right now. | Wait. |
| `ready` | Credential proven, latest sync recent, no unabsorbed failure. | None. |
| `stale` | The last successful sync is older than the stale threshold (or the programme has finished). | Sync again or start a new programme. |
| `degraded` | Something is wrong but earlier data exists: expired or revoked credential, partial import, repeated or non-retryable failures, a programme needing review. | Reconnect, retry, review. |

## Persisted facts

| Fact | Stored in | Written by |
| --- | --- | --- |
| Credential present and when it last changed | `user_api_keys` (`updated_at`; presence is read without decrypting) | Settings key save and delete |
| Google Health authorisation | `hevy_meta` token presence plus `authorize` runs | `/google-health/connect`, `/callback`, `/disconnect` |
| Connector run ledger | `connector_runs` | verification, history sync, programme builder reads, Coach requests, the scheduled agent, OAuth flow |
| Active programme | `programmes` | programme activation |

`connector_runs` columns: `user_id` (foreign key to `users`, immutable, first
column of the index), `connector` (`hevy`, `google_health`, `ai:<provider>`),
`kind`, `status`, `started_at`, `finished_at`, `error_code`, `retryable`,
`fetched`, `written`, `skipped`. Rows hold codes, timestamps and counters only,
never payloads or secrets. Retention keeps the newest 50 runs plus the newest 20
successes per tenant and connector, and never prunes a running row.

Run kinds: `authorize` (OAuth/consent flow), `test` (credential verification or
a read-through probe), `request` (an on-demand provider call such as a Coach
reply) and `sync` (data import). Run statuses reuse `SyncState`: `running`,
`succeeded`, `failed`, `partial`.

## Policies

| Setting | Value | Notes |
| --- | --- | --- |
| Fresh for | 24 hours | `health_freshness.classify_freshness`: `fresh` |
| Stale after | 72 hours | between the two is `aging`, which is still `ready` |
| In-flight lease | 15 minutes | a `running` row older than this is treated as `failed` with `interrupted` |
| Degrade after | 2 consecutive failed or partial data runs | a non-retryable failure, or any failure once data is no longer `fresh`, degrades immediately |
| Credential errors | `unauthorized`, `forbidden`, `invalid_grant` | only a new or re-authorised credential fixes these |

Per connector: Hevy and Google Health are sync-based (freshness applies); the AI
provider is request-based (`requires_sync` false: configured means `ready`,
failures degrade it, staleness never applies). Hevy offers manual verify and
sync; actions a connector cannot offer are reported as `none`.

## Derivation order

The first matching rule wins. This is the transition table; each row is a test
in `test_integration_state.py`.

| # | Condition | State | Reason | Next action |
| --- | --- | --- | --- | --- |
| 1 | Connector unavailable on this server | `setup_required` | `not_available` | `none` |
| 2 | No credential, authorisation in flight | `connecting` | `authorizing` | `none` |
| 3 | No credential, last authorisation failed or was abandoned | `setup_required` | `authorization_failed` | `connect` |
| 4 | No credential | `setup_required` | `credential_missing` | `connect` |
| 5 | Sync running | `syncing` | `sync_in_progress` | `none` |
| 6 | Authorisation running | `connecting` | `authorizing` | `none` |
| 7 | Verification running | `connecting` | `verifying` | `none` |
| 8 | Credential error not followed by a success, credential had worked before | `degraded` | `credential_rejected` | `reconnect` |
| 9 | Credential error not followed by a success, never worked | `setup_required` | `credential_rejected` | `reconnect` |
| 10 | Latest data run is `partial` | `degraded` | `partial_import` | `sync` |
| 11 | Failing data runs and not absorbed (see Policies) | `degraded` | `provider_failing` | `sync`, `reconnect` or `none` |
| 12 | Request-based connector, no unabsorbed failure | `ready` | `configured` | `none` |
| 13 | No successful sync yet, credential verified | `connecting` | `awaiting_first_sync` | `sync` |
| 14 | No successful sync yet, credential unverified | `connecting` | `awaiting_verification` | `verify` |
| 15 | Last successful sync older than the stale threshold | `stale` | `data_stale` | `sync` |
| 16 | Otherwise | `ready` | `up_to_date` | `none` |

Programme component: no active Hevy programme is `setup_required` /
`programme_missing` (`build_programme`); `needs_review` is `degraded` /
`programme_needs_review` (`review_programme`); `completed` is `stale` /
`programme_completed` (`build_programme`); `active` and `not_started` are
`ready` / `programme_active`.

## Events

| Event | Effect |
| --- | --- |
| Key saved or replaced | Older evidence stops applying; state becomes `connecting` / `awaiting_verification`. |
| Key removed, Google Health disconnected | `setup_required` / `credential_missing`; the connector's run history is deleted. |
| Verify succeeds | Credential proven; `connecting` / `awaiting_first_sync` until a sync succeeds. |
| Verify or read returns 401/403 | `credential_rejected` (rule 8 or 9); one alert. |
| Sync starts | `syncing`; a second start while one is in flight is refused (`409`). |
| Sync succeeds | `ready`; failure streak and credential errors cleared. |
| Sync is partial | `degraded` / `partial_import` until a later `sync` succeeds (a connection test does not clear it). |
| One transient failure while data is fresh | Recorded and absorbed: still `ready`, no alert. |
| Second consecutive failure, or any failure once data is not fresh | `degraded` / `provider_failing`. |
| Worker crashes mid-sync | After the lease the row counts as `interrupted`; state never sticks on `syncing`. |
| Time passes | `ready` becomes `stale` with no event at all. |
| Coach reply fails | Recorded as a `request` run; repeated or non-retryable failures degrade the AI provider. |
| Later success | Failure streak resets; `degraded` returns to `ready` with a "working again" alert. |

## Account aggregation

`derive_account_status` takes the required components in dependency order
(`hevy`, `programme`, `ai_provider`). The account takes the state, reason, message
and next action of the least ready one, ordered `setup_required`, `degraded`,
`connecting`, `stale`, `syncing`, `ready`; ties go to the earlier component.
`blocking` lists required components in `setup_required`, `degraded` or
`connecting`. `google_health` is optional: reported, never blocking.

## Surfaces and API

All responses use `IntegrationStatus.to_payload()` / `AccountStatus.to_payload()`.

| Surface | Where the model appears |
| --- | --- |
| `GET /api/integrations/status` | full account status (Coach page) |
| `GET /api/settings` | `integrations`; `connectors_info[].state` uses the same states; `user_keys.*.has_key`, `gh_connected` and `gh_configured` remain for older clients |
| `GET /api/dashboard`, `GET /api/plan` | `readiness`; `setup_required` is derived from the programme component |
| `GET /api/programmes` | `connection` is the Hevy component plus `username`, `detail` and `load_failed` |
| `POST /api/settings/verify-hevy` | verifies the stored key (no key, empty or `__stored__`) and records it; a different candidate key is probed but never recorded; returns `integration` |
| `POST /api/settings/sync-history` | `integration`; `status` is `ok` or `partial`; `409` while a sync runs; `400` for a rejected credential, `502` for an outage |
| `GET /api/rag_search` | Coach prompt includes a system-generated data-readiness paragraph when Hevy data is missing, failing or stale |
| `POST /api/notifications/test-coach` | never claims Coach is active unless the AI provider is `ready` |

Status payload fields: `connector`, `label`, `state`, `reason`, `message`,
`next_action`, `action_label`, `action_href`, `retryable`, `freshness`,
`last_attempt_at`, `last_success_at`, `last_error_code`,
`consecutive_failures`, `last_run`. Account adds `required`, `blocking` and
`components`.

## Notifications and observability

* An in-app notification is created only when the derived `(state, reason)`
  changes, into `degraded`, into `setup_required` / `credential_rejected`, or back
  to `ready` after one of those. Repeated failures, retries and replayed
  completions never repeat an alert. `stale` is time-derived and has no event.
* Each completed run logs `connector_run` with the privacy-safe labels from
  `health_observability.ConnectorMetric`; each change logs
  `integration_state_transition` with connector, states, reasons and error code.
  Neither includes tenant ids, credentials or provider payloads.

## Adding a connector

1. Add a facts collector in `integration_status._facts_for` (credential presence
   and change time, availability, run history) and a policy.
2. Record every real attempt through `begin_run` / `complete_run` or `record_run`
   with a classified `error_code` (use `connectors.errors.map_http_error`).
3. Render only the shared payload; add the component to the account if it is
   required.
4. Add scenario rows to `test_integration_state.py` and a tenant-isolation test.

## Known limits

* Google Health has no sync pipeline for web users yet, so a connected account
  stays `connecting` / `awaiting_first_sync` instead of claiming data it does not
  have. The roadmap's connector work will record `sync` runs for it.
* The Hevy history rebuild is delete-then-insert, not one transaction. A failure
  part-way is recorded as a `partial` import (`degraded`) and a retry repairs it.
* Stale data is only alerted when something else triggers an alert; it is always
  visible on read.
* Accounts created before this model have no ledger rows, so a saved Hevy key
  reports `connecting` / `awaiting_verification` until the user verifies it, the
  scheduled agent runs, or a sync succeeds. Nothing is changed or deleted, and
  Coach treats existing local history as data of unknown age rather than absent.
* The scheduled daily-plan notification (legacy agent) does not yet carry a
  readiness caveat; the agent only records its Hevy read in the ledger.
* Programme-builder reads, previews and Coach replies write to the ledger from
  `GET`/`POST` handlers that already call a provider; they add telemetry rows only.
