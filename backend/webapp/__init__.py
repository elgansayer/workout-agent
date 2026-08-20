"""Security bootstrap for the internal workout web application.

Importing the package validates the runtime authentication boundary before any
route, database, or OAuth setup can occur. When the web runtime dependencies are
installed it also installs the fail-safe response cache guard so health data and
credential-bearing responses are private by default.
"""

from __future__ import annotations

from webapp.runtime_security import WebRuntimeSecurity, validate_web_runtime

RUNTIME_SECURITY: WebRuntimeSecurity = validate_web_runtime()

# Some focused security-policy workflows intentionally test pure-Python modules
# without installing the FastAPI web dependency set. Do not make importing
# webapp.runtime_security or webapp.security_headers depend on FastAPI. In the
# actual web runtime FastAPI is mandatory and the guard is still installed
# fail-safe before application construction.
try:
    from webapp.cache_security import install_response_cache_guard
except ModuleNotFoundError as exc:
    if exc.name not in {"fastapi", "starlette"}:
        raise
else:
    install_response_cache_guard()

__all__ = ["RUNTIME_SECURITY"]
