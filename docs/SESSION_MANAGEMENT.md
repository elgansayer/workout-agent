# Account session management

The web dashboard stores browser sessions in the `user_sessions` table. The
signed cookie carries an opaque session id (`sid`); the database stores only a
SHA-256 hash of that id, the owning `user_id`, a coarse device label, a coarse
location approximation, creation time, last-used time, and an optional
revocation timestamp. Raw user agents, full IP addresses, and session
credentials are never stored or returned.

## User-visible behaviour

- The account page (`/profile`) lists only the authenticated user's active
  sessions: device, approximate location, creation time, last-used time, and
  which session is the current browser.
- `DELETE /api/sessions/{id}` revokes one session owned by the caller.
  `POST /api/sessions/revoke-others` revokes every other active session.
  Cross-account ids are treated as not found.
- Revoking the current session clears the cookie and signs the browser out.
  Revoking another session rotates the caller's session id so an observed
  cookie cannot be replayed after the security-sensitive change.
- Logging out revokes the current server-side session before clearing the
  cookie.

## Revocation guarantees

The authentication boundary validates the session id against the registry on
every non-public request. A revoked, expired, unknown, or foreign session id
fails closed immediately, so revocation takes effect on the next request rather
than waiting for cookie expiry.

Sessions created before this registry existed have no `sid`; they are adopted
once on their next request and become revocable like any new session. Once a
user has revoked any session (logout, single revoke, or signing out other
sessions), unregistered legacy cookies are rejected instead of adopted, so a
copied legacy cookie cannot be replayed after revocation. Sessions expire after
14 days.

## Privacy boundaries

- Device labels are derived from the user agent and bounded to 160 characters.
- Location is limited to an optional two-letter CDN country hint plus a masked
  IPv4 `/24` or IPv6 `/48` network prefix. Private addresses render as
  `Local network`.
- API responses never include the session hash, the opaque session id, the raw
  user agent, or the full client IP address.
- Every read, write, revoke, and rotate statement filters on `user_id` from the
  authenticated session, never on a client-supplied owner id.
