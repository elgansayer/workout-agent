# Hevy credential verification

**Status:** Implemented  
**Canonical delivery issue:** #814

## 1. Problem

The settings form accepted any string as a Hevy API key. Nothing proved that the
key identified an account, carried the access the product needs, or was even
reachable. An invalid or revoked key was stored and silently activated: every
later sync failed while the settings page still showed "Connected".

## 2. The gate

A Hevy credential is verified **before it is stored or activated**.
`backend/hevy_connection.py::verify_hevy_credentials()` performs a small,
bounded set of requests against the Hevy API and returns safe metadata only.

| Step | Request | Purpose | Failure meaning |
| --- | --- | --- | --- |
| 1 | `GET /v1/user/info` | prove the key is accepted and identify the connected account | `401`/`403` → invalid key |
| 2 | `GET /v1/workouts?page=1&pageSize=1` | prove the minimum access the product needs (read one logged workout) | `401` → invalid key, `403` → insufficient scope |
| 3 | `GET /v1/routines?page=1&pageSize=1` | optional capability | recorded as unavailable, never fatal |
| 4 | `GET /v1/exercise_templates?page=1&pageSize=1` | optional capability | recorded as unavailable, never fatal |
| 5 | `GET /v1/workouts/count` | optional capability and workout count | recorded as unavailable, never fatal |

The check is bounded on purpose:

- at most five requests, one page of at most one item each;
- a ten second timeout per request (`CREDENTIAL_TEST_TIMEOUT`);
- no pagination, no history download and no write operations.

### Outcomes

| State | Meaning | HTTP status when surfaced by the API |
| --- | --- | --- |
| `connected` | the key identifies an account and can read workouts | `200` |
| `invalid` | Hevy refused the credential, or the key is unusable | `400` |
| `insufficient_scope` | the account cannot read workouts | `400` |
| `unverified` | Hevy could not be reached or answered unusably | `502` |

`unverified` is deliberately distinct from `invalid`: a transient Hevy outage
must not be reported to the user as a bad credential, and must not activate an
unproven key either. Public behaviour fails closed — an unproven credential is
never activated.

## 3. What is stored

`hevy_connections` holds the verification result per tenant:

| Column | Content |
| --- | --- |
| `user_id` | owning tenant (foreign key to `users(id)`, indexed first) |
| `provider` | `hevy` |
| `state` | `connected`, `invalid`, `insufficient_scope` or `unverified` |
| `account_username`, `account_id` | connected account identity |
| `api_version` | API version reported by Hevy |
| `capabilities_json` | verified capability map (`workouts`, `routines`, `exercise_templates`, `workout_count`) |
| `tested_at` | time of the verification |

The table contains **no credential material**. The API key itself is stored
only in `user_api_keys`, encrypted with the deployment's `ENCRYPTION_KEY`.

## 4. API surface

| Route | Behaviour |
| --- | --- |
| `POST /api/settings/key` (provider `hevy`) | verifies the supplied key first; an unusable key is rejected with `400`/`502` and is **not** stored; a verified key is stored encrypted and the connection metadata is recorded |
| `POST /api/settings/verify-hevy` | verifies the supplied `api_key`, or the stored key when the body is empty; a tested key is not stored. A verified stored key records its metadata, and a broken stored key is marked `invalid`/`unverified` |
| `GET /api/settings` | returns `hevy_connection` (state, account, API version, capabilities, test time) next to the masked key information |
| `POST /api/settings/key/delete` | removes the stored key and the connection metadata |

AI provider keys are unaffected: they are stored without a provider round-trip.

## 5. Safety properties

- **Tenant scoped.** The connection record is written and read with the
  authenticated session identity. A client-supplied owner identifier is never
  used to choose the record owner.
- **No echo.** Responses contain the connection metadata, never the key. The
  key is transmitted to Hevy only as a request header.
- **No redirects.** The credential check never follows a redirect. The HTTP
  transport keeps custom headers such as `api-key` when it follows a redirect
  to another host, so a redirect answer would hand the key to a host the user
  never chose. A redirect is reported as "could not verify" and the credential
  is not activated.
- **No logging.** The client logs the request path and the HTTP status only.
  The credential is never written to logs, notifications or error messages.
  Transport errors that quote the offending header value are redacted before
  they are logged.
- **No unnecessary retention.** Only a *verified* credential is stored, and it
  is stored encrypted. The Angular form clears the typed key after a successful
  save; a key that was only tested stays in the form so it can be saved without
  being pasted again.
- **Truthful state.** A stored key that stops working is recorded as
  `invalid`/`unverified`; the settings page never claims a live connection it
  could not prove.
- **Unusable keys are refused locally.** A key that cannot be placed in an
  HTTP header (control characters, non-ASCII bytes, excessive length) can
  never authenticate, so it is rejected before any request is made and is
  never put on the wire.

## 6. Operational notes

- The verification runs inside the request that saves or checks the key. The
  settings routes are rate limited; a rejected verification is cheap because
  the check fails fast on the first refused request.
- `ENCRYPTION_KEY` must be configured for at-rest protection of stored
  credentials. Without it the deployment stores keys in plaintext and logs a
  warning — the verification gate does not change that behaviour.
- A `502` response means Hevy was unreachable. The user should retry; the
  credential was neither approved nor rejected.

## 7. Verification

```bash
.venv/bin/python -m pytest -q backend/tests/test_hevy_connection.py
.venv/bin/python -m pytest -q backend/tests/test_hevy_settings_routes.py
.venv/bin/python -m pytest -q backend/tests/test_hevy_client.py
```

The frontend behaviour is covered by
`frontend/src/app/components/settings/settings.spec.ts`.
