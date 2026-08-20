"""Production ASGI entrypoint with operational and HTTP security boundaries."""

from __future__ import annotations

from webapp.app import DB_PATH, app as application
from webapp.health import OperationalHealthMiddleware, install_authenticated_diagnostics
from webapp.security_headers import SecurityHeadersMiddleware

# Authenticated dependency details live on the FastAPI application so the
# existing session/auth middleware remains authoritative. Public liveness and
# readiness are served outside that auth boundary, then the whole stack is
# wrapped with the same defence-in-depth response headers as normal traffic.
install_authenticated_diagnostics(application, db_path=DB_PATH)
app = SecurityHeadersMiddleware(
    OperationalHealthMiddleware(application, db_path=DB_PATH)
)
