"""Tenant-resolution policy shared by every process that touches user data.

Workout Agent is multi-user. Every read and write must act for an explicit,
authenticated user. Earlier single-user releases silently fell back to a global
"legacy" tenant whenever a caller omitted a user identifier; that fallback must
not exist in production.

The only remaining implicit tenant is the *local single-user mode* used by
development and the automated test-suite. It is available when, and only when,
both of the following are set explicitly:

* ``APP_ENV`` is a recognised non-production value, and
* ``ALLOW_ANONYMOUS_WEB`` is truthy.

An unset or unrecognised ``APP_ENV`` is treated like production, so a
misconfigured container fails closed instead of selecting a shared tenant.

This module is deliberately a dependency-free leaf so persistence, web, agent
and scheduler code can all share one definition without import cycles.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# Mirrors ``webapp.runtime_security``; ``test_tenancy`` asserts the two stay in
# sync so the web boundary and the persistence layer cannot disagree about what
# counts as production.
PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})
NON_PRODUCTION_ENVIRONMENTS = frozenset(
    {"dev", "development", "local", "test", "testing"}
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: Reserved address of the placeholder account that holds data written before
#: accounts existed. It is never a login identity: it carries no external
#: identity and, in production, is kept in the ``unclaimed`` status until an
#: operator assigns its data to a verified owner (see ``legacy_ownership``).
LEGACY_TENANT_EMAIL = "legacy@local"


class TenantRequiredError(RuntimeError):
    """An operation needed an explicit tenant and none could be established."""


def implicit_single_user_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether callers may omit ``user_id`` and use the local tenant.

    Production, an unset ``APP_ENV`` and any unrecognised ``APP_ENV`` always
    return ``False``. The function never raises: a malformed value simply
    keeps the fallback disabled.
    """

    source = os.environ if environ is None else environ
    app_env = source.get("APP_ENV", "").strip().lower()
    if app_env not in NON_PRODUCTION_ENVIRONMENTS:
        return False
    return source.get("ALLOW_ANONYMOUS_WEB", "").strip().lower() in _TRUE_VALUES


def normalise_email(email: str) -> str:
    """Return the canonical form used to compare account email addresses."""

    return email.strip().lower()


__all__ = [
    "LEGACY_TENANT_EMAIL",
    "NON_PRODUCTION_ENVIRONMENTS",
    "PRODUCTION_ENVIRONMENTS",
    "TenantRequiredError",
    "implicit_single_user_allowed",
    "normalise_email",
]
