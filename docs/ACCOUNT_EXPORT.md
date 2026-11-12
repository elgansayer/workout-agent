# Account data export

A signed-in user can request a complete, portable copy of the data Workout Agent
holds about them from **Settings → Account and data**. The archive is a ZIP of
JSON files with a manifest, built asynchronously by the scheduler, offered only to
its owner, and deleted automatically when it expires. The implementation is
`backend/account_export.py`; the HTTP surface is in `backend/webapp/app.py` and
the panel is `frontend/src/app/components/settings/account-export.ts`.

## What the user sees

| Step | Behaviour |
| --- | --- |
| Request | `POST /api/account/exports` queues one export. If one is already queued or running, the same request is returned with `200` instead of creating a duplicate (`202` when newly queued). |
| Follow | The panel polls `GET /api/account/exports` while the state is `queued` or `running`, then shows the size, record count, SHA-256 and expiry. |
| Download | `GET /api/account/exports/{export_id}/download` streams the ZIP only while the export is `ready` and unexpired. Other accounts' ids return the same `404` as unknown ids. |
| Expiry | The archive and its ledger row are deleted after `ACCOUNT_EXPORT_RETENTION_HOURS`; the panel then offers a new request. |

Exports are deliberately not emailed or pushed: the archive contains health data,
so it is only ever served from the authenticated download route.

## Lifecycle

```text
requested ─▶ queued ─▶ running ─▶ ready ─▶ expired
                │          │
                │          └─▶ failed (retry after back-off, then permanent)
                └────────────▶ superseded by a newer request
```

* **One open request per account.** A partial unique index allows only one
  `queued` or `running` row for a user.
* **Built off the request path.** The web process only queues and serves exports.
  The scheduler process runs `scheduler.run_data_exports()` on every tick, which
  claims due requests atomically, renews a 15-minute lease while building, and
  retries a failed attempt after five minutes up to three times.
* **Consistent snapshot.** Every build reads one SQLite read snapshot, in batches
  of at most 500 rows, so a concurrent write cannot produce a half-updated
  account.
* **Bounded.** `ACCOUNT_EXPORT_MAX_MB` caps uncompressed content. Exceeding it
  fails the request with `too_large` rather than publishing a partial archive.
* **Publish after re-check.** The archive is written to a private `.partial` file
  and renamed into place only inside the database write lock, after re-checking
  that the request still belongs to the owner and the account is not being
  erased.
* **Crash-safe.** A crashed worker's lease expires and another attempt resumes.
  A superseded attempt cannot publish or settle the row.
* **Erased with the account.** Closing the account removes the export ledger and
  the archive directory. Erasing health data also removes retained archives
  because they embed that data.

## Archive layout

The ZIP root is `workout-agent-export/`. It contains `manifest.json`, `README.txt`
and one JSON file per section. Each JSON file is:

```json
{
  "schema_version": 1,
  "section": "workouts",
  "description": "Workouts, exercise progress and Hevy routines",
  "derived": false,
  "records": [],
  "record_count": 0
}
```

The manifest records the format and schema versions, account id, preferences, file
list with record counts, sizes and SHA-256 digests, total counts, the exclusions
below, and any table that owns rows but has not yet been reviewed for export. A
reviewed table is never silently dropped: adding an account-owned table without a
decision makes it appear under `unreviewed_tables` and logs a warning.

### Files

| Category | File | Contents |
| --- | --- | --- |
| Profile | `profile/account.json` | Identity, regional and accessibility settings |
| Profile | `profile/sign_in_methods.json` | Linked identity providers without subject ids |
| Profile | `profile/coaching_profile.json` | Goals, constraints and coaching preferences |
| Profile | `profile/training_rules.json` | Training rules and safety constraints |
| Profile | `profile/onboarding.json` | Onboarding progress and answers |
| Profile | `profile/access_invitations.json` | Invitations and beta access records for the owner |
| Programmes | `programmes/programmes.json` | Programmes and their versions |
| Programmes | `programmes/state.json` | Active programme state |
| Programmes | `programmes/revisions.json` | Programme revisions |
| Programmes | `programmes/routines.json` | Programme routines |
| Programmes | `programmes/blocks.json` | Programme blocks |
| Programmes | `programmes/prescriptions.json` | Exercise prescriptions |
| Programmes | `programmes/week_states.json` | Per-week programme state |
| Programmes | `programmes/adaptation_decisions.json` | Adaptation proposals and outcomes |
| Programmes | `programmes/decisions.json` | Programme decisions |
| Workouts | `workouts/workouts.json` | Workout history |
| Workouts | `workouts/exercise_progress.json` | Per-exercise progress |
| Workouts | `workouts/routines.json` | Hevy routines |
| Workouts | `workouts/routine_snapshots.json` | Imported routine snapshots |
| Workouts | `workouts/routine_matches.json` | Routine matching results |
| Workouts | `workouts/routine_match_overrides.json` | User overrides of routine matches |
| Workouts | `workouts/exercise_baselines.json` | Exercise baselines |
| Metrics | `metrics/body_metrics.json` | Body measurements |
| Metrics | `metrics/health_metrics.json` | Imported health metrics |
| Check-ins | `check_ins/check_ins.json` | Check-in responses |
| Connectors | `connectors/hevy_connection.json` | Hevy connection metadata (no key) |
| Connectors | `connectors/credentials.json` | Credential metadata (provider, status, dates; never secrets) |
| Connectors | `connectors/health_connections.json` | Health connector metadata (no tokens) |
| Connectors | `connectors/health_sync_runs.json` | Health sync run history and outcomes |
| Connectors | `connectors/state.json` | Connector state and last-sync information |
| Notifications | `notifications/notifications.json` | Notifications shown to the owner |
| Notifications | `notifications/push_subscriptions.json` | Push subscription metadata (never endpoints or keys) |
| Coaching | `coaching/chat_messages.json` | Coach conversation history |
| Coaching | `coaching/daily_plans.json` | Daily plans |
| Coaching | `coaching/insights.json` | Generated insights |
| Coaching | `coaching/correlations.json` | Deep correlation results |
| Coaching | `coaching/reasoning.json` | Reasoning logs |
| Audit | `audit/security_events.json` | The owner's user-facing security events |
| Audit | `audit/credential_events.json` | Credential activity for the owner |
| Audit | `audit/deletion_requests.json` | The owner's deletion requests |
| Provenance | `provenance/record_provenance.json` | Full correction history from the provenance ledger |

Derived and imported records carry a `provenance` object with the contract
version, kind, source, source label, transformation version, ingestion time and
confidence. A `null` provenance means the source was not recorded when the value
was stored; it is never invented. The provenance ledger keeps corrections and the
values they replaced.

## Excluded data

The archive is allow-listed: every exported table names the columns it releases.
A new column is withheld until someone reviews it, and credential-class columns
can never be listed. These categories are withheld deliberately and are named in
the manifest so the archive never claims more than it holds:

* **`credentials`** — API keys, OAuth and refresh tokens, client secrets,
  credential hints, and web-push endpoints and keys. Only non-secret metadata such
  as provider, status and dates is exported.
* **`sessions`** — the browser-session registry and anti-forgery tokens (internal
  security data).
* **`audit_internals`** — hash-chain digests, sequence numbers, key identifiers,
  client-address pseudonyms, and system alerts raised for operators.
* **`identity_provider_ids`** — the sign-in provider's stable subject identifiers.
* **`other_accounts`** — records that belong to other accounts, including
  operator identities.
* **`operational_ledgers`** — job, deletion-evidence and ownership-migration
  ledgers kept by the service itself.

JSON blobs are additionally scrubbed of credential-class keys before being
written, as defence in depth.

### Tables that are never exported

These tables carry an account reference but are excluded as a whole:

* **`user_sessions`** — the session registry; rows hold one-way digests of
  session identifiers (credential class).
* **`csrf_tokens`** — single-use anti-forgery nonces (internal security data).
* **`account_tombstones`** — deletion evidence that exists only after an account
  is closed, never for an account that can still request an export.
* **`legacy_ownership_migrations`** — an operator-run ownership assignment ledger
  that names the operator.
* **`account_export_requests`** — this feature's own job ledger; each delivery is
  audited as `data.exported` instead.

## HTTP API

All routes require a signed-in session. The owner is always taken from the
authenticated session, never from a body, header or query parameter. Responses
carry `Cache-Control: private, no-store`; the download is an attachment.

| Route | Purpose |
| --- | --- |
| `GET /api/account/exports` | Eligibility, retention and daily limit, what is included and excluded, and the owner's recent exports. |
| `POST /api/account/exports` | Queue an export. `202` when newly queued, `200` when an open request is returned. Requires the usual CSRF token. |
| `GET /api/account/exports/{export_id}` | One export's status; identical `404` for unknown, malformed and other accounts' ids. |
| `GET /api/account/exports/{export_id}/download` | Stream the ZIP while it is ready and unexpired. |

Errors use `{"detail": "<code>"}`: `403 not_eligible`, `404 not_found`,
`409 not_ready`, `410 expired`, `423` while the account is being erased, `429`
rate limit, and `401` when unauthenticated or closed. A failed build reports a
non-sensitive `error_code` (`too_large`, `timed_out`, `filesystem_error`,
`database_error`, `internal_error`, `not_eligible`) on the status payload.

Each successful delivery is recorded as a `data.exported` security audit event
with the format version and size, never the archive contents.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ACCOUNT_EXPORT_RETENTION_HOURS` | `24` (1–168) | How long a finished export stays downloadable before it is deleted. |
| `ACCOUNT_EXPORT_MAX_MB` | `512` (1–4096) | Largest uncompressed export. Larger accounts get `too_large`. |
| `ACCOUNT_EXPORT_DAILY_LIMIT` | `5` (1–50) | Exports one account may request in any rolling 24 hours. |

Out-of-range values are clamped and logged; non-numeric values fall back to the
default. The web and scheduler processes must share the data volume because
archives live under `<database directory>/exports/<user id>/`.

## Tenant and safety guarantees

* Every query is scoped by the authenticated or persisted owner id; a
  client-supplied owner id is ignored.
* The worker re-authorizes the owner through `tenant_isolation.run_owned_job`
  immediately before building.
* Archives are stored owner-only (`0600`) under the deletion-managed exports
  directory, published atomically, and never served after expiry or tampering.
* A database without the export table is left untouched, so older deployments
  degrade safely.
* The shared local tenant used when web authentication is off cannot export.

## Tests

Deterministic coverage lives in `backend/tests/test_account_export.py` (archive
contents, exclusions, provenance, retention, leases, deletion integration) and
`backend/tests/test_account_export_api.py` (authentication, ownership, routing,
background build, download and expiry end to end). Frontend behaviour is covered
by `frontend/src/app/components/settings/account-export.spec.ts`.
