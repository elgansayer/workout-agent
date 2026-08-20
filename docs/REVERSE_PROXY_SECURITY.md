# Reverse-proxy and public-origin security

The production web entrypoint has one explicit trust boundary for request origin
metadata. Its job is to make `Host`, external scheme, client IP, OAuth redirect
URLs, absolute URLs, rate-limit keys, and request/audit metadata agree on the
same validated view of the request.

## Required production configuration

Set the canonical public origin and the exact proxy networks that are allowed to
supply forwarding metadata:

```dotenv
APP_ENV=production
WEB_PUBLIC_URL=https://workout.example.com
WEB_ALLOWED_HOSTS=www.workout.example.com
WEB_TRUSTED_PROXY_CIDRS=172.18.0.1/32
```

`WEB_PUBLIC_URL` is required in production and must be an HTTPS origin only. Do
not include a path, query string, fragment, or credentials. Its host is always
accepted and is the canonical host emitted downstream.

`WEB_ALLOWED_HOSTS` is optional. It adds comma-separated public aliases that may
arrive in the raw `Host` header. Accepted aliases are canonicalised to the host
from `WEB_PUBLIC_URL` before the application sees the request.

`WEB_TRUSTED_PROXY_CIDRS` is optional and defaults to trusting no proxy
forwarding headers. Use the narrowest possible IPv4/IPv6 CIDRs for the immediate
reverse proxies. Do not configure `0.0.0.0/0` or `::/0` merely to make a proxy
work.

## Request trust model

The production Docker image starts Uvicorn with `--no-proxy-headers`. This is
intentional: Uvicorn must not rewrite the ASGI client or scheme before the
application can determine whether the socket peer itself is trusted.

`webapp.proxy_security.ProxySecurityMiddleware` then applies these rules before
FastAPI handles a request:

1. Exactly one raw `Host` header must be present and it must match the canonical
   host or a configured public alias. Unknown, duplicate, or malformed hosts
   receive HTTP 400.
2. `Forwarded`, `X-Forwarded-For`, `X-Real-IP`, `X-Forwarded-Host`,
   `X-Forwarded-Proto`, and `X-Forwarded-Port` are removed before application
   code runs.
3. Forwarding metadata is considered only when the real socket peer is inside
   `WEB_TRUSTED_PROXY_CIDRS`. Metadata sent directly by an untrusted client is
   ignored and stripped.
4. A trusted `X-Forwarded-For` chain is parsed as IP addresses and walked from
   right to left across trusted proxy hops. The first untrusted address becomes
   the validated client IP. Malformed chains fail closed with HTTP 400.
5. A trusted `X-Forwarded-Host` must itself be an allowed public host. A trusted
   `X-Forwarded-Proto`, when present, must be `http` or `https`. Neither value is
   allowed to override the configured canonical origin.
6. The downstream ASGI scope is rewritten to the canonical scheme and host and
   to the validated client address. The middleware also exposes
   `validated_origin`, `validated_client_ip`, and `trusted_proxy` in
   `scope["state"]`.

The standard RFC `Forwarded` header is deliberately stripped rather than
interpreted. Supporting one well-defined proxy-header format reduces ambiguity
between proxy implementations.

## Why this also protects OAuth, URLs, rate limits, and logs

The existing web routes call `request.url_for(...)` for OAuth callbacks and
absolute URLs, and the rate limiter falls back to `request.client` after checking
forwarding headers. Because the boundary runs first, it replaces the Host and
scheme, strips forwarding headers, and supplies the validated client address.
Those consumers therefore cannot be influenced by a direct client's forged
`Host` or `X-Forwarded-*` values.

The same normalized `request.client` is available to request and audit logging,
while `request.state.validated_origin` and
`request.state.validated_client_ip` provide explicit validated values for new
code.

## Reverse-proxy configuration

The proxy should:

- preserve the browser's public `Host` value;
- connect to the application from an address covered by
  `WEB_TRUSTED_PROXY_CIDRS`;
- overwrite or correctly append `X-Forwarded-For`;
- set `X-Forwarded-Host` to the public host;
- set `X-Forwarded-Proto` to the browser-facing scheme.

Do not expose the application container directly to untrusted networks when its
proxy CIDR is trusted.

## Verification

Run the deterministic boundary tests:

```bash
APP_ENV=test ALLOW_ANONYMOUS_WEB=1 \
  python -m pytest -q backend/tests/test_proxy_security.py
```

They cover production configuration, untrusted and duplicate hosts, direct
forwarding-header spoofing, multi-hop trusted proxy chains, forwarded-host
validation, canonical HTTPS enforcement, malformed chains, and the Uvicorn
startup contract.

Before rollout, also verify these negative cases through the deployed proxy:

```bash
curl -i -H 'Host: evil.example' https://workout.example.com/
curl -i -H 'X-Forwarded-For: 127.0.0.1' https://workout.example.com/
```

The first request must be rejected. The second must not make an untrusted direct
peer appear to the application as `127.0.0.1`.
