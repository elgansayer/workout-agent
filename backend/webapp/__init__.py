"""Internal web app package for the workout agent.

Importing the package validates the runtime security boundary before any route,
database, or OAuth setup can occur.
"""

from __future__ import annotations

from webapp.runtime_security import WebRuntimeSecurity, validate_web_runtime

RUNTIME_SECURITY: WebRuntimeSecurity = validate_web_runtime()

__all__ = ["RUNTIME_SECURITY"]
