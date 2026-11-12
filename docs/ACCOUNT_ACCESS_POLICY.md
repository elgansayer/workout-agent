# Account access policy

Who may sign in to a deployment is decided by the server, not by the UI. This
document describes what is enforced, where, how to configure and operate it, and
what it deliberately does not cover.

Implementation: `backend/account_policy.py` (decisions), `backend/database.py`
(persistence), `backend/webapp/app.py` (`/auth` login completion),
`backend/webapp/auth_boundary.py` (every request), and
`backend/scripts/manage_accounts.py` (operator CLI).

## What is enforced, and when

A signed session cookie proves who signed in earlier. It is not proof that the
person may still be here, so the policy is checked twice.

**Login completion** (`GET /auth`, after Google has authenticated the person).
In order:

1. The identity is usable: a well-formed, ASCII email address and an explicit
   `email_verified` claim from Google. An unverified address is never trusted,
   because an allowlist entry is only meaningful if the claim is.
2. The address is on `ALLOWED_EMAILS`, when one is configured (case-insensitive).
3. If the account exists: its status must be `active`. `suspended`, `deleted`
   and any unrecognised status are refused.
4. If no account exists: with `INVITE_ONLY` enabled, exactly one pending,
   unexpired invitation for that address must exist, and it is consumed in the
   same database transaction that creates the account. Without `INVITE_ONLY`,
   the account is created (open registration).

Only an admitted login receives a session. A refused login receives no session
and creates or consumes nothing.

After an admitted login the browser returns to the page that prompted sign-in
(for example `/onboarding` or `/plan?week=2`), not always the dashboard. The
destination is carried through the OAuth round trip in the signed pre-login
session. Only a same-origin absolute path is accepted: a scheme, authority,
protocol-relative path, backslash, control character or loop endpoint (`/login`,
`/auth`, `/logout`) is replaced with `/`. The value is validated when the OAuth
flow starts and again on callback (`webapp/login_destination.py`).

**Every authenticated request** (`AuthenticationBoundaryMiddleware`, which wraps
every non-public HTTP route, including future ones). The account is re-read from
the database and must still exist, be `active`, still pass the allowlist, and
carry a session epoch equal to the stored one. A session that fails is cleared
and answered exactly like a request that never had a session.

## Configuration

| Variable | Meaning |
|---|---|
| `ALLOWED_EMAILS` | Comma-separated addresses allowed to sign in. Empty or unset means no allowlist. |
| `INVITE_ONLY` | `1`/`true`/`yes`/`on` requires an invitation for new accounts. Default off. Existing active accounts are unaffected. |

Both can be combined; both then apply. Neither has any effect in the
trusted-local anonymous mode, which has no sign-in.

Configuration fails closed. Whether an allowlist exists is decided by whether
`ALLOWED_EMAILS` is set, never by how many entries parsed, so a typo cannot turn
a restricted deployment into an open one. A value with a malformed entry (for
example two addresses separated by a space), or with no usable entry, stops
startup with an error that names the position of the problem and never prints
the configured values. An unrecognised `INVITE_ONLY` value also stops startup.

Addresses must be plain ASCII. They are lower-cased and trimmed, and nothing
else is folded: provider aliasing such as dots or `+tags` is not collapsed, and
there is no Unicode compatibility mapping, because either could make two
different identities compare equal.

## Account state and session revocation

`users` gains three additive columns: `status` (`active` | `suspended` |
`deleted`, default `active`), `status_changed_at`, and `session_epoch` (integer,
default 0). A session stores the epoch it was issued under. A request is valid
only while the cookie's epoch equals the account's stored epoch, so raising the
stored epoch ends every older session at once, with no server-side session list.

The epoch is raised by:

| Event | Effect |
|---|---|
| Any status change (`suspend`, `reactivate`, `delete`), including re-asserting the current status | All earlier sessions end. Reactivating therefore cannot resurrect a session from before the suspension. |
| `revoke-sessions` | Signs the account out of every device; a new sign-in works. |
| Service start with an allowlist configured | Every account outside the allowlist has its sessions ended. |

Removing an address from `ALLOWED_EMAILS` takes effect in two layers. The
request-time allowlist check refuses that account as soon as the service runs
with the new value, and the start-up step makes the revocation permanent, so
adding the address back later does not bring back a cookie issued before the
removal. Environment variables only change when the service restarts, which is
when both layers apply.

`deleted` is terminal and cannot be reactivated. It is an access tombstone: it
blocks sign-in permanently and ends sessions, but it does not erase the
account's data (see "Not covered").

Sessions issued by the release before this one have no epoch and are treated as
epoch 0, so they stay valid until the first revocation. Nobody is signed out by
deploying this change.

## Denials: non-enumerating and logged without secrets

Every login refusal, whatever the reason, is the same response: `303` to
`/login?error=access_denied`, with an empty body. The login page shows one fixed
message for it. A response never says whether an address is allowlisted,
invited, suspended, deleted or unverified, and never echoes any part of the
submitted identity. (The previous callback reflected the email into an
unescaped HTML body.)

A revoked or suspended session on a later request gets what a signed-out
visitor gets: `401 {"detail":"Not authenticated"}` for `/api/*` and a `303` to
`/login` for pages, plus an expired session cookie.

The reason exists only in the server log, one line per denial:

```
access_denied surface=<login|session> reason=<reason> account=<opaque id or -> identity=<tag or ->
```

| `reason` | Meaning |
|---|---|
| `identity_unverified` | Missing/malformed identity, or `email_verified` not affirmatively true |
| `not_allowlisted` | Address is not in `ALLOWED_EMAILS` |
| `not_invited` | `INVITE_ONLY` and no pending, unexpired invitation |
| `account_suspended`, `account_deleted` | Stored status |
| `account_state_invalid` | Stored status is not a recognised value (fail closed) |
| `account_unknown` | Session names an account that does not exist |
| `session_revoked` | Session epoch no longer matches (sign-out everywhere, reactivation, allowlist removal) |

Logs contain no email address, token, cookie or secret. The address is reduced to
a 12-character pseudonymous tag (`data_classification.pseudonymise_identifier`,
a SHA-256 prefix with a fixed domain label) so repeated events can be
correlated; the account id is the opaque UUID and is logged only if it looks
like one, so a hostile identity cannot inject log lines. The tag is
pseudonymisation, not anonymisation: someone who already holds a candidate
address can confirm it, but cannot recover an unknown one. Operators can compute
the tag for an address with `manage_accounts.py status`.

If the policy store cannot be read, requests are refused with `503` and the login
page says sign-in is temporarily unavailable. The session is **not** cleared, so
a database outage does not sign everyone out.

## Operating it

Run the CLI against the web service's database from the application root:

```bash
docker exec -it workout-agent-web python scripts/manage_accounts.py status person@example.com
```

| Command | Effect |
|---|---|
| `status <address>` | Account state, session epoch and invitation history. Never prints the address. |
| `invite <address> [--days N]` | Create a pending invitation (default 7 days, maximum 90). Replaces any earlier pending one. |
| `revoke-invite <address>` | Revoke pending invitations. |
| `suspend <address>` / `reactivate <address>` | Change status and end all earlier sessions. |
| `revoke-sessions <address>` | Sign the account out everywhere without changing its status. |
| `delete <address> --yes` | Permanent access tombstone. Requires confirmation. |

Typical procedures:

- **Invite someone to an invite-only deployment:** set `INVITE_ONLY=1`, restart,
  then `invite person@example.com`. Their first Google sign-in creates the
  account and uses up the invitation; a replayed callback reuses the account.
- **Remove someone from an allowlist:** delete the address from `ALLOWED_EMAILS`
  and restart. Their sessions end and cannot return if the address is re-added.
- **Respond to a suspected stolen session:** `revoke-sessions`, then ask the
  person to sign in again. Use `suspend` if the account itself must be blocked.

## Not covered

These are separate pieces of work and are intentionally not changed here:

- An admin UI or HTTP API for invitations and suspension; invitation rate
  limiting, bulk invites and invitation delivery.
- Pausing scheduled coaching, insight and notification jobs, and connector
  access, for suspended or deleted accounts. Those jobs still run for every
  stored user.
- Erasing an account's data on deletion, and legal-hold states.
- Session cookie attributes, rotation, idle and absolute lifetimes, and a
  user-facing device list.
- A persistent, tamper-evident audit trail. Denials are structured log lines
  only, and the CLI does not record operator actions.
- The CSRF token endpoint validates the cookie signature only. It grants no
  access, since every route behind it re-applies the policy above.

## Tests

```bash
.venv/bin/python -m pytest -q backend/tests/test_account_policy.py \
  backend/tests/test_auth_policy_routes.py backend/tests/test_login_destination.py \
  backend/tests/test_manage_accounts.py backend/tests/test_database.py \
  backend/tests/test_data_classification.py
cd frontend && npm exec -- ng test --watch=false --include='src/app/components/login/login.spec.ts'
```

`test_auth_policy_routes.py` drives the real application: only Google's token
exchange is simulated. It asserts that every denial reason produces a
byte-identical response, that suspension, deletion, revocation and allowlist
changes end a live session on its next request, that denied attempts create no
accounts and spend no invitations, that logs contain no address or secret, and
that malformed configuration stops startup.
