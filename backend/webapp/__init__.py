"""Security bootstrap for the internal workout web application.

Importing the package always validates the runtime authentication boundary.
When the optional web dependencies are installed it also installs fail-safe
response-cache and state-changing-request guards. Focused runtime-security
checks intentionally do not install FastAPI, so optional web guard installation
must stay lazy in those environments.
"""

from __future__ import annotations

from webapp.runtime_security import WebRuntimeSecurity, validate_web_runtime

RUNTIME_SECURITY: WebRuntimeSecurity = validate_web_runtime()

try:
    import fastapi as _fastapi  # noqa: F401
except ModuleNotFoundError:
    # Core agent and focused runtime-security tooling do not install the
    # optional web requirements. A real web process imports webapp.app next,
    # which itself requires FastAPI and therefore cannot silently start without
    # the dependency.
    pass
else:
    from webapp.cache_security import install_response_cache_guard
    from webapp.mutation_security import install_mutation_security_guard

    install_response_cache_guard()
    install_mutation_security_guard()

__all__ = ["RUNTIME_SECURITY"]
