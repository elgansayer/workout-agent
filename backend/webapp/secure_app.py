"""Production ASGI entrypoint with operational and layered HTTP security boundaries."""

from __future__ import annotations

import os

from webapp.app import DB_PATH, app as application
from webapp.csrf_security import CSRFMiddleware
from webapp.health import OperationalHealthMiddleware, install_authenticated_diagnostics
from webapp.security_headers import SecurityHeadersMiddleware


def _trusted_browser_origins() -> tuple[str, ...]:
    """Return explicit production SPA origins allowed to submit mutations."""

    configured = [
        value.strip()
        for value in os.environ.get("ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    ]
    public_url = os.environ.get("WEB_PUBLIC_URL", "").strip()
    if public_url:
        configured.append(public_url)

    if os.environ.get("APP_ENV", "").strip().lower() not in {"prod", "production"}:
        configured.extend(
            (
                "http://localhost:4200",
                "http://127.0.0.1:4200",
                "http://localhost:8770",
                "http://127.0.0.1:8770",
                "http://localhost:8080",
                "http://127.0.0.1:8080",
                "http://localhost:3000",
            )
        )
    return tuple(dict.fromkeys(configured))


# Register authenticated diagnostics on the canonical FastAPI application so
# its existing session/auth boundary remains authoritative.
install_authenticated_diagnostics(application, db_path=DB_PATH)

# Keep the canonical FastAPI object importable for unit tests, while the
# production entrypoint wraps every route. CSRF is installed only when cookie
# authentication is configured; production runtime validation already requires
# that configuration and fails startup before this module is reached otherwise.
secured_application = application
_session_secret = os.environ.get("WEB_AUTH_SECRET", "").strip()
_google_client_id = os.environ.get("WEB_GOOGLE_CLIENT_ID", "").strip()
if _session_secret and _google_client_id:
    secured_application = CSRFMiddleware(
        secured_application,
        db_path=DB_PATH,
        session_secret=_session_secret,
        trusted_origins=_trusted_browser_origins(),
    )

# Liveness/readiness stay outside interactive auth and CSRF so orchestrators can
# probe the service without a browser session, while security headers still wrap
# every response including the public operational probes.
app = SecurityHeadersMiddleware(
    OperationalHealthMiddleware(secured_application, db_path=DB_PATH)
)
