"""Security bootstrap for the internal workout web application.

Importing the package validates the runtime authentication boundary before any
route, database, or OAuth setup can occur, then installs fail-safe response
cache and state-changing-request guards for every FastAPI application created
by this process.
"""

from __future__ import annotations

from webapp.runtime_security import WebRuntimeSecurity, validate_web_runtime

RUNTIME_SECURITY: WebRuntimeSecurity = validate_web_runtime()

from webapp.cache_security import install_response_cache_guard
from webapp.mutation_security import install_mutation_security_guard

install_response_cache_guard()
install_mutation_security_guard()

__all__ = ["RUNTIME_SECURITY"]
