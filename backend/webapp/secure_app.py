"""Production ASGI entrypoint with defence-in-depth HTTP response headers."""

from __future__ import annotations

from webapp.app import app as application
from webapp.security_headers import SecurityHeadersMiddleware

# Keep the FastAPI application unchanged for unit-level imports while ensuring
# the production Uvicorn entrypoint is wrapped outside every mounted route.
app = SecurityHeadersMiddleware(application)
