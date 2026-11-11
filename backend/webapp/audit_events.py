"""Turn an authenticated HTTP request into an audited security event.

Route handlers call :func:`audit_request_event` after the action they perform.
The tenant (``user_id``) is always supplied by the caller from the signed
session, never read from a request body, query string or header.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from fastapi import Request
from security_alerts import (
    PRODUCTION_ENVIRONMENTS,
    evaluate_runtime_posture,
    record_and_evaluate,
)
from security_audit import (
    ActorType,
    AuditEventType,
    AuditKey,
    AuditOutcome,
    AuditSubjectType,
    SecurityAuditConfigurationError,
    SecurityEvent,
    resolve_audit_key,
)

from webapp.auth_boundary import has_verified_identity
from webapp.request_correlation import get_request_id

logger = logging.getLogger(__name__)


def _verified_session_user(request: Request) -> str | None:
    session = request.scope.get("session")
    if isinstance(session, Mapping) and has_verified_identity(session):
        return str(session["user_id"])
    return None


def _source_ref(request: Request, key: AuditKey) -> str | None:
    # ProxySecurityMiddleware replaces ``client`` with the validated client
    # address, so forwarding headers are never trusted here.
    client = getattr(request, "client", None)
    if client is None or not client.host:
        return None
    return key.pseudonym("ip", client.host)


def audit_request_event(
    request: Request,
    db_path: str,
    event_type: AuditEventType,
    outcome: AuditOutcome,
    *,
    subject_type: AuditSubjectType,
    subject_id: str | None = None,
    user_id: str | None = None,
    attempted_identity: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Record one event for *request*. Never raises into the route handler.

    ``attempted_identity`` is the e-mail address presented at sign-in. It is
    stored only as a keyed pseudonym, as the event subject, so repeated attempts
    against one identity can be correlated without retaining the address.
    """

    try:
        key = resolve_audit_key()
    except SecurityAuditConfigurationError:
        logger.exception("security_audit_key_invalid event_type=%s", event_type.value)
        return

    session_user = _verified_session_user(request)
    if attempted_identity is not None:
        subject_type = AuditSubjectType.IDENTITY
        subject_id = key.pseudonym("identity", attempted_identity.strip().lower())

    record_and_evaluate(
        db_path,
        SecurityEvent(
            event_type=event_type,
            outcome=outcome,
            actor_type=ActorType.USER if session_user else ActorType.ANONYMOUS,
            actor_id=session_user,
            user_id=user_id or session_user,
            subject_type=subject_type,
            subject_id=subject_id,
            request_id=get_request_id(request),
            source_ref=_source_ref(request, key),
            metadata=metadata or {},
        ),
        key=key,
    )


def audit_runtime_posture(
    db_path: str,
    *,
    authentication_enabled: bool,
    anonymous_enabled: bool,
    detected_by: str,
    request: Request | None = None,
) -> None:
    """Raise the production-auth alert when the live process posture is unsafe.

    The posture is read from the same configuration the request path actually
    uses, not from ``validate_web_runtime``, so a regression in that validator
    cannot silence the tripwire. Non-production processes return immediately.
    """

    environment = os.environ.get("APP_ENV", "")
    if environment.strip().lower() not in PRODUCTION_ENVIRONMENTS:
        return
    try:
        key = resolve_audit_key()
    except SecurityAuditConfigurationError:
        logger.exception("security_audit_key_invalid detected_by=%s", detected_by)
        return
    evaluate_runtime_posture(
        db_path,
        environment=environment,
        authentication_enabled=authentication_enabled,
        anonymous_enabled=anonymous_enabled,
        detected_by=detected_by,
        request_id=get_request_id(request) if request is not None else None,
        source_ref=_source_ref(request, key) if request is not None else None,
        key=key,
    )
