# Account recovery and verified email changes

Workout Agent supports passwordless account recovery and verified changes of
the account email address. Both flows are server-side, tenant-scoped and use
short-lived, single-use tokens stored only as SHA-256 hashes.

## Recovery

1. `POST /api/account/recovery/request` with `{"email": "..."}`.
2. The response is always the same `202 {"status": "accepted"}` whether an
   account exists or not, preventing address enumeration. Requests are rate
   limited by client address and by canonical email address.
3. If an **active** account has a **verified** address, a recovery link is sent
   to that address. The token expires after 15 minutes and any earlier unused
   recovery link for the account is revoked.
4. `POST /api/account/recovery/complete` with `{"token": "..."}` consumes the
   token atomically, rejects replay and expiry, clears the old session and
   issues a fresh authenticated session. Suspended accounts are refused.

Recovery never changes an email address and never relies on a client-supplied
account identifier.

## Verified email changes

1. The user must have a session authenticated within the last 10 minutes.
2. `POST /api/account/email/request` with `{"email": "new@example.com"}` checks
   that the address is valid and free, sends a verification link to the new
   address, and sends a security alert to the current address. If delivery
   fails the pending request is revoked and the endpoint returns `503`.
3. `POST /api/account/email/confirm` with `{"token": "..."}` consumes the
   single-use token (30-minute lifetime), checks that the target address is
   still free, changes the account address, marks it verified, and sends a
   post-change alert to the previous address.

OAuth sign-ins are bound to the provider's stable `sub`, not to the email
address. Changing an address at the provider or in Workout Agent therefore keeps
the same account, while an address already bound to a different provider subject
is refused rather than adopted.

## SMTP configuration

Set the following environment variables for the web service:

| Variable | Purpose |
| --- | --- |
| `SMTP_HOST` | SMTP server host name |
| `SMTP_PORT` | Submission port (default `587`) |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Optional SMTP credentials |
| `SMTP_FROM` | Envelope/header sender address |
| `SMTP_USE_TLS` | STARTTLS toggle (default `1`) |
| `SMTP_USE_SSL` | Implicit TLS toggle (default `0`, usually port `465`) |

Without `SMTP_HOST` and `SMTP_FROM`, no account email is delivered. Recovery
requests continue to return the generic anti-enumeration response, while an
authenticated email-change request fails closed with `503` and is revoked.

## Public surface

The recovery endpoints and the two recovery pages (`/recover`,
`/verify-email`) are the only anonymous additions. They are listed explicitly in
`webapp.auth_boundary.PUBLIC_EXACT_PATHS`; the mutation guard imports
`PUBLIC_MUTATION_PATHS` rather than keeping a duplicate allowlist. Every other
state-changing route still requires a complete signed session.
