# OAuth correlation and redirect security

Both OAuth flows in Workout Agent (web sign-in and the Google Health connector)
use replay-resistant correlation and strict redirect handling.

## Correlation state

- `state`, `nonce` and PKCE material are generated for every authorization
  request. They are short-lived (10 minutes) and consumed exactly once.
- The callback must come from the same browser session that initiated it. A
  callback from a different session is rejected, even when the signed `state` or
  the Authlib session entry is copied verbatim.
- The signed envelope binds the authenticated user, connector/provider, nonce,
  redirect URI and session identity. A provider mix-up, tenant mismatch, expired
  state or replayed callback fails closed.
- A server-side `oauth_state_claims` ledger records each consumed state. The
  claim is atomic, so a replayed callback using an older copy of the session
  cookie still fails and concurrent callbacks cannot both succeed.
- Concurrent connection attempts each keep independent state and nonce values.
  Starting a new attempt never lets it complete with another attempt's code.
- Web sign-in delegates state, nonce and the PKCE verifier to Authlib, while
  `webapp.app._HardenedOAuth` / `webapp.oauth_state.single_use_integration`
  enforces the 10-minute expiry and single-use claim that Authlib's session
  backend otherwise records but does not check.

## PKCE

- Web sign-in requests `code_challenge_method=S256` from Authlib.
- The Google Health connector generates an S256 verifier/challenge pair; the
  verifier is derived again from the server secret when the matching state is
  consumed and is only sent in the token exchange.
- The one-time CLI helper (`google_health_auth.py`) uses the same PKCE helpers.

## Redirect URIs

- Redirect URIs must be safe absolute HTTP(S) URLs with a host and no userinfo or
  fragment, and must match an allowlisted value **exactly** (no prefix, suffix,
  scheme or host substitution).
- The allowlist is the explicit `WEB_GOOGLE_REDIRECT_URI` /
  `GOOGLE_HEALTH_REDIRECT_URI` value and/or the canonical `WEB_PUBLIC_URL`
  origin. In production `WEB_PUBLIC_URL` is mandatory, so a forged request Host
  cannot widen the allowlist.
- The callback completes the token exchange with the redirect URI stored at
  initiation, never a value recomputed from the callback request.

## Tests

- `backend/tests/test_connector_oauth.py`: signing, tamper detection, expiry,
  session/provider binding, replay, concurrent claims, PKCE and redirect
  allowlist.
- `backend/tests/test_google_health_oauth.py`: connect/callback replay, cross
  session mix-up, expiry, concurrent connections, stored redirect URI, forged
  Host/open-redirect handling, configured login callback override, and web
  sign-in PKCE/state expiry.
