"""Security bootstrap for the internal workout web application.

Importing the package always validates the runtime authentication boundary.
When the optional web dependencies are installed it also installs process-wide
guards so personalised responses require authentication and remain private by
default.  Core/runtime-security tooling intentionally does not install FastAPI,
so guard installation must stay lazy in those environments.
"""

from __future__ import annotations

from webapp.runtime_security import WebRuntimeSecurity, validate_web_runtime

RUNTIME_SECURITY: WebRuntimeSecurity = validate_web_runtime()

try:
    import fastapi as _fastapi  # noqa: F401
except ModuleNotFoundError:
    # The core agent and focused runtime-security checks do not install the
    # optional web requirements.  A real web process imports webapp.app next,
    # which itself requires FastAPI and therefore cannot silently start without
    # the dependency.
    pass
else:
    from webapp.auth_boundary import install_authentication_boundary
    from webapp.cache_security import install_response_cache_guard

    install_authentication_boundary()
    install_response_cache_guard()

__all__ = ["RUNTIME_SECURITY"]
