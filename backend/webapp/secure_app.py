"""Production ASGI entrypoint with operational and layered HTTP security boundaries."""

from __future__ import annotations

import os

from webapp.proxy_security import ProxySecurityMiddleware, load_proxy_security_config

# Validate the canonical origin and trusted proxy policy before importing the
# route graph so production startup fails closed on unsafe deployment settings.
# Starlette's TestClient uses ``testserver`` as its synthetic request host, so
# give the explicit test runtime a matching local origin unless a test provides
# a stricter WEB_PUBLIC_URL itself. Production and development policy is
# unchanged.
_proxy_environment = os.environ
if (
    os.environ.get("APP_ENV", "").strip().lower() == "test"
    and not os.environ.get("WEB_PUBLIC_URL", "").strip()
):
    _proxy_environment = dict(os.environ)
    _proxy_environment["WEB_PUBLIC_URL"] = "http://testserver"
_PROXY_SECURITY = load_proxy_security_config(_proxy_environment)

from webapp.app import DB_PATH, app as application  # noqa: E402
from webapp.csrf_security import CSRFMiddleware  # noqa: E402
from webapp.health import (  # noqa: E402
    OperationalHealthMiddleware,
    install_authenticated_diagnostics,
)
from webapp.security_headers import SecurityHeadersMiddleware  # noqa: E402


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
# production entrypoint wraps every interactive route. CSRF is installed only
# when cookie authentication is configured; production runtime validation
# already requires that configuration and fails startup otherwise.
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

# ProxySecurityMiddleware is the request-side boundary for the interactive app:
# it validates Host and strips untrusted forwarding metadata before FastAPI,
# OAuth, rate limiting, or audit logging can consume it. Operational probes stay
# outside that boundary so container/orchestrator health checks can reach them
# directly. Security headers remain outermost for every response.
app = SecurityHeadersMiddleware(
    OperationalHealthMiddleware(
        ProxySecurityMiddleware(secured_application, _PROXY_SECURITY),
        db_path=DB_PATH,
    )
)
