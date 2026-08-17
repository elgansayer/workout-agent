"""Production ASGI entry point with fail-closed auth and private caching."""

from __future__ import annotations

from webapp.app import app as core_app
from webapp.security import PrivateResponseHeadersMiddleware

app = PrivateResponseHeadersMiddleware(core_app)
