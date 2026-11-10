# Account access policy

Who may sign in to a deployment is decided by the server, not by the UI. This
document describes what is enforced, where, how to configure and operate it, and
what it deliberately does not cover.

Implementation: `backend/account_policy.py` (decisions), `backend/accounts.py`
(identity resolution and registration), `backend/database.py` (persistence),
`backend/webapp/app.py` (`/auth` login completion),
`backend/webapp/auth_boundary.py` (every request), and
`backend/scripts/manage_accounts.py` (operator CLI). The sign-in and
registration experience built on top of it is described in
[REGISTRATION_AND_SIGN_IN.md](REGISTRATION_AND_SIGN_IN.md).

## What is enforced, and when

A signed session cookie proves who signed in earlier. It is not proof that the
person may still be here, so the policy is checked twice.

**Login completion** (`GET /auth`, after Google has authenticated the person).
In order:

1. The identity is usable: a well-formed, ASCII email address, an explicit
   `email_verified` claim from Google, and Google's stable subject (`sub`). An
   unverified address is never trusted, because an allowlist entry is only
   meaningful if the claim is, and the subject is what selects the account, so
   a sign-in without one cannot be mapped to anybody.
2. The address passes the address allowlist (`ALLOWED_EMAILS`) and the domain
   allowlist (`ALLOWED_EMAIL_DOMAINS`), for each one that is configured
   (case-insensitive).
3. The identity is resolved to an account, in one database transaction
   (`accounts.admit_oauth_identity`):
   1. The subject is already bound to an account: that account, whatever
      address Google reports now. Its status must be `active`; `suspended`,
      `deleted` and any unrecognised status are refused.
   2. Otherwise an account that holds the address and has no Google identity
      adopts this one (the one-time upgrade path for provisioned accounts). The
      same status rule applies. An address that is already bound to a
      *different* Google account is refused: it may have been reassigned.
   3. Otherwise a new account is registered. With `INVITE_ONLY` enabled,
      exactly one pending, unexpired invitation for that address must exist,
      and it is consumed in the same transaction that creates the account and
      binds the identity. Without `INVITE_ONLY`, registration is open.
4. The account's own recorded address must also pass the allowlists, because
   every later request re-checks it (it differs from the address Google
   reports only after the person changed their Google address).

Only an admitted login receives a session. A refused login receives no session
and creates, binds or consumes nothing: no account, no identity, no spent
invitation.

**Every authenticated request** (`AuthenticationBoundaryMiddleware`, which wraps
every non-public HTTP route, including future ones). The account is re-read from
the database and must still exist, be `active`, still pass the allowlist, and
carry a session epoch equal to the stored one. A session that fails is cleared
and answered exactly like a request that never had a session.

## Configuration

| Variable | Meaning |
|---|---|
| `ALLOWED_EMAILS` | Comma-separated addresses allowed to sign in. Empty or unset means no allowlist. |
| `ALLOWED_EMAIL_DOMAINS` | Comma-separated bare domains (`example.com`) whose addresses may sign in. Empty or unset means no domain rule. A domain matches exactly: `mail.example.com` is another domain and must be listed itself. |
| `INVITE_ONLY` | `1`/`true`/`yes`/`on` requires an invitation for new accounts. Default off. Existing active accounts are unaffected. |

All three can be combined; every configured rule then applies, so an address
allowlist together with a domain allowlist admits only the listed addresses at
the listed domains. None has any effect in the trusted-local anonymous mode,
which has no sign-in.

The domain rule is evaluated on the address Google has verified, the same trust
as `ALLOWED_EMAILS`. It is not a Google Workspace membership check (the `hd`
claim): use it to say which mail domains this deployment is for, not to prove
who currently belongs to an organisation.

Configuration fails closed. Whether an allowlist exists is decided by whether
`ALLOWED_EMAILS` (or `ALLOWED_EMAIL_DOMAINS`) is set, never by how many entries
parsed, so a typo cannot turn a restricted deployment into an open one. A value
with a malformed entry (for example two addresses separated by a space, or a
wildcard or `@` in a domain), or with no usable entry, stops startup with an
error that names the position of the problem and never prints the configured
values. An unrecognised `INVITE_ONLY` value also stops startup.

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

Removing an address from `ALLOWED_EMAILS` (or a domain from
`ALLOWED_EMAIL_DOMAINS`) takes effect in two layers. The request-time check
refuses that account as soon as the service runs with the new value, and the
start-up step makes the revocation permanent, so adding it back later does not
bring back a cookie issued before the removal. Environment variables only change when the service restarts, which is
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
| `identity_unverified` | Missing/malformed identity, no usable stable subject, or `email_verified` not affirmatively true |
| `not_allowlisted` | Address is not in `ALLOWED_EMAILS` |
| `domain_not_allowed` | The address's domain is not in `ALLOWED_EMAIL_DOMAINS` |
| `not_invited` | `INVITE_ONLY` and no pending, unexpired invitation |
| `identity_conflict` | The address belongs to an account bound to a different Google account, or matches more than one account |
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
| `relink <address> --yes` | Unbind the account's Google identity and end its sessions, so the next verified Google sign-in for the address binds a new Google account. Requires confirmation. |
| `delete <address> --yes` | Permanent access tombstone. Requires confirmation. |

Typical procedures:

- **Invite someone to an invite-only deployment:** set `INVITE_ONLY=1`, restart,
  then `invite person@example.com`. Their first Google sign-in creates the
  account and uses up the invitation; a replayed callback reuses the account.
- **Remove someone from an allowlist:** delete the address from `ALLOWED_EMAILS`
  and restart. Their sessions end and cannot return if the address is re-added.
- **Move a person to a recreated Google account:** sign-in refuses an address
  that is already bound to a different Google account (it may have been
  reassigned), so the person is locked out until you have confirmed it is them
  and run `relink <address> --yes`. Their next sign-in binds the new Google
  account; the account and its data are unchanged.
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
  backend/tests/test_accounts.py backend/tests/test_auth_policy_routes.py \
  backend/tests/test_login_destination.py backend/tests/test_manage_accounts.py \
  backend/tests/test_database.py backend/tests/test_data_classification.py
cd frontend && npm exec -- ng test --watch=false \
  --include='src/app/components/login/login.spec.ts' \
  --include='src/app/services/auth/*.spec.ts' --include='src/app/services/auth.spec.ts' \
  --include='src/app/interceptors/api.interceptor.spec.ts'
```

`test_auth_policy_routes.py` drives the real application: only Google's token
exchange is simulated. It asserts that every denial reason produces a
byte-identical response, that suspension, deletion, revocation and allowlist
changes end a live session on its next request, that denied attempts create no
accounts, bind no identities and spend no invitations, that logs contain no
address or secret, and that malformed configuration stops startup.
