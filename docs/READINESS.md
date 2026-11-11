# Account readiness and Coach capability

Every surface that can legitimately be empty (Home, Progress, Stats, History,
Check-ins, Plan, Coach and Settings) renders one server-derived contract that
says *why* data is missing or incomplete and which in-product step comes next.
Nothing is assumed: each state is reproducibly derived from records the signed-in
user owns, and Coach never claims more than has been verified for that user.

## The six account states

`backend/setup_state.py` (`SetupStateKind`) derives exactly one state per tenant.
Precedence, most specific first:

| State | Meaning | Derived from |
| --- | --- | --- |
| `connecting` | A Hevy connection check is running right now. | Live operation lease, phase `connecting`. |
| `syncing` | A Hevy history sync is running right now; imported history is being rebuilt. | Live operation lease, phase `syncing`. |
| `degraded` | The pipeline is failing and needs repair. | A stalled lease (`operation_stalled`); a removed key with data still present (`credential_removed`); Hevy rejecting the key (`credential_rejected`, `credential_insufficient_scope`); a check that could not reach Hevy (`connection_unverified`, cleared by a later successful sync); a failed last sync (`sync_failed`). |
| `setup_required` | A step is still missing: Hevy key, programme, or imported history. `first_run` is true when nothing at all is configured. | Stored credential, active programme, imported data. |
| `stale` | Nothing has happened for more than seven days: no training activity **and** no successful sync. A recent successful sync of a quiet week is *not* stale. | Latest training activity and last successful sync. |
| `ready` | Connected, set up and current. | Everything above is clear. |

A live operation outranks every other state because the data a surface would show
is changing under it. A surface whose own requirements are all met is `ready` even
while setup is open elsewhere (for example Progress without a programme).

### Persisted connector state

`backend/connector_operations.py` records what cannot be derived from imported rows
in the tenant-scoped `hevy_meta` store (`connector:hevy:operation`,
`connector:hevy:last_sync`). No new table: account deletion and ownership
migration already cover `hevy_meta`.

* **Operation lease.** The connect and sync routes hold a per-tenant lease for the
  duration of the work (`BEGIN IMMEDIATE`, so two workers cannot both take it). A
  second sync or check is refused with `409` while one runs, which also stops two
  history rebuilds from interleaving. The lease expires after 15 minutes; an expired
  lease is reported as `degraded` (`operation_stalled`), never as syncing forever.
* **Last sync outcome.** `succeeded` (with the number of workouts Hevy returned) or
  `failed` with a fixed code such as `hevy_unavailable`. Provider error text and
  credentials are never stored. Saving a newly verified Hevy key clears a failure
  recorded for the previous key.
* The sync runs off the event loop, so a long sync never stalls other tenants and
  the `syncing` state is observable while it runs.

A failed Hevy fetch is an error, not an empty success: `scripts/sync_history.py`
returns `error_code: hevy_unavailable` and leaves existing history untouched
instead of reporting "synced 0 of 0 workouts".

## Coach: verified facts only

`backend/coach_capability.py` derives what Coach may claim. There are two facts, and
`status: ready` requires **both**:

* **A working provider.** A stored key is configuration, not proof. The provider is
  `verified` only when the last connection test of the account's own stored key
  passed (`user_api_keys.last_test_status = "ok"`; rotating the key clears it).
  States: `missing_key`, `unsupported`, `unverified` (never tested, or the shared
  server Gemini key, which carries no per-account evidence), `verified`, `failed`.
  An account on the shared key is never claimed verified, but is not sent to test a
  key it does not own: the navigation and setup notice only ask for a test when the
  account has its own stored key.
* **Access to data.** `verified_data` means this account has training data of its
  own and it is not being rebuilt by a running sync.

| Status | Condition | `can_chat` |
| --- | --- | --- |
| `ready` | Provider verified, current data, healthy connection. | yes |
| `provider_unverified` | Everything else fine, but the key was never tested. | yes (never claimed to work) |
| `provider_failed` | The last connection test failed. | **no** |
| `syncing` | A sync is rebuilding history. | yes, data-grounded questions refused |
| `degraded` | Data exists but the connection needs attention. | yes, disclosed to the model |
| `stale_data`, `no_data` | Data is old or absent. | yes |
| `provider_missing`, `unconfigured` | No usable provider. | **no** |

### Server enforcement matches the UI

`/api/rag_search` consults the same capability state the UI renders:

* `can_chat` false returns `409` with `{"detail": {"code": "<status>", "message": "…"}}`
  before anything is saved or any model is called.
* A data-grounded question while history is being rebuilt, or without the data it
  needs, is answered with the fixed "Data-Grounded Coaching Unavailable"
  explanation instead of calling the model (`get_coaching_capabilities`).
* The prompt carries only the sources verified for this user plus the readiness, and
  tells the model to say so when a sync is running or the connection is degraded.

Other server-side guards that match the UI: a sync, key check or provider test is
refused while one is already running (`409`); `sync-history` without a stored key is
`400`; saving a Hevy key verifies it first and never stores a rejected one; the
test notification no longer claims "Coach analysis active".

## API contract

`GET /api/setup/state` returns `{account, surfaces}`. `account` carries `state`,
`label`, `reason`, `reason_code`, `first_run`, `checks`, `missing`, `stale`,
`connection_state`, `ai_provider_state` and `connector` (`operation`,
`operation_stalled`, `last_sync`). Each surface carries `state`, `label`, `reason`,
`reason_code`, `first_run`, `action` (`id`, `label`, `href`, `description`) and
`missing`. Every page response embeds the surface contract as `setup`.

`GET /api/coach/capability` returns `provider_state`, `provider_verified`,
`provider_source` (`user`, `server`, `none`), `readiness`, `verified_data`,
`can_chat`, `status` and the per-source freshness.

## Frontend

* `SetupNotice` renders the contract: a first-run welcome, a polite `status` with
  `aria-busy` while connecting or syncing (no action that would collide with it), an
  `alert` with the repair for `degraded`, and a status for `stale`.
* Data is shown beside a notice (`ViewState = attention`) rather than hidden or
  presented as if all were well; a failed request is a retryable alert, distinct from
  an empty account.
* Navigation badges read `Setup`, `Connecting`, `Syncing`, `Stale` or `Attention`
  and are part of each link's accessible name. Settings carries the account-level
  badge.
* Settings shows an Account status panel (Hevy, last sync, AI provider), disables Sync
  while any session is syncing, and labels each stored AI key `Verified`, `Not tested`
  or `Test failed` from its last connection test.
* Coach never says `Ready` unless both facts are verified, pauses on a failed
  provider test, withholds data-grounded questions during a sync, and can test the
  stored key in place.

## Tests

* `backend/tests/test_setup_state.py`, `test_connector_operations.py`,
  `test_coach_capability.py`, `test_coaching_capabilities.py`, `test_sync_history.py`
* Route and end-to-end: `test_webapp_readiness.py` (live syncing/connecting through
  the real routes, failure and recovery, tenant isolation, Coach enforcement) and
  `test_webapp_setup_states.py`.
* Frontend: `surface-readiness.spec.ts` (every surface × every state),
  `setup-notice.spec.ts`, `setup-state.spec.ts`, `settings.spec.ts`,
  `settings.readiness.spec.ts`, `chat.spec.ts`, `app.spec.ts`, and the Playwright
  journeys in `frontend/e2e/ui.spec.ts`.
