# Consent and permission ledger

Purpose-specific consent is stored in an append-only, tenant-scoped ledger
(`consent_decisions`) and enforced by the feature boundaries that process or
deliver data. The code lives in `backend/consent_ledger.py` (decision store) and
`backend/consent_service.py` (withdrawal workflow).

## What a decision records

Every row is immutable and identifies:

| Field | Meaning |
| --- | --- |
| `user_id` | The authenticated account that owns the decision. Never taken from a request body. |
| `purpose` | `health_data`, `ai_processing`, `notifications` or `analytics`. |
| `scope` | `*` for the whole purpose (connector-specific scopes can be added without a schema change). |
| `policy_version` | Version of the purpose policy the user agreed to. |
| `scopes_json` | The granted capabilities, such as sleep, body, push or generate_coaching. |
| `decision` | `granted` or `withdrawn`. |
| `actor_type` / `actor_id` | `user` (the signed-in account), `operator` or `system`. |
| `decided_at` / `revoked_at` | UTC decision time and, for a withdrawal, the revocation time. |
| `source` | `web`, `oauth_callback` or a migration marker. |

The table has a tenant-first index and database triggers reject `UPDATE` and
`DELETE`. Granting and withdrawing append a new row; the current state is the
newest row for the purpose. A decision made under a different policy version
does not count as current consent and requires the user to choose again.

Existing explicit opt-ins are migrated once as `system`-sourced grants:
connected health providers, saved AI provider keys and push subscriptions. This
preserves an earlier user choice without inventing consent for a feature the
account never enabled.

## Enforcement

* **Health data**: the agent only resolves a Google Health token or imports an
  Android Health Connect file when current `health_data` consent exists. A
  successful Google OAuth callback records the grant.
* **AI processing**: web AI routes, the daily agent and the insight jobs check
  `ai_processing` consent before resolving a provider or generating text. Saving
  an AI provider key records a grant. Without consent the daily agent uses its
  deterministic plan and the scheduled insight jobs skip generation.
* **Notifications**: saves and push delivery require `notifications` consent.
  Subscribing records a grant.
* **Analytics**: a closed purpose exists for product analytics. This deployment
  currently sends no analytics data to a third party; any future analytics
  pipeline must call the same check before processing.

The checks fail closed: an absent, withdrawn or stale record denies the
feature. A user who has not granted a purpose can still use the rest of the app.

## Withdrawal workflow

Consent withdrawal is a `POST /api/consent/withdraw` request with a purpose and,
for health data, a retention choice:

* `health_data` + `retain`: stop future imports and revoke connector credentials,
  but keep records already imported.
* `health_data` + `purge`: stop future imports, revoke credentials and run the
  existing provider-scoped purge for all health providers. Imported metrics,
  sync runs, connection rows and derived insights are removed; user-entered
  workouts and other records are not touched.
* `notifications`: remove every push subscription and stop new notification
  delivery.
* `ai_processing` / `analytics`: stop future processing; stored coaching history
  or records are not deleted by this action.

The decision and the reason are also written to the security audit trail as a
`permission.changed` event (metadata only, no secrets). The ledger is retained
on account closure as compliance evidence, alongside the security audit trail,
and contains no health, workout or credential payload. Account and health-data
deletion remain separate, stronger workflows described in
[`ACCOUNT_DELETION.md`](ACCOUNT_DELETION.md).

## HTTP surface

| Route | Purpose |
| --- | --- |
| `GET /api/consent` | Current state for every purpose plus the health retention options. |
| `POST /api/consent/grant` | Record an explicit grant. |
| `POST /api/consent/withdraw` | Record a withdrawal and apply its workflow. |
| `POST /api/settings/push-unsubscribe` | Withdraw notification consent and remove all push subscriptions. |

Mutations use the normal authenticated session and CSRF boundary. The settings
UI exposes these choices under **Settings → Privacy and consent**.
