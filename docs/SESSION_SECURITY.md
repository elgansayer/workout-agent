# Session security

Workout Agent signs users in with Google and keeps them signed in with a browser
cookie. This page is the contract for that cookie: how it is delivered, how long
it lives, when its identifier changes, and how it is revoked. The implementation
is `backend/webapp/session_security.py`; the CSRF boundary
([`CSRF_SECURITY.md`](CSRF_SECURITY.md)) and the public-origin boundary
([`REVERSE_PROXY_SECURITY.md`](REVERSE_PROXY_SECURITY.md)) build on it.

## Why a registry exists

A signed cookie on its own is a bearer token. It cannot be revoked, it never goes
idle, and a copy lifts exactly as much access as the original until its signature
ages out. Logging out only asked the browser to forget the cookie, so a cookie
that had already been copied (a shared computer, a leaked backup or log, malware)
kept working.

Every authenticated cookie therefore carries a random session identifier
(`sid`), and the server keeps a registry (`user_sessions`) of which identifiers
it still honours. **A cookie is only believed while the registry says its session
is active and owned by the user the cookie claims.** The signature proves who
issued the cookie; the registry proves the server still stands behind it.

## Cookie policy

| Attribute | Value | Reason |
| --- | --- | --- |
| Name | `__Host-session` when `Secure`, `session` otherwise | The `__Host-` prefix makes browsers *enforce* `Secure`, `Path=/` and host-only, and refuse a cookie that a sibling subdomain tries to set or overwrite. Plain HTTP development keeps the unprefixed name because browsers reject the prefix without `Secure`. |
| `HttpOnly` | always | Page script, including injected script, cannot read the session. |
| `Secure` | always in production; elsewhere when `WEB_PUBLIC_URL` is `https://` | Never sent in clear text. Production cannot be downgraded by configuration. |
| `SameSite` | `Lax` | The Google sign-in callback is a cross-site top-level navigation that must carry the OAuth state stored in the session; `Strict` would drop it and break sign-in. State-changing requests are separately protected by single-use CSRF tokens and `Origin` checks. |
| `Path` | `/` | The application owns its whole origin and every SPA route (`/plan`, `/settings`, `/chat`…) is gated by the server, so the cookie must accompany all of them; no narrower path works. `Path` is not a security boundary against same-origin script anyway, so scoping is achieved with the attributes below instead. |
| `Domain` | never set (host-only) | The cookie is returned to the exact host that issued it and is never shared with sibling subdomains. The `__Host-` prefix makes the browser enforce this. |
| `Max-Age` | the absolute lifetime | A best-effort client-side bound; the server enforces the real limits (below). |

There is deliberately no setting for `SameSite`, `Path` or `Domain`: each one is
a way to weaken the cookie, and none is needed for the supported deployment (one
canonical HTTPS origin, see `WEB_PUBLIC_URL`). Responses that set or read the
session are `Cache-Control: private, no-store` and `Vary: Cookie` through the
existing cache guard.

## Lifetime limits

| Variable | Default | Meaning |
| --- | --- | --- |
| `SESSION_IDLE_TIMEOUT_SECONDS` | `259200` (3 days) | A session ends after this long without a request. Activity refreshes it, at most once a minute (and at most once per quarter of the limit for limits under four minutes, so short limits stay accurate). |
| `SESSION_ABSOLUTE_TIMEOUT_SECONDS` | `1209600` (14 days) | A session ends this long after sign-in however active it has been. Activity never extends it. |

Both accept whole seconds from `60` to `7776000` (90 days); blank uses the
default. The idle limit must not exceed the absolute limit. A malformed or
out-of-range value stops startup instead of silently falling back, as does an
unsafe production configuration. The defaults keep the previous 14-day ceiling
and add the idle limit; a stricter deployment might use `3600` / `43200`.

The limits are checked against the registry on **every request**, so they hold
even when a client keeps a cookie past its `Max-Age` and the cookie's signature
is still cryptographically valid.

## The registry

`user_sessions` (created idempotently by `database.ensure_user_sessions_schema`,
called from `init_db` and again when the store connects):

| Column | Purpose |
| --- | --- |
| `session_token_hash` | SHA-256 digest of the 256-bit random identifier. **The identifier itself is never stored**, so a database leak or backup cannot be replayed as a session. |
| `user_id` | Immutable owner (foreign key to `users`). The leading column of the index `idx_user_sessions_user (user_id, revoked_at)`. |
| `created_at` | Sign-in time; the anchor of the absolute limit. |
| `last_seen_at` | Last activity; the anchor of the idle limit. |
| `revoked_at`, `revoked_reason` | Set when the session is ended: `logout`, `logout_all`, `reauthenticated`, `access_removed`. |

Every statement is parameterised and every mutation is keyed by both the session
digest *and* its owner, so knowing someone else's identifier is not enough to
revoke or read their session. Revoked and expired rows are kept for seven days
for incident review and then purged when the next session is created.

## Rotation

A new identifier is issued on every sign-in (`SessionLifecycle.establish`). In
one operation it:

1. registers a brand-new session for the user (so a storage failure leaves the
   previous state untouched);
2. revokes whichever identifier the browser already held (`reauthenticated`),
   which covers signing in again and switching accounts in the same browser;
3. discards **all** pre-sign-in session data, including leftover OAuth state, so
   an identifier or state planted before sign-in is useless afterwards
   (session-fixation defence).

**Privilege changes.** The product has no roles or step-up authentication, so the
privilege transitions that exist are: anonymous → signed-in (rotated, above),
one account → another (rotated, above) and access being withdrawn
(`ALLOWED_EMAILS` and `ALLOWED_EMAIL_DOMAINS`, below). The registry binds each identifier to one `user_id`;
a cookie whose `user_id` differs from the registry owner is rejected, so code
that rewrites the identity inside an existing session without calling
`establish` fails closed on the very next request. **Any future role or step-up
feature must call `SessionLifecycle.establish` when it changes what a session may
do.**

## Revocation

| Action | Endpoint | Effect |
| --- | --- | --- |
| Log out | `POST /api/auth/logout` | Revokes this browser's session and deletes its cookie. CSRF-protected. The navigation link `GET /logout` is kept for bookmarks, but only a same-origin or user-typed navigation signs out; navigations the browser reports as cross-site or same-site (a sibling subdomain) are ignored, so another site cannot sign a user out by linking to it. |
| Log out of all devices | `POST /api/auth/logout-all` | Revokes every session of the authenticated user, on every device, including this one, and deletes this cookie. Returns the number of sessions that were still active. The owner always comes from the verified session; a request that claims another owner is rejected. Rows that had already aged out are revoked too, so loosening a timeout later cannot revive a signed-out device. |
| Access withdrawn | remove an address from `ALLOWED_EMAILS`, or a domain from `ALLOWED_EMAIL_DOMAINS` | The allow-lists are re-applied to existing sessions: the next request revokes the session (`access_removed`). Adding the address or domain back does not revive it. |
| Operator | see [`OPERATIONS_RUNBOOKS.md`](OPERATIONS_RUNBOOKS.md) | Rotate `WEB_AUTH_SECRET` to invalidate every cookie, or revoke rows directly. |

There is currently no account-deletion flow. Any future one must call `SessionStore.revoke_all` for the deleted user so no session outlives its account.

The Angular client signs out with the POST endpoints (Account page: **Log out**
and **Sign out of all devices**) and treats any `401` from the API as an ended
session: it clears its user state and returns to the sign-in page.

## Failure behaviour

| Situation | Result |
| --- | --- |
| Cookie unsigned, tampered or older than its signature allows | Treated as logged out. |
| Signed cookie with no registered session (legacy cookie, forged with a leaked secret, guessed identifier) | Emptied before any guard sees it: logged out, cookie deleted. |
| Revoked, idle-expired or absolute-expired session | Logged out, cookie deleted. |
| Signed cookie whose `user_id` is not the registry owner | Logged out, cookie deleted, warning logged (no identifiers). |
| Registry database unavailable | `503` with `Retry-After`; **never** access. The cookie is *not* deleted, because a transient fault is not a sign-out. |
| Refreshing `last_seen_at` blocked by a busy writer | The request proceeds; the refresh is retried on the next request. |
| Pre-sign-in browser (no identity in the cookie) | Never queries the registry. |

The emptied session is seen by the authentication boundary, the mutation guard
and every route as an anonymous browser, so no route can accidentally honour a
revoked cookie. The CSRF layer applies the same registry check, so a revoked
cookie can neither mint nor spend a CSRF token.

## Deployment and rollback

* **First deploy signs everyone out once.** Existing cookies have no `sid`, so
  they are rejected (fail closed) and users sign in again with Google. The old
  `session` cookie is ignored in production and expires on its own.
* The schema change is additive and idempotent. Rolling the code back is safe:
  older code ignores `user_sessions`, and its sessions use a different cookie
  name, so users would simply sign in again.
* The registry lives in the same SQLite file as the rest of the data and nothing
  is cached per process, so a revocation takes effect immediately for every
  worker sharing that file.

## Verification

```bash
.venv/bin/python -m pytest -q backend/tests/test_session_security.py \
  backend/tests/test_csrf_security.py backend/tests/test_mutation_security.py \
  backend/tests/test_anonymous_access_routes.py backend/tests/test_database.py
```

| Acceptance criterion | Tests in `test_session_security.py` |
| --- | --- |
| Cookie attributes | `test_login_cookie_is_secure_httponly_lax_host_only_and_bounded`, `test_development_cookie_keeps_httponly_and_samesite_without_secure`, `test_google_callback_issues_a_registered_hardened_session`, the `*_policy_*` configuration tests |
| Rotation | `test_login_rotates_the_identifier_and_discards_pre_login_state`, `test_logging_in_again_revokes_the_previous_identifier`, `test_switching_account_in_the_same_browser_ends_the_previous_users_session`, `test_callback_rotates_away_leftover_oauth_state_and_old_identifiers` |
| Idle and absolute expiry | `test_session_expires_exactly_at_the_idle_limit`, `test_an_active_user_is_never_idled_out_even_with_a_short_idle_limit`, `test_absolute_limit_is_never_extended_by_activity`, `test_idle_session_is_rejected_and_its_cookie_deleted`, `test_expiry_is_enforced_even_though_the_cookie_signature_is_still_valid` |
| Logout, logout-all | `test_logout_revokes_server_side_so_a_stolen_copy_is_dead`, `test_logout_all_ends_every_device_of_that_user_and_only_that_user`, `test_api_logout_*`, `test_logout_all_ignores_client_supplied_owner_identifiers` |
| Expired session | `test_expired_session_is_rejected_by_the_real_application` and the expiry tests above |
| Stolen cookie | `test_logout_revokes_server_side_so_a_stolen_copy_is_dead`, `test_correctly_signed_cookie_without_a_registered_session_is_rejected`, `test_signed_cookie_cannot_be_retargeted_at_another_tenant`, `test_secure_entrypoint_lets_a_revoked_cookie_neither_mint_nor_spend_csrf_tokens` |

Time is driven by an injected clock, so no test sleeps or depends on wall-clock
timing. After deploying, confirm in the browser's developer tools that the
`__Host-session` cookie shows **HttpOnly**, **Secure**, **SameSite Lax**, path
`/`, and no domain.
