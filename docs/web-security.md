# Web security boundary

The dashboard supports two explicit operating modes:

1. **Production with Google OAuth**. This is the default for the web container image.
2. **Anonymous local development**. This must be enabled deliberately and is rejected in production.

There is no implicit anonymous mode.

## Production configuration

The web process refuses to start when `APP_ENV` is `production` or `prod` unless all of the following are present:

```dotenv
APP_ENV=production
WEB_AUTH_MODE=google
WEB_AUTH_SECRET=<strong random secret>
WEB_GOOGLE_CLIENT_ID=<google oauth client id>
WEB_GOOGLE_CLIENT_SECRET=<google oauth client secret>
ALLOWED_EMAILS=owner@example.com
WEB_ALLOW_ANONYMOUS=0
```

Generate the session secret rather than writing one manually:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

`WEB_AUTH_SECRET` must contain at least 32 characters and sufficient character diversity. Validation reports variable names and corrective actions but never prints configured secret values.

`ALLOWED_EMAILS` is a comma-separated server-side allowlist. Production requires at least one syntactically valid address.

## Local development

Anonymous development requires both a non-production environment and an explicit opt-in:

```bash
APP_ENV=development \
WEB_ALLOW_ANONYMOUS=1 \
uvicorn webapp.app:app --reload
```

`WEB_ALLOW_ANONYMOUS=1` is rejected whenever the environment is `production` or `prod`. It is also rejected when combined with a partial authentication configuration, avoiding ambiguous deployments.

## Production entry point

`Dockerfile.web` runs:

```text
uvicorn webapp.secure_app:app
```

The secure entry point wraps the FastAPI application with a response-header boundary. Every non-static HTTP response receives:

```text
Cache-Control: private, no-store, max-age=0
Pragma: no-cache
Expires: 0
Vary: Cookie
```

This applies to dashboard HTML, settings, APIs, authentication responses, exports, errors, and streaming endpoints. Versioned files under `/static/` retain their independent static-asset cache behavior.

## Deployment order

Configure the Google OAuth client, session secret, and allowlisted email addresses in Portainer or the deployment secret store **before** deploying the secure image. A deployment with missing or partial values will stop during application import by design rather than expose the dashboard anonymously.

After deployment, verify:

```bash
curl -I https://workout.elgansayer.com/settings
curl -I https://workout.elgansayer.com/api/chat/history
```

Logged-out personalised routes must redirect to login or return `401`, and their responses must include the private no-store policy.

## Tests

Run the deterministic security tests with:

```bash
pytest -q tests/test_web_security.py
```

The suite covers explicit local mode, missing and partial production credentials, weak session secrets, anonymous-mode rejection, allowlist validation, and private response headers.
