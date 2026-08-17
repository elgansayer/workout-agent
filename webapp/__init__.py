"""Internal web app package for the workout agent."""

from __future__ import annotations

from webapp.security import validate_current_environment

# Validate before any route, database, OAuth, or connector module is imported.
validate_current_environment()
