# CSRF security boundary

Production uses cookie-backed sessions, so every browser `POST`, `PUT`, `PATCH`, and `DELETE` is protected at the outer ASGI boundary before application route code runs.

The Angular client obtains a fresh token from `GET /api/csrf-token` immediately before each mutation and sends it as `X-CSRF-Token`. Tokens expire after ten minutes, are persisted only as SHA-256 hashes, are bound to both the authenticated user and the exact signed session cookie, and are deleted atomically on successful validation. A captured token therefore cannot be replayed or moved to another user or browser session.

Browser `Origin` is required to match the request origin or an explicitly configured `ALLOWED_ORIGINS` entry. Production does not implicitly trust localhost origins. The token endpoint is `Cache-Control: no-store` and mirrors credentialed CORS headers only for an origin that passed that explicit check.

OAuth callbacks remain safe-method `GET` requests and continue to use their purpose-specific OAuth state/nonce validation. Connector webhooks do not receive a path-based CSRF exemption: requests without the application's session cookie pass through to the connector's own signature and replay validation, while a browser request carrying a session cookie must satisfy CSRF regardless of path.

The canonical production entrypoint is `webapp.secure_app:app`. Direct imports of `webapp.app:app` are retained for unit-level testing and local anonymous development; cookie-authenticated deployments must use the secured entrypoint.
