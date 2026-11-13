# Account recovery and verified e-mail changes

Workout Agent signs people in with Google. This page covers the two account
actions that still need a secret delivered out of band: recovering access when
the usual sign-in is unavailable, and moving the account's contact e-mail
address. The implementation is `backend/account_recovery.py`,
`backend/email_delivery.py`, the routes in `backend/webapp/app.py`, and
`frontend/src/app/components/recovery/`.

## Security properties

Both actions use a 256-bit random token (`secrets.token_urlsafe(32)`).

- **Hashed at rest.** The database stores only
  `SHA-256("workout-agent/token/v1/" || token)` in
  `account_action_tokens.token_hash`. The raw token appears only in the message
  sent to the person.
- **Single use.** Consumption is `UPDATE ... WHERE used_at IS NULL` in the same
  `BEGIN IMMEDIATE` transaction that validates expiry and account state. A
  concurrent replay can succeed at most once.
- **Short lived.** Recovery and e-mail change tokens both expire after one hour.
  A new request supersedes any earlier unused token for the same action.
- **Rate limited.** Attempts are counted per subject (hashed address or account
  id) and per source (hashed client bucket) over a one-hour window. The request
  log is written in its own committed transaction, so rejected actions still
  count. Unknown addresses are recorded too, so the limit is not an account
  existence oracle.
- **Non-enumerating.** `POST /api/auth/recovery/request` returns the same `202`
  body for an unknown address, a suspended account, a disallowed address, a rate
  limited request, and a delivery failure. Whether a message was actually issued
  is visible only in the server log and is not attributed to an address.
- **Fail closed.** Recovery requires the account to be `active` and its address
  to pass `ALLOWED_EMAILS`. Completing recovery revokes every existing session
  before the new one is established. A change of contact address requires a
  session authenticated in the last 15 minutes; completing it bumps
  `session_epoch` and revokes every session.
- **Previous-address alerts.** Starting an e-mail change warns the previous
  address immediately. Confirming the change sends a second message to the
  previous address after the change is committed. A change request is discarded
  if either the verification or the warning cannot be delivered.
- **No secrets in logs or audit.** Neither route logs an address, token, SMTP
  credential, or provider error text. Audit events carry only the account id and
  a closed-vocabulary outcome/reason.

## Routes

| Method and path | Auth | Purpose |
|---|---|---|
| `POST /api/auth/recovery/request` | Public | Body `{"email": "..."}`. Always `202 {"status":"accepted"}`. |
| `POST /api/auth/recovery/complete` | Public (token) | Body `{"token": "..."}`. Establishes a new session and returns `{"status":"ok"}`. |
| `POST /api/account/email/change-request` | Session + CSRF | Body `{"new_email": "..."}`. Requires recent authentication; returns `202` and mails the new and previous addresses. |
| `POST /api/account/email/confirm` | Public (token) | Body `{"token": "..."}`. Applies the verified address, revokes sessions, and returns `{"status":"ok","signed_out":true}`. |

The public pages are `/recover` (request, or confirm when `?token=` is present)
and the Account page's e-mail section. E-mail links point at
`<WEB_PUBLIC_URL>/recover?token=...` (or the same URL with
`&kind=email_change` for an address change).

These three POST paths are the only deliberate anonymous mutations in
`backend/webapp/mutation_security.py`. The match is exact, not a prefix, and each
route authenticates with its own single-use token; there is no path-based
bypass for any other mutation.

## Configuration

SMTP is the delivery transport and uses only the standard library.

| Variable | Meaning |
|---|---|
| `WEB_SMTP_HOST` | SMTP server. Blank disables delivery. |
| `WEB_SMTP_PORT` | Port. Default `587`, or `465` when `WEB_SMTP_SSL=1`. |
| `WEB_SMTP_FROM` | Sender address. Blank disables delivery. |
| `WEB_SMTP_USERNAME` / `WEB_SMTP_PASSWORD` | Optional credentials. Never logged. |
| `WEB_SMTP_STARTTLS` | `1`/`true` (default) to upgrade a plaintext connection. |
| `WEB_SMTP_SSL` | `1`/`true` for implicit TLS. |
| `WEB_SMTP_TIMEOUT_SECONDS` | Socket timeout, default `15`. |

`WEB_PUBLIC_URL` must be the canonical HTTPS origin used in the links. If it is
not set, links are built from the validated request origin.

With SMTP unconfigured, requests are still accepted with the generic response,
but no token is created and no message is sent. Operators should alert on the
`SMTP delivery failed` / `Recovery e-mail could not be delivered` log markers.

## Signed-in identity and the account address

The Google identity remains the sign-in key: `resolve_oauth_user` still selects
an account by the provider's immutable `(provider, subject)` pair. Changing the
account's contact address does not re-point or replace that identity. The new
address is the account's recovery/contact address and is verified by the token
before it is stored in `users.email`.

## Deployment notes

- Run the web process with SMTP configured if recovery or e-mail changes should
  be available.
- `WEB_SMTP_PASSWORD` belongs in the deployment secret store, never in a
  committed `.env` file.
- The scheduler is not involved; all actions are synchronous HTTP requests.
- Tokens and request-attempt rows are deleted with the account by the normal
  account-erasure flow.

## Tests

`backend/tests/test_account_recovery.py` covers token hashing, single use,
expiry, replay, enumeration, suspension/deletion, rate limits, delivery failure,
recent-authentication rejection, address conflicts, and previous-address alerts.
`backend/tests/test_account_recovery_routes.py` covers the anonymous HTTP flows
and the session/e-mail change end to end. `frontend/src/app/components/recovery/`
and the Account page have focused component tests.
