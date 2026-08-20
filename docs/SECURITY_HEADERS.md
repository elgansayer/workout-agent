# HTTP security header baseline

The production web image starts `webapp.secure_app:app`. That entrypoint wraps the FastAPI application with `SecurityHeadersMiddleware`, so API responses, authentication redirects, the Angular shell, static files, and mounted applications receive the same defence-in-depth headers.

## Content Security Policy

A fresh cryptographically random nonce is generated for every HTTP response. For HTML responses the middleware injects that nonce into `<script>` and `<style>` elements and sets Angular's `ngCspNonce` attribute on `<app-root>`. Angular therefore reuses the request nonce when it creates component `<style>` elements at runtime.

The baseline denies objects and framing, restricts scripts/styles/resources to the application origin, disables inline script attributes, restricts forms to the application origin, and upgrades insecure subresource requests. `script-src` never permits `unsafe-inline`.

### Temporary style-attribute exception

`style-src-attr 'unsafe-inline'` is the only inline exception. It is deliberately scoped to style attributes because the current Angular templates use `[style.*]` bindings for dynamic presentation. This does **not** allow inline scripts or un-nonced `<style>` elements. New inline script exceptions are not permitted; new styles should prefer classes or nonce-bearing Angular component styles. The exception can be removed once the remaining style-attribute bindings have been migrated.

## Other response headers

The wrapper also sends:

- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY` as a legacy companion to CSP `frame-ancestors 'none'`
- a restrictive `Permissions-Policy` for sensors, camera, microphone, location, payment and USB
- `Cross-Origin-Opener-Policy: same-origin`
- `Cross-Origin-Resource-Policy: same-site`
- `Cross-Origin-Embedder-Policy: credentialless`
- `X-Permitted-Cross-Domain-Policies: none`

`Cross-Origin-Resource-Policy` uses `same-site` rather than `same-origin` so supported same-site frontend deployments on a different port are not needlessly broken while cross-site embedding remains restricted.

## Verification

Run the deterministic policy and middleware tests from the backend directory:

```bash
python -m unittest discover -s tests -p 'test_security_headers.py' -v
```

Validate any deployed hostname with the standard-library smoke checker:

```bash
python backend/scripts/check_security_headers.py https://workout.elgansayer.com/
```

`HTTP security headers` runs the unit checks on pull requests. After the `Build and push images` workflow completes successfully on `main`, it retries the same production smoke policy against `https://workout.elgansayer.com/` to allow the Portainer rollout to converge before failing the deployment check.
