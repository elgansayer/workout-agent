"""Security bootstrap for the internal workout web application.

Importing the package validates the runtime authentication boundary before any
route, database, or OAuth setup can occur, then installs process-wide guards so
personalised responses require authentication and remain private by default.
"""

from __future__ import annotations

from webapp.runtime_security import WebRuntimeSecurity, validate_web_runtime

RUNTIME_SECURITY: WebRuntimeSecurity = validate_web_runtime()

from webapp.auth_boundary import install_authentication_boundary
from webapp.cache_security import install_response_cache_guard

install_authentication_boundary()
install_response_cache_guard()

__all__ = ["RUNTIME_SECURITY"]
