---
name: secrets-and-encryption
description: 'Store or read any per-user credential (AI provider key, connector API key, OAuth refresh token) safely through encryption.py and the user_api_keys table. Use whenever a feature needs to persist something sensitive tied to a user.'
---

# Secrets and Encryption

## When to Use

Any time a feature needs to remember a per-user secret: an AI provider key,
a connector API key/OAuth client secret/refresh token, or anything else that
would be bad to leak.

## How It Works Today

- `encryption.py` wraps `cryptography`'s Fernet symmetric encryption, keyed
  by the `ENCRYPTION_KEY` env var. If `ENCRYPTION_KEY` is unset or
  `cryptography` isn't installed, it **falls back to storing plaintext with a
  logged warning** — this is a deliberate degrade-gracefully choice for local
  single-user dev, but it means `ENCRYPTION_KEY` is effectively mandatory for
  any real multi-user/public deployment. Never remove the warning or make the
  fallback silent.
- `user_api_keys` (database.py) has dedicated encrypted columns:
  `encrypted_api_key`, `encrypted_client_secret`, `encrypted_refresh_token`,
  plus `extra_json` for anything else provider-specific — use the field that
  matches the credential type rather than stuffing everything into
  `extra_json`.
- Helpers already exist: `database.save_user_api_key(user_id, provider,
  api_key=None, client_secret=None, refresh_token=None, extra=None)`,
  `database.get_user_api_key(user_id, provider)`,
  `database.get_user_api_keys(user_id)` — use these, don't write raw SQL
  against `user_api_keys` from feature code.

## Procedure

1. **Never** put a per-user secret in a process env var, a plain (non-`encrypted_`)
   column, or `extra_json` if a dedicated encrypted column already fits.
2. **Never** return a stored secret's full value from an API endpoint after
   the initial save — mask it (e.g. `sk-***...{last 4 chars}`) for any
   "current settings" display, matching how the Settings page should behave
   (check `webapp/app.py`'s existing `/api/settings/key` handlers for the
   established masking convention before adding a new one).
3. **Never** log a secret, even at debug level — grep your diff for
   `api_key`/`client_secret`/`refresh_token`/`token` near any `logging.*`
   or `print()` call before considering a task done.
4. When adding a **new credential type** for a new provider/connector, reuse
   the existing `user_api_keys` schema (it's generic across providers via
   the `provider` column) rather than adding a new bespoke table — only add
   a new table if the credential genuinely needs structure the existing
   columns can't express.
5. **Session secrets** (`WEB_AUTH_SECRET`) are a separate concern from
   per-user API keys — they sign the login session cookie, not stored in the
   DB at all. Don't conflate the two; a bug that leaks `WEB_AUTH_SECRET`
   compromises every user's session, so it must only ever be read from env,
   never persisted or exposed via any endpoint or error message.

## Verification

Run the `verification-gate` skill's steps. Additionally: confirm
`ENCRYPTION_KEY` being unset doesn't crash the app (graceful plaintext
fallback with a warning), and that a saved key round-trips correctly
(`save_user_api_key` → `get_user_api_key` returns the original plaintext
value, not the ciphertext) when `ENCRYPTION_KEY` **is** set.

## Gotchas

- Rotating `ENCRYPTION_KEY` invalidates every previously-encrypted value in
  the DB (Fernet ciphertext is tied to the key that produced it) — there is
  currently no key-rotation/re-encryption path. If you add one, it needs to
  decrypt-with-old-key then re-encrypt-with-new-key for every row, not just
  swap the env var.
- `.env.example` documents `ENCRYPTION_KEY` — any new secret-bearing env var
  must be documented there too, per `AGENTS.md` §5.
