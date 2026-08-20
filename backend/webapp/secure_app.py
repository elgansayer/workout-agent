"""Production ASGI entrypoint with defence-in-depth request/response guards."""

from __future__ import annotations

from webapp.proxy_security import (
    ProxySecurityMiddleware,
    load_proxy_security_config,
)

# Validate the public origin/proxy boundary before importing the route graph.
_PROXY_SECURITY = load_proxy_security_config()

from webapp.app import app as application  # noqa: E402
from webapp.security_headers import SecurityHeadersMiddleware  # noqa: E402

# The proxy boundary executes before FastAPI sees Host, scheme, client IP, or
# forwarding metadata. Security headers remain the outermost response wrapper
# so even rejected requests receive the standard response hardening.
app = SecurityHeadersMiddleware(
    ProxySecurityMiddleware(application, _PROXY_SECURITY)
)
