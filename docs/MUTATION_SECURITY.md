# Mutation authentication and ownership boundary

Workout Agent treats every state-changing browser/API request as private. The web application can expose a deliberately read-only anonymous mode in trusted local/test environments, but anonymous writes are never permitted.

## Global contract

`webapp.mutation_security` installs a fail-closed ASGI boundary immediately inside Starlette's signed `SessionMiddleware`. Every `POST`, `PUT`, `PATCH`, and `DELETE` request therefore requires both the authenticated session identity and its immutable `user_id` before route code executes. Missing or incomplete identities receive `401 Not authenticated`.

This is a default-deny boundary. New mutation routes, uploads, and action endpoints inherit it automatically instead of relying on each route author remembering to add an authentication check.

The trusted owner is always the signed session `user_id`. Request bodies, query strings, form fields, multipart fields, and `X-User-Id`/`X-Owner-Id`/`X-Account-Id` headers are never used to select an owner. When a small request explicitly supplies one of those owner assertions, the guard verifies that it equals the authenticated session. A different owner receives the deliberately non-enumerating `404 Not found` response.

Large or streaming uploads are still authenticated globally. Their owner must still be obtained from the signed session; the guard simply avoids buffering large bodies solely for the extra contradictory-claim check.

## Route and service requirements

Authentication is only the outer boundary. Object-level authorization remains mandatory:

- derive the caller from `request.state.mutation_user_id` or the same signed `request.session["user_id"]`;
- never accept an arbitrary client-supplied user identifier as the database owner;
- scope `SELECT`, `UPDATE`, and `DELETE` operations by the authenticated owner;
- return a non-enumerating `404` when an authenticated user names another tenant's object;
- keep credentials and health payloads out of logs and error responses.

The current programme, credential, preferences/profile, push-subscription, Hevy sync, Google Health disconnect, chat-clear, and notification mutation paths all execute behind this boundary. Notification object mutations additionally pass the authenticated `user_id` into the database operation, so another user's notification ID is indistinguishable from a missing record.

## OAuth callbacks and future webhooks

Current OAuth callbacks are `GET` endpoints and use purpose-specific OAuth state handling, so they are not browser mutation routes covered by the method guard. A future inbound webhook must **not** be made anonymous by adding a broad mutation exception. It should use a separate purpose-specific signature/secret verification boundary, replay protection, and tenant resolution derived from verified provider metadata.

## Verification

Focused regression coverage lives in `backend/tests/test_mutation_security.py`. It proves anonymous rejection and cross-user denial across credentials, profiles, programmes, check-ins, notifications, and connector state, plus PUT/PATCH/DELETE, URL-encoded forms, headers, query strings, nested JSON, and multipart uploads.
