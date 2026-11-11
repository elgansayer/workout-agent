# Invite-only beta onboarding

Controlled onboarding is opt-in. Set `BETA_INVITE_ONLY=1` to require a valid
invitation before an unknown Google account can create an account. Existing
accounts continue to sign in normally.

Administrators are identified by the `ADMIN_EMAILS` environment variable
(comma-separated Google email addresses). An empty value disables the
administrator surface, failing closed.

## Invitation lifecycle

Each invitation has a bearer link of the form `/invite/<token>`.

- **Single-use**: accepting it creates or attaches exactly one account.
- **Email-bound**: the Google email must match the intended email stored on the
  invitation, and Google must report the address as verified (`email_verified`).
- **Expiring**: `ttl_hours` defaults to 72 and is capped at 720 hours.
- **Revocable**: pending invitations can be revoked; accepted invitations
  cannot.
- **Rate limited**: admins can create 20 invitations per hour and revoke 60 per
  hour. Redemption links are also rate limited per client.
- **Hashed at rest**: only a SHA-256 hash of the bearer token is stored. The raw
  token is returned once in the creation response.

Derived statuses are `pending`, `accepted`, `expired`, and `revoked`. Listing
invitations never consults or exposes account existence.

## Administrator API

All routes require an authenticated session whose email is in `ADMIN_EMAILS`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/admin/invitations?status=all\|pending\|accepted\|expired\|revoked` | List invitations |
| `POST` | `/api/admin/invitations` | Create `{ "email": "...", "ttl_hours": 72, "policy": "beta" }` |
| `POST` | `/api/admin/invitations/{id}/revoke` | Revoke a pending invitation with optional `{ "reason": "..." }` |

The creation response includes `token` and `invite_url` exactly once. The
Settings page exposes the same controls to administrators under **Invitations**.

## Sign-in flow

1. The invitee opens `/invite/<token>`; the token is stored in the signed
   session and the browser is sent to Google sign-in.
2. The OAuth callback compares the Google email with the invitation and, in one
   SQLite transaction, creates or reuses the account and marks the invitation
   accepted.
3. Retrying the callback with the same token and email returns the same account;
   it does not create a duplicate.

Uninvited sign-in attempts return a generic `403` without revealing whether an
account exists. Revoked, expired, consumed, and mismatched invitations also
return the same generic failure.
