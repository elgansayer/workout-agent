# Registration and sign-in

How a person gets an account and signs in, and what happens at each edge. The
rules that decide *who may* are in
[ACCOUNT_ACCESS_POLICY.md](ACCOUNT_ACCESS_POLICY.md); the account and identity
model is in [USER_MODEL.md](USER_MODEL.md). This document is the experience on
top of them.

There is one way in: **Sign in with Google**. There is no separate "register"
form and no password. Registration is the first verified sign-in that the
deployment admits, so account creation and sign-in are one decision made on the
server (`AccessPolicy.admit_login`), never in the browser.

## Registration modes

Set by configuration; the sign-in page describes whichever applies.

| Mode | Configuration | Who gets an account |
|---|---|---|
| `open` | none of the settings below | Anyone with a verified Google address. The first sign-in creates the account. |
| `restricted` | `ALLOWED_EMAILS` and/or `ALLOWED_EMAIL_DOMAINS` | Only listed addresses, or addresses at listed domains (both lists must pass when both are set). |
| `invite_only` | `INVITE_ONLY=1` | Only an address with a pending, unexpired invitation (`manage_accounts.py invite`). The invitation is spent by the sign-in that creates the account. Combine with the allowlists to narrow further. |

`GET /api/auth/config` is the one anonymous endpoint behind this. It returns
`{"google": <bool>, "registration": "open" | "restricted" | "invite_only"}`:
whether Google sign-in is configured and which mode applies. It describes the
deployment only. It never says which addresses, domains or invitations exist, so
it cannot be used to probe for a person. If it cannot be read, the page falls
back to generic wording and still offers the button.

## What happens to each kind of sign-in

Every refusal is the same response to the browser (`303` to
`/login?error=access_denied`, no session, empty body), so the page cannot be used
to learn *why*. The reason exists only in the server log.

| Situation | Outcome | Log reason |
|---|---|---|
| New address, open deployment | Account created, identity bound, signed in. | |
| New address, invite-only, invited | Account created, invitation spent in the same transaction, signed in. | |
| New address, invite-only, not invited (or invitation expired or revoked) | Refused. Nothing created. | `not_invited` |
| Domain not in `ALLOWED_EMAIL_DOMAINS` (including a subdomain or look-alike) | Refused before anything is read or written. | `domain_not_allowed` |
| Address not in `ALLOWED_EMAILS` | Refused. | `not_allowlisted` |
| **Duplicate email**: same address in a different letter case | Same account (addresses are compared lower-cased). Never a second account. | |
| **Duplicate email**: the address belongs to an account bound to a *different* Google account | Refused, nothing changes, no invitation spent. The address may have been reassigned at Google, so it is never handed over. | `identity_conflict` |
| **Provider relinking**: same Google account, new address at Google | Same account. The account's own address is unchanged; the identity's last-seen address is refreshed. No invitation needed. | |
| **Provider relinking**: the person recreated their Google account (new stable subject, same address) | Refused until an operator confirms the person and runs `manage_accounts.py relink <address> --yes`. The next verified sign-in then binds the new Google account to the same account. | `identity_conflict` |
| Account is **suspended** | Refused, whatever address Google reports. | `account_suspended` |
| Account is **deleted** | Refused permanently; the address cannot register again. | `account_deleted` |
| Unverified address, or no stable subject | Refused. | `identity_unverified` |

A refused sign-in never creates an account, binds an identity or spends an
invitation, and a replayed callback signs in to the account it already created.

## Resuming where the person was headed

The destination survives the round trip through Google, so a deep link, a
bookmark or a session that ended mid-task lands back on the same page, including
onboarding steps such as Settings or the programme builder.

1. A page that needs a session sends the visitor to `/login?next=<path>`:
   the server does it for a full page load (`GET /plan?week=2` becomes
   `/login?next=%2Fplan%3Fweek%3D2`), and the app does it when its route guard or
   an API `401` finds the session gone. Only a page navigation is resumed, never
   an API call or a mutation, and the default page adds nothing.
2. The sign-in page's button points at `/login/google?next=<path>`.
3. `GET /login/google` keeps the destination in the signed session cookie
   (never in the OAuth `state` or the redirect URI Google sees). A destination
   that is missing, unsafe or just `/` clears whatever an abandoned attempt
   left behind.
4. After a successful `GET /auth` the person is redirected to it; it is read
   once, before the session is rotated, and is not part of the signed-in session.
   A refused sign-in drops it with the rest of the pre-login state, and the
   person lands on `/login?error=access_denied` as usual.

A destination is only ever a same-origin page path, validated on both ends
(`backend/webapp/login_destination.py` is the authority; the app mirrors it in
`frontend/src/app/services/auth/destination.ts`). A scheme, host, `//` prefix,
backslash, control character, dot segment, non-ASCII character or oversized value
is dropped, percent-encoded forms are checked after decoding, and the sign-in
machinery and APIs (`/login`, `/logout`, `/auth`, `/api`, `/static`, `/assets`,
`/google-health`, health checks) are never a destination. An invalid destination
is not an error: the person lands on the default page. So the sign-in redirect
cannot become an open redirect or loop.

A brand-new account with no destination lands on the dashboard, which shows the
account's own setup state and the next verified setup action (for example
connecting Hevy).

## Operating it

```bash
# Invite someone to an invite-only deployment (then they sign in with Google):
docker exec -it workout-agent-web python scripts/manage_accounts.py invite person@example.com
# See whether their Google account is linked (never prints the Google subject):
docker exec -it workout-agent-web python scripts/manage_accounts.py status person@example.com
# They recreated their Google account and you have confirmed it is them:
docker exec -it workout-agent-web python scripts/manage_accounts.py relink person@example.com --yes
```

## Not covered

- An admin UI or HTTP API for invitations, suspension or relinking, and
  invitation delivery or rate limiting.
- Providers other than Google, and a user-facing "link another provider" flow.
- Changing the address on an account. The account keeps the address it was
  created with; account recovery and verified email change are separate work.
- A guided first-run setup wizard. A new account sees the setup state described
  above.

## Tests

```bash
.venv/bin/python -m pytest -q backend/tests/test_account_policy.py \
  backend/tests/test_accounts.py backend/tests/test_auth_policy_routes.py \
  backend/tests/test_login_destination.py backend/tests/test_manage_accounts.py
cd frontend && npm exec -- ng test --watch=false \
  --include='src/app/components/login/login.spec.ts' \
  --include='src/app/services/auth/*.spec.ts' --include='src/app/services/auth.spec.ts' \
  --include='src/app/interceptors/api.interceptor.spec.ts'
```

`test_auth_policy_routes.py` drives the real application and simulates only
Google. It covers each row of the table above end to end (including that every
refusal is byte-identical), the destination round trip, hostile `next` values,
and the public configuration endpoint.
