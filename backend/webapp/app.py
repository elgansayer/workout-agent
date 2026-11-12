"""Internal progressive-overload web app (FastAPI).

A self-hosted dashboard for the workout agent. It reads the same SQLite database
the agent writes and renders it as a rich control centre: today's session and
progressive-overload targets, server-rendered SVG charts of every lift and your
body composition, all-time personal records, training-load trends, a consistency
calendar, the full periodisation plan, and programme check-ins.

Google OAuth login is available when WEB_AUTH_SECRET is configured, providing
user-scoped data isolation. When auth is disabled, it is read-only and meant to
sit behind a reverse proxy on a trusted host (e.g. Apache -> Docker ->
gym.example.com). All motivation is automated; nothing here calls out to an API
on a page view.

Run locally:   APP_ENV=development ALLOW_ANONYMOUS_WEB=1 \
                   uvicorn webapp.secure_app:app --reload
In a container: see Dockerfile.web / the `web` service in docker-compose.yml
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import account_deletion
import analytics
import insights
import lifestyle
from account_policy import SESSION_EPOCH_KEY, AccessPolicy, AccessPolicyConfig
from accounts import (
    AccountError,
    require_active_user,
    resolve_oauth_user,
)
from ai_provider import AIProvider, available_providers
from ai_resolver import resolve_provider
from authlib.integrations.starlette_client import OAuth
from coach_capability import (
    CoachCapabilityState,
    capability_prompt_block,
    derive_coach_capability,
)
from coaching_capabilities import (
    classify_coaching_query,
    get_coaching_capabilities,
)
from coaching_evidence import (
    build_evidence_bundle,
    evidence_prompt,
    resolve_grounding,
    serialise_bundle,
    validate_citations,
)
from config import Config
from connector_operations import (
    OperationBusyError,
    OperationPhase,
    clear_sync_outcome,
    tracked_operation,
)
from connectors.base import ConnectorContext
from connectors.builtin import build_builtin_registry
from database import (
    clear_all_notifications,
    clear_chat_messages,
    correct_body_metrics,
    delete_hevy_connection,
    delete_notification,
    get_active_programme,
    get_body_metrics,
    get_cached_workout_payloads,
    get_chat_messages,
    get_checkins,
    get_daily_logs,
    get_dashboard_insight,
    get_exercise_volumes,
    get_hevy_connection,
    get_legacy_user_id,
    get_meta,
    get_notifications,
    get_personal_records,
    get_progress_history,
    get_provenance_summary,
    get_reasoning_log,
    get_recent_bests,
    get_recent_hevy_logs,
    get_record_lineage,
    get_record_provenance,
    get_routine_match_overrides,
    get_session_volumes,
    get_training_rules,
    get_unread_notification_count,
    get_user_by_id,
    get_user_preferences,
    init_db,
    mark_all_notifications_read,
    mark_notification_read,
    save_chat_message,
    save_hevy_connection,
    save_notification,
    save_push_subscription,
    save_reasoning_log,
    save_routine_match_override,
    save_routine_snapshots,
    save_routine_workout_matches,
    save_training_rules,
    save_user_preferences,
    set_meta,
)
from dynamic_programme import (
    MAX_DURATION_WEEKS,
    MIN_DURATION_WEEKS,
    ProgrammeActivationRequest,
    ProgrammePreviewRequest,
    build_programme_preview,
    detect_source_drift,
    goal_options,
    serialise_hevy_source,
)
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from google_health_auth import build_authorize_url, exchange_code
from health_connection_store import get_connection, list_connections
from health_disconnect import disconnect_connector
from health_purge import purge_imported_data_at
from health_retention import (
    DisconnectMode,
    DisconnectRequest,
    parse_mode,
    retention_options_payload,
)
from hevy_client import HevyAuthError, HevyError, HevyRateLimitError
from hevy_connection import (
    HevyCredentialState,
    HevyCredentialTest,
    verify_hevy_credentials,
)
from hevy_history import analyse_history, build_snapshot
from hevy_parser import normalise_name
from hevy_reader import HevySourceSnapshot, HevyTrainingData
from localisation import (
    convert_weight,
    format_weight,
    load_step,
    local_today,
    prompt_context,
    spelling_for,
    weight_unit,
)
from onboarding import (
    OnboardingError,
    build_onboarding,
    resume_step,
    skip_step,
)
from programme_service import (
    ProgrammeConflictError,
    ProgrammeValidationError,
)
from programme_service import (
    activate_programme as activate_generated_programme,
)
from programme_validator import validate_programme
from provenance import (
    RETRIEVAL_GUIDANCE,
    RecordType,
    ai_provenance,
    annotate_for_retrieval,
    programme_view,
)
from pydantic import BaseModel, ConfigDict, Field
from secret_vault import (
    LEGACY_GOOGLE_HEALTH_META_KEY,
    CredentialExistsError,
    CredentialNotFoundError,
    CredentialSummary,
    CredentialUnavailableError,
    InvalidSecretError,
    ResolvedCredential,
    SecretVault,
    UnknownProviderError,
    UnknownUserError,
    VaultError,
    VaultUnavailableError,
    VerificationResult,
    VerificationUnsupportedError,
    migrate_legacy_credentials,
    provider_spec,
)
from security_audit import (
    AuditEventType,
    AuditOutcome,
    AuditSubjectType,
    resolve_audit_key,
)
from session_registry import (
    SESSION_COOKIE_KEY,
    approximate_location,
    current_session_row_id,
    describe_device,
    list_user_sessions,
    request_metadata,
    revoke_other_sessions,
    revoke_session,
    rotate_session,
)
from setup_state import (
    AccountSetup,
    AiProviderStatus,
    ProviderState,
    ai_provider_status,
    build_account_setup,
    surface_setup_payload,
)
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from tenancy import implicit_single_user_allowed
from tenant_isolation import OwnerIdentity, OwnershipError, owner_scoped_key
from training_rules import (
    StructuredTrainingRules,
    format_rules_for_prompt,
)
from user_settings import (
    SettingsValidationError,
    UserNotFoundError,
    UserSettings,
    get_user_settings,
    settings_options,
    update_user_settings,
)

from webapp import ai_widgets, charts
from webapp.audit_events import audit_request_event, audit_runtime_posture
from webapp.auth_boundary import has_verified_identity
from webapp.request_correlation import RequestCorrelationMiddleware
from webapp.runtime_security import validate_web_runtime
from webapp.session_security import (
    HardenedSessionMiddleware,
    SessionLifecycle,
    SessionStore,
    load_session_security_config,
)

DB_PATH = os.environ.get("DATABASE_PATH", "workout_agent.db").strip()
logger = logging.getLogger(__name__)
_WEB_RUNTIME = validate_web_runtime()
_SESSION_LIFETIME_SECONDS = 14 * 24 * 60 * 60


def _vault() -> SecretVault:
    """Construct the narrow credential service for the current database."""
    return SecretVault(DB_PATH)


def _vault_credential(
    user_id: str,
    provider: str,
    *,
    purpose: str,
) -> ResolvedCredential | None:
    """Return the owner's credential or ``None`` when it is not configured."""
    try:
        return _vault().resolve(user_id, provider, purpose=purpose)
    except CredentialUnavailableError:
        return None
    except VaultUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _verify_hevy_credential(credential: ResolvedCredential) -> VerificationResult:
    from hevy_client import get_workout_count

    key = credential.api_key.reveal() if credential.api_key is not None else ""
    if not key:
        return VerificationResult(False, "not_configured", "No Hevy key provided.")
    count = get_workout_count(key)
    if count is None:
        return VerificationResult(
            False,
            "unreachable",
            "Could not connect to Hevy. Check the API key.",
        )
    return VerificationResult(
        True,
        "ok",
        f"Connected to Hevy ({count} workouts).",
        {"workout_count": count},
    )


def _verify_ai_credential(credential: ResolvedCredential) -> VerificationResult:
    from ai_provider import get_provider

    key = credential.api_key.reveal() if credential.api_key is not None else ""
    if not key:
        return VerificationResult(False, "not_configured", "No API key provided.")
    try:
        provider = get_provider(
            credential.provider,
            api_key=key,
            model=credential.model,
        )
        response = provider.generate("Reply with the single word OK.")
        if not isinstance(response, str):
            response = "".join(response)
        if response.strip():
            return VerificationResult(True, "ok", "The provider accepted the key.")
    except Exception as exc:  # noqa: BLE001 - provider SDKs raise arbitrary types
        logger.warning(
            "AI credential test failed for %s (%s).",
            credential.provider,
            type(exc).__name__,
        )
    return VerificationResult(
        False,
        "rejected",
        "The provider rejected the key or could not be reached.",
    )


def _verifier_for(provider: str) -> Callable[[ResolvedCredential], VerificationResult]:
    spec = provider_spec(provider)
    if not spec.testable:
        raise VerificationUnsupportedError("This credential cannot be tested.")
    if spec.provider == "hevy":
        return _verify_hevy_credential
    return _verify_ai_credential


def _test_credential(
    user_id: str,
    provider: str,
    api_key: str,
    model: str | None,
) -> dict[str, Any]:
    vault = _vault()
    verifier = _verifier_for(provider)
    if api_key and api_key != "__stored__":
        result = vault.verify_candidate(
            user_id,
            provider,
            verifier=verifier,
            api_key=api_key,
            model=model,
        )
    else:
        result = vault.verify_stored(user_id, provider, verifier=verifier)
    payload: dict[str, Any] = {
        "status": "ok" if result.ok else "error",
        "provider": provider,
        "code": result.code,
        "detail": result.message,
    }
    workout_count = result.details.get("workout_count")
    if isinstance(workout_count, int):
        payload["workout_count"] = workout_count
    return payload


def _vault_http_error(exc: VaultError) -> HTTPException:
    if isinstance(exc, (UnknownProviderError, InvalidSecretError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, CredentialNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, CredentialExistsError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, VaultUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, (CredentialUnavailableError, UnknownUserError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail="The credential operation failed.")


def _settings_key_payload(summary: CredentialSummary) -> dict[str, Any]:
    """Public credential metadata; never contains secret material."""
    return summary.to_public_dict()


def _regional_prompt_context(user_id: str) -> str:
    """The acting user's time zone, locale and unit instructions for a prompt."""
    settings = get_user_settings(user_id, DB_PATH)
    return prompt_context(settings.timezone, settings.locale, settings.unit_system)


def _resolve_provider_for_request(request: Request, config: Config) -> AIProvider:
    """Resolve the authenticated user's provider without caching credentials."""
    user_id = _check_api_auth(request)
    try:
        return resolve_provider(
            user_id=user_id,
            server_gemini_key=config.gemini_api_key,
            server_gemini_model=config.gemini_model,
            db_path=DB_PATH,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ImportError as exc:
        logger.exception("The selected AI provider SDK is unavailable.")
        raise HTTPException(
            status_code=503,
            detail="The selected AI provider is unavailable on this server.",
        ) from exc


def get_config():
    return Config.load()


_RATE_LIMITS: dict[str, list[float]] = {}


def _rate_limit_key(request: Request) -> str:
    """Return the throttle bucket for *request*.

    A signed-in request is throttled per owner, so one tenant's traffic (or a
    spoofed ``X-Forwarded-For``) cannot exhaust a neighbour's quota behind the
    same proxy or NAT. Only a request with no session identity falls back to
    the client address.
    """
    session = request.session if hasattr(request, "session") else {}
    user = session.get("user")
    candidate = session.get("user_id") or (
        user.get("id") if isinstance(user, dict) else None
    )
    if candidate:
        try:
            return owner_scoped_key(OwnerIdentity(str(candidate)), "rate-limit")
        except OwnershipError:
            logger.warning("Throttling a malformed session identity by address.")

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    elif request.headers.get("x-real-ip"):
        ip = request.headers.get("x-real-ip").strip()  # type: ignore[union-attr]
    else:
        ip = request.client.host if request.client else "unknown"
    return f"rate-limit-ip:{ip}"


def _check_rate_limit(request: Request, limit: int = 10, window: int = 60) -> None:
    now = time.time()
    key = _rate_limit_key(request)

    if key not in _RATE_LIMITS:
        _RATE_LIMITS[key] = []
    _RATE_LIMITS[key] = [t for t in _RATE_LIMITS[key] if now - t < window]
    if len(_RATE_LIMITS[key]) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    _RATE_LIMITS[key].append(now)


WEB_GOOGLE_CLIENT_ID = os.environ.get("WEB_GOOGLE_CLIENT_ID", "").strip()
WEB_GOOGLE_CLIENT_SECRET = os.environ.get("WEB_GOOGLE_CLIENT_SECRET", "").strip()
WEB_AUTH_SECRET = os.environ.get("WEB_AUTH_SECRET", "").strip()
ALLOW_ANONYMOUS_WEB = not bool(WEB_GOOGLE_CLIENT_ID)

# The one place the deployment's access policy (ALLOWED_EMAILS, INVITE_ONLY and
# the stored account status) is composed. Login completion below and the global
# authentication boundary both decide through this object; an invalid setting
# raises here so the server refuses to start rather than run unrestricted.
ACCESS_POLICY = AccessPolicy(
    AccessPolicyConfig.from_environ(
        {
            "ALLOWED_EMAILS": os.environ.get("ALLOWED_EMAILS", ""),
            "INVITE_ONLY": os.environ.get("INVITE_ONLY", ""),
        }
    ),
    DB_PATH,
)

# Canonical allow-list addresses, or ``None`` when no allowlist is configured.
# Derived from the composed policy so login and the per-request session check
# never disagree about which addresses are admitted.
ALLOWED_EMAILS = ACCESS_POLICY.config.allowed_emails

# Fixed, content-free destinations for a refused sign-in. They are identical for
# every denial reason so a response cannot reveal why an address was refused.
_LOGIN_DENIED_URL = "/login?error=access_denied"
_LOGIN_UNAVAILABLE_URL = "/login?error=unavailable"

# Cookie attributes, idle/absolute lifetimes and the server-side session
# registry. Validated at import so unsafe limits stop startup (issue #752).
SESSION_SECURITY = load_session_security_config()
if _WEB_RUNTIME.production and not SESSION_SECURITY.cookie.secure:
    raise RuntimeError("Production sessions must use Secure cookies.")
SESSION_STORE = SessionStore(DB_PATH, SESSION_SECURITY.lifetime)
SESSION_LIFECYCLE = SessionLifecycle(SESSION_STORE)


def _identity_allowed(email: object) -> bool:
    """Re-apply the e-mail allow-list to sessions that already exist.

    Removing an address from ``ALLOWED_EMAILS`` therefore ends its sessions on
    their next request instead of leaving them valid until they age out.
    """
    if not ALLOWED_EMAILS:
        return True
    return isinstance(email, str) and email in ALLOWED_EMAILS


def _audit(
    request: Request,
    event_type: AuditEventType,
    outcome: AuditOutcome,
    subject_type: AuditSubjectType,
    subject_id: str | None = None,
    *,
    user_id: str | None = None,
    attempted_identity: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a security event for this request; audit failures never reach callers."""
    audit_request_event(
        request,
        DB_PATH,
        event_type,
        outcome,
        subject_type=subject_type,
        subject_id=subject_id,
        user_id=user_id,
        attempted_identity=attempted_identity,
        metadata=metadata,
    )


def _audit_posture(detected_by: str, request: Request | None = None) -> None:
    """Alert if a production-configured process is running without complete auth."""
    audit_runtime_posture(
        DB_PATH,
        authentication_enabled=bool(
            WEB_AUTH_SECRET and WEB_GOOGLE_CLIENT_ID and WEB_GOOGLE_CLIENT_SECRET
        ),
        anonymous_enabled=ALLOW_ANONYMOUS_WEB,
        detected_by=detected_by,
        request=request,
    )


def _enforce_account_gate(request: Request, uid: Any, *, allow_closing: bool) -> None:
    """Fail closed for accounts that deletion has locked or closed.

    A closed (tombstoned) account's signed cookie is stateless, so it is rejected
    here on every request and the session is cleared. While erasure runs the
    account is locked; only the deletion status endpoint may pass
    ``allow_closing`` so progress stays visible.
    """
    if not isinstance(uid, str):
        raise HTTPException(status_code=401, detail="Unauthorized")
    gate = account_deletion.account_gate(DB_PATH, uid)
    if gate is account_deletion.AccountGate.CLOSED:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Unauthorized")
    if gate is account_deletion.AccountGate.LOCKED and not allow_closing:
        raise HTTPException(status_code=423, detail="Account deletion is in progress.")


def _check_api_auth(request: Request, *, allow_closing: bool = False) -> str:
    """Return the authenticated, active account id for this request.

    A signed session is not enough on its own: the account it names must still
    exist and be ``active``, so suspending or removing an account takes effect on
    its very next request. The only unauthenticated path is the explicit
    non-production local mode; production has no shared fallback user.
    """
    session = request.session if hasattr(request, "session") else {}
    user = session.get("user")
    uid = session.get("user_id")
    if not uid and isinstance(user, dict) and user.get("id"):
        uid = user["id"]
    if uid:
        _enforce_account_gate(request, uid, allow_closing=allow_closing)
        try:
            return require_active_user(str(uid), DB_PATH)["id"]
        except AccountError as exc:
            raise HTTPException(status_code=401, detail="Unauthorized") from exc
    if ALLOW_ANONYMOUS_WEB:
        # Never reachable in a correctly configured production process; if it is,
        # raise the critical "production auth disabled" alert and fail closed.
        _audit_posture("anonymous_request", request)
        if implicit_single_user_allowed():
            return get_legacy_user_id(DB_PATH)
    raise HTTPException(status_code=401, detail="Unauthorized")


def _request_session_metadata(request: Request) -> tuple[str, str, str]:
    validated_ip = getattr(request.state, "validated_client_ip", "")
    client_ip = str(validated_ip or (request.client.host if request.client else ""))
    return request_metadata(request.headers, client_ip)


def _current_session_id(request: Request) -> str | None:
    session_id = request.session.get(SESSION_COOKIE_KEY)
    return session_id if isinstance(session_id, str) and session_id else None


def _account_setup(user_id: str) -> AccountSetup:
    """Derive this tenant's account setup state from their own records."""

    return build_account_setup(
        user_id=user_id,
        db_path=DB_PATH,
        server_ai_key=os.environ.get("GEMINI_API_KEY", "").strip() or None,
    )


def _setup_payload(
    user_id: str,
    surface: str,
    *,
    programme_status: str | None = None,
) -> dict[str, Any]:
    """Return the setup contract for one surface of this tenant."""

    return surface_setup_payload(
        _account_setup(user_id),
        surface,
        programme_status=programme_status,
    )


def _onboarding_payload(user_id: str) -> dict[str, Any]:
    """Return this tenant's resumable first-run setup state."""

    return build_onboarding(
        user_id=user_id,
        db_path=DB_PATH,
        server_ai_key=os.environ.get("GEMINI_API_KEY", "").strip() or None,
    ).as_dict()


# Google Health linking is opt-in: set the OAuth client in the web service's
# environment to enable the "Connect Google Health" button on the Settings page.
GH_CLIENT_ID = os.environ.get("GOOGLE_HEALTH_CLIENT_ID", "").strip()
GH_CLIENT_SECRET = os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET", "").strip()
# Optional explicit redirect URI (recommended behind a reverse proxy so the
# scheme/host match exactly what is registered with Google). When unset it is
# derived from the incoming request.
GH_REDIRECT_URI = os.environ.get("GOOGLE_HEALTH_REDIRECT_URI", "").strip()
_GH_STATE_KEY = "google_health_oauth_state"

oauth = OAuth()
if WEB_GOOGLE_CLIENT_ID and WEB_GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=WEB_GOOGLE_CLIENT_ID,
        client_secret=WEB_GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

_BASE_DIR = Path(__file__).resolve().parent

# Cache-busting for static assets. Cloudflare (and browsers) cache /static/*
# aggressively, so a plain "/static/style.css" can serve a stale copy for hours
# after a deploy. We append a short content hash (?v=...) so every change yields
# a brand-new URL the cache has never seen, while unchanged files stay cached.
_ASSET_VERSIONS: dict[str, str] = {}


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Safe even if the agent already created the database (CREATE IF NOT EXISTS).
    init_db(DB_PATH)
    # Fail startup on a malformed AUDIT_LOG_HMAC_KEY instead of silently running
    # without a usable audit chain.
    resolve_audit_key()
    _audit_posture("startup")
    # Re-seal any pre-vault Fernet/plaintext rows under per-user encryption.
    migrate_legacy_credentials(DB_PATH)
    if not ALLOW_ANONYMOUS_WEB:
        logger.info("Account access policy: %s", ACCESS_POLICY.config.describe())
        ended = ACCESS_POLICY.reconcile()
        if ended:
            logger.info(
                "Ended sessions for %d account(s) outside the configured allowlist.",
                ended,
            )
    yield


# Determine frontend dist path
# Locally: root/frontend/dist/...
# Docker: /app/frontend/dist/...
if (_BASE_DIR.parent / "frontend/dist/frontend/browser").exists():
    FRONTEND_DIST = _BASE_DIR.parent / "frontend/dist/frontend/browser"  # Docker
else:
    FRONTEND_DIST = (
        _BASE_DIR.parent.parent / "frontend/dist/frontend/browser"
    )  # Local dev
app = FastAPI(title="Workout Agent", docs_url=None, redoc_url=None, lifespan=_lifespan)
# Read by the authentication boundary when it builds its middleware stack, so the
# per-request session check uses the same policy and database as login does.
app.state.access_policy = ACCESS_POLICY
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")
app.mount(
    "/assets",
    StaticFiles(directory=str(FRONTEND_DIST), check_dir=False),
    name="angular_assets",
)

# Enable CORS for external/remote frontend hosting
_cors_origins_raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
_cors_origins = (
    [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    if _cors_origins_raw
    else [
        "http://localhost:4200",
        "http://127.0.0.1:4200",
        "http://localhost:8770",
        "http://127.0.0.1:8770",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:3000",
    ]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if WEB_AUTH_SECRET and WEB_GOOGLE_CLIENT_ID:

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            path = request.url.path
            # Allow static files, assets, JS/CSS/image/font assets, and auth endpoints
            if (
                path.startswith(("/static", "/assets"))
                or path.endswith(
                    (
                        ".js",
                        ".css",
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".svg",
                        ".ico",
                        ".woff",
                        ".woff2",
                        ".ttf",
                        ".map",
                        ".json",
                        ".webmanifest",
                    )
                )
                or path
                in [
                    "/login",
                    "/login/google",
                    "/logout",
                    "/auth",
                    
                    "/favicon.ico",
                    "/sw.js",
                ]
            ):
                return await call_next(request)
            if not request.session.get("user"):
                # API routes: return 401 JSON instead of a redirect so fetch()
                # callers get a clear error instead of the login page HTML.
                if path.startswith("/api/"):
                    return HTMLResponse(
                        '{"detail":"Not authenticated"}',
                        status_code=401,
                        media_type="application/json",
                    )
                return RedirectResponse("/login")
            return await call_next(request)

    app.add_middleware(AuthMiddleware)

# Always mount the session middleware so templates and routes can safely access
# request.session even when web auth is disabled. Without it, any access to
# request.session raises an AssertionError -> 500 Internal Server Error.
# When WEB_AUTH_SECRET is unset we fall back to a per-process random key; this
# is fine because there is nothing sensitive to protect when auth is off.
#
# The cookie is HttpOnly (Starlette default), host-only, SameSite=Lax and bounded
# to the same 14-day lifetime the CSRF token signer accepts. In production the
# runtime boundary requires HTTPS, so the cookie is also marked Secure and can
# never be sent over a plaintext connection.
# HardenedSessionMiddleware is Starlette's SessionMiddleware with a Secure /
# HttpOnly / SameSite / host-only cookie and a server-side registry: a session
# is honoured only while it is unexpired (idle and absolute), unrevoked and
# owned by the user its cookie claims. See docs/SESSION_SECURITY.md.
app.add_middleware(
    HardenedSessionMiddleware,
    secret_key=WEB_AUTH_SECRET or secrets.token_hex(32),
    config=SESSION_SECURITY,
    store=SESSION_STORE,
    identity_allowed=_identity_allowed,
)
# Outermost: every response carries X-Request-ID and every audit event written
# while serving the request shares that correlation id.
app.add_middleware(RequestCorrelationMiddleware)


@app.get("/login/google")
async def login_google(request: Request) -> Any:
    if not WEB_GOOGLE_CLIENT_ID:
        return HTMLResponse("Web auth is not configured.", status_code=500)
    redirect_uri = str(request.url_for("auth"))
    if "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    # Legacy navigation endpoint, kept for bookmarks. A state change on GET must
    # not be triggerable from another site, so only a browser-reported
    # same-origin or user-typed navigation signs out. Cross-site and same-site
    # (sibling subdomain) navigations are ignored; a browser that sends no
    # Fetch Metadata is allowed. The SPA signs out with POST /api/auth/logout,
    # which is CSRF protected.
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return RedirectResponse("/")
    # Audit before clearing so the event is attributed to the revoked session's
    # user. An anonymous visit has nothing to revoke and is not recorded.
    if has_verified_identity(request.session):
        _audit(
            request,
            AuditEventType.SESSION_REVOKED,
            AuditOutcome.SUCCESS,
            AuditSubjectType.SESSION,
            metadata={"reason": "logout"},
        )
    await run_in_threadpool(SESSION_LIFECYCLE.end, request.session)
    return RedirectResponse("/login")


@app.post("/api/auth/logout")
def api_logout(request: Request) -> dict[str, str]:
    """Revoke this browser's session server-side and delete its cookie."""
    SESSION_LIFECYCLE.end(request.session)
    return {"status": "ok"}


@app.post("/api/auth/logout-all")
def api_logout_all(request: Request) -> dict[str, Any]:
    """Revoke every session of the authenticated user, on every device.

    The owner comes from the verified session, never from the request, so one
    tenant can never end another tenant's sessions.
    """
    user_id = _check_api_auth(request)
    revoked = SESSION_LIFECYCLE.end_all(request.session, user_id)
    return {"status": "ok", "revoked": revoked}


@app.get("/api/me")
async def get_current_user_api(request: Request) -> Any:
    user = request.session.get("user")
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        # A session whose account was suspended or removed is no longer a login.
        # A closing account stays visible so the SPA can show deletion progress.
        _check_api_auth(request, allow_closing=True)
    except HTTPException:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({"user": user})


@app.get("/api/sessions")
def api_list_sessions(request: Request) -> dict[str, Any]:
    user_id = _check_api_auth(request)
    sessions = list_user_sessions(
        user_id,
        _current_session_id(request),
        DB_PATH,
    )
    return {"sessions": sessions}


@app.delete("/api/sessions/{session_id}")
def api_revoke_session(request: Request, session_id: int) -> dict[str, Any]:
    user_id = _check_api_auth(request)
    current_id = current_session_row_id(
        user_id,
        _current_session_id(request),
        DB_PATH,
    )
    if not revoke_session(user_id, session_id, DB_PATH):
        raise HTTPException(status_code=404, detail="Not found")

    if current_id == session_id:
        request.session.clear()
        return {"status": "ok", "logged_out": True}

    user_agent, client_ip, country_code = _request_session_metadata(request)
    rotated = rotate_session(
        user_id,
        _current_session_id(request),
        user_agent=user_agent,
        client_ip=client_ip,
        country_code=country_code,
        db_path=DB_PATH,
    )
    if rotated:
        request.session[SESSION_COOKIE_KEY] = rotated["sid"]
    return {
        "status": "ok",
        "logged_out": False,
        "current_session_id": rotated["id"] if rotated else current_id,
    }


@app.post("/api/sessions/revoke-others")
def api_revoke_other_sessions(request: Request) -> dict[str, Any]:
    user_id = _check_api_auth(request)
    session_id = _current_session_id(request)
    revoked = revoke_other_sessions(user_id, session_id, DB_PATH)

    user_agent, client_ip, country_code = _request_session_metadata(request)
    rotated = rotate_session(
        user_id,
        session_id,
        user_agent=user_agent,
        client_ip=client_ip,
        country_code=country_code,
        db_path=DB_PATH,
    )
    if rotated:
        request.session[SESSION_COOKIE_KEY] = rotated["sid"]
    return {
        "status": "ok",
        "revoked": revoked,
        "current_session_id": rotated["id"] if rotated else None,
    }


def _claim_is_true(value: Any) -> bool:
    """Interpret an OIDC boolean claim (providers differ on bool vs string)."""
    return value is True or (isinstance(value, str) and value.lower() == "true")


@app.get("/auth")
async def auth(request: Request) -> Any:
    if not WEB_GOOGLE_CLIENT_ID:
        return RedirectResponse("/")
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:  # noqa: BLE001
        logger.warning("OAuth token exchange failed: %s", e)
        _audit(
            request,
            AuditEventType.LOGIN,
            AuditOutcome.FAILURE,
            AuditSubjectType.IDENTITY,
            metadata={"reason": "oauth_exchange_failed", "error_type": type(e).__name__},
        )
        return RedirectResponse("/login")
    userinfo = token.get("userinfo")
    claims: dict[str, Any] = userinfo if isinstance(userinfo, dict) else {}
    # A sign-in is bound to the provider's stable subject. Refuse a missing
    # subject before the access policy can create an account, so a failed
    # callback never leaves an identity-less account behind.
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        logger.warning("Sign-in rejected: the provider supplied no stable subject.")
        _audit(
            request,
            AuditEventType.LOGIN,
            AuditOutcome.FAILURE,
            AuditSubjectType.IDENTITY,
            metadata={"reason": "missing_subject"},
        )
        request.session.clear()
        return RedirectResponse(_LOGIN_DENIED_URL, status_code=303)

    # Authenticating with Google proves who the person is, not that this
    # deployment admits them. Allowlist, invitation, suspension and deletion
    # state are all decided here, server-side, before any session exists.
    try:
        decision = await run_in_threadpool(ACCESS_POLICY.admit_login, claims)
    except sqlite3.Error:
        # An unreadable policy store is an outage, not a verdict on the person:
        # refuse the sign-in (fail closed) but say so, and keep no session.
        logger.exception("Account policy lookup failed during login completion.")
        _audit(
            request,
            AuditEventType.LOGIN,
            AuditOutcome.FAILURE,
            AuditSubjectType.IDENTITY,
            metadata={"reason": "policy_lookup_failed"},
        )
        request.session.clear()
        return RedirectResponse(_LOGIN_UNAVAILABLE_URL, status_code=303)

    if not decision.allowed:
        # Always start from an empty session on denial too: drop leftover
        # pre-login state (OAuth correlation keys, a previous identity).
        _audit(
            request,
            AuditEventType.LOGIN,
            AuditOutcome.DENIED,
            AuditSubjectType.IDENTITY,
            attempted_identity=str(claims.get("email") or "") or None,
            metadata={"reason": "access_policy_denied"},
        )
        request.session.clear()
        return RedirectResponse(_LOGIN_DENIED_URL, status_code=303)

    # Bind the canonical (provider, subject) identity to the admitted account.
    # Email alone never selects an account; the identity cannot be re-pointed,
    # and a subject already bound elsewhere refuses this sign-in.
    try:
        user_record = await run_in_threadpool(
            lambda: resolve_oauth_user(
                provider="google",
                subject=subject,
                email=str(claims.get("email") or ""),
                email_verified=_claim_is_true(claims.get("email_verified")),
                display_name=claims.get("name"),
                db_path=DB_PATH,
            )
        )
    except sqlite3.Error:
        logger.exception("Identity lookup failed during login completion.")
        _audit(
            request,
            AuditEventType.LOGIN,
            AuditOutcome.FAILURE,
            AuditSubjectType.IDENTITY,
            metadata={"reason": "identity_lookup_failed"},
        )
        request.session.clear()
        return RedirectResponse(_LOGIN_UNAVAILABLE_URL, status_code=303)
    except AccountError as exc:
        # Unverified or missing claims, an identity conflict, or an identity
        # whose account no longer exists: all refuse the sign-in rather than
        # surfacing a 500. The messages never contain the email or subject.
        logger.warning("Sign-in rejected: %s", exc)
        _audit(
            request,
            AuditEventType.LOGIN,
            AuditOutcome.FAILURE,
            AuditSubjectType.IDENTITY,
            metadata={"reason": "invalid_identity"},
        )
        request.session.clear()
        return RedirectResponse(_LOGIN_DENIED_URL, status_code=303)

    if user_record["id"] != decision.user_id:
        # The stable subject names a different account than the policy admitted:
        # never mint a session from that mismatch (fail closed).
        logger.warning("Sign-in rejected: identity/policy account mismatch.")
        _audit(
            request,
            AuditEventType.LOGIN,
            AuditOutcome.DENIED,
            AuditSubjectType.IDENTITY,
            metadata={"reason": "identity_policy_mismatch"},
        )
        request.session.clear()
        return RedirectResponse(_LOGIN_DENIED_URL, status_code=303)

    # Rotate on login: a fresh session identifier is registered, whatever
    # identifier the browser held is revoked, and all pre-login session data
    # (including leftover OAuth state) is discarded.
    approved_user_id = decision.user_id
    approved_email = decision.email
    if approved_user_id is None or approved_email is None:  # pragma: no cover
        # Defensive: an allowed decision always carries both, but never mint a
        # session from a partially populated result (fail closed).
        request.session.clear()
        return RedirectResponse(_LOGIN_DENIED_URL, status_code=303)
    user_agent, client_ip, country_code = _request_session_metadata(request)
    await run_in_threadpool(
        SESSION_LIFECYCLE.establish,
        request.session,
        user_id=approved_user_id,
        email=approved_email,
        device=describe_device(user_agent),
        location=approximate_location(client_ip, country_code),
    )
    # ``establish`` clears the session, so record the policy epoch (issue #753)
    # after it; the per-request guard compares this against the stored account.
    request.session[SESSION_EPOCH_KEY] = decision.session_epoch
    _audit(
        request,
        AuditEventType.LOGIN,
        AuditOutcome.SUCCESS,
        AuditSubjectType.ACCOUNT,
        user_record["id"],
        user_id=user_record["id"],
        metadata={"method": "google_oauth"},
    )
    return RedirectResponse("/")


# Automated hype lines. One is shown per day, chosen deterministically from the
# date, so the dashboard always greets you without an API call or a button.
_QUOTES = [
    "Show up today and the work gets easier tomorrow. One more rep than last time.",
    "Strength is a skill you practise. Brace, control, repeat.",
    "Small jumps, stacked weekly, become real progress. Keep stacking.",
    "Your next personal best is built on the boring set you log today.",
    "Consistency now, results later. Hit your plan and your target.",
    "The last clean rep is where the adaptation lives. Earn it.",
    "Win the morning by winning last night's sleep.",
    "Train with intent, recover with purpose. Stay the course.",
    "You do not need motivation, you need the next set. Go.",
    "Consistency is the cheat code. You already have it.",
    "Add a rep or add a little load. Either way, move forward.",
    "Recovery is training too. Eat, sleep, repeat.",
]


def _daily_quote(today: date | None = None) -> str:
    today = today or datetime.now(tz=UTC).date()
    return _QUOTES[today.toordinal() % len(_QUOTES)]


def _epley_1rm(weight: float | None, reps: int | None) -> float | None:
    return analytics.epley_1rm(weight, reps)


def _weight_on_or_before(weights: list[tuple[str, float]], when: str) -> float | None:
    """Latest bodyweight recorded on or before a date (weights sorted ascending)."""
    result = None
    for when_w, value in weights:
        if when_w <= when:
            result = value
        else:
            break
    return result


def _find_lift_series(
    series: dict[str, Any], *keywords: str
) -> tuple[str | None, list[Any]]:
    """Find an exercise whose name contains all (then any) of the keywords."""
    for name, entries in series.items():
        low = name.lower()
        if all(k in low for k in keywords):
            return name, entries
    for name, entries in series.items():
        low = name.lower()
        if any(k in low for k in keywords):
            return name, entries
    return None, []


def _rep_top(rep_range: str) -> int | None:
    digits = [int(n) for n in rep_range.replace("-", " ").split() if n.isdigit()]
    return digits[-1] if digits else None


def _overload_nudge(
    planned_rep_range: str,
    best: dict[str, Any] | None,
    unit_system: str = UserSettings().unit_system,
) -> str:
    """Suggest the next progressive-overload step for an exercise."""
    if not best or best.get("top_reps") is None:
        return "Log this lift to start tracking progress."
    top = _rep_top(planned_rep_range)
    reps = best["top_reps"]
    weight = best.get("top_weight_kg")
    if top is not None and reps >= top:
        if weight:
            return (
                "You hit the top of the range. "
                f"Add {load_step(unit_system)} next time."
            )
        return "You hit the top of the range. Add a rep or a little load."
    if top is not None:
        return f"Chase {top} clean reps, then add load."
    return "Keep the reps strict and progress when it feels easy."


def _format_best(
    best: dict[str, Any] | None, unit_system: str = UserSettings().unit_system
) -> str:
    if not best:
        return "No data yet"
    weight = best.get("top_weight_kg")
    reps = best.get("top_reps")
    if weight is not None and reps is not None:
        return f"{format_weight(weight, unit_system)} x {reps}"
    if reps is not None:
        return f"{reps} reps"
    return "No data yet"


def _training_levels(user_id: str | None = None) -> dict[str, int]:
    """Map ISO dates to a calendar-heatmap intensity (0-4)."""
    levels: dict[str, int] = {}
    for log in get_daily_logs(limit=400, db_path=DB_PATH, user_id=user_id):
        levels[log["date"]] = 2 if log["day"] is not None else 1
    # A session with logged sets is the strongest signal of a completed workout.
    for session in get_session_volumes(db_path=DB_PATH, user_id=user_id):
        levels[session["date"]] = 4
    return levels


def _current_streak(levels: dict[str, int], today: date | None = None) -> int:
    """Count consecutive recent days that had any logged activity.

    ``today`` is the viewer's local calendar day; UTC is only the fallback.
    """
    streak = 0
    day = today or datetime.now(tz=UTC).date()
    # Allow today to be empty (the morning run may not have happened yet).
    if levels.get(day.isoformat(), 0) == 0:
        day -= timedelta(days=1)
    while levels.get(day.isoformat(), 0) > 0:
        streak += 1
        day -= timedelta(days=1)
    return streak


def _dashboard_context(
    today: date | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Build a dashboard from the active programme, or an explicit setup state."""
    from programme_runtime import load_programme_runtime, local_today

    settings = get_user_settings(user_id, DB_PATH) if user_id else UserSettings()
    timezone_name = settings.timezone
    unit_system = settings.unit_system
    today = today or local_today(timezone_name)
    bests = get_recent_bests(DB_PATH, user_id=user_id)
    bests_norm = {normalise_name(name): best for name, best in bests.items()}

    runtime = (
        load_programme_runtime(
            user_id,
            DB_PATH,
            today=today,
            timezone_name=timezone_name,
        )
        if user_id
        else None
    )
    if runtime is None:
        from programme_runtime import ProgrammeRuntime

        runtime = ProgrammeRuntime()
    setup_required = runtime.setup_required
    rows: list[dict[str, Any]] = []
    focus = "Select routines from Hevy"
    is_rest_day = True
    cycle_weeks = 0
    week = 0
    block_weeks = 1
    week_in_block = 0
    block: dict[str, Any] = {
        "number": 0,
        "name": "No active programme",
        "focus": "Connect Hevy, select routines and generate your programme.",
        "rep_emphasis": "No prescription yet",
        "target_rir": "",
        "progression": "",
    }

    if not setup_required:
        cycle_weeks = runtime.cycle_weeks
        week = runtime.week
        if runtime.block:
            block = runtime.block
            start_week = int(block.get("start_week") or 1)
            end_week = int(block.get("end_week") or start_week)
            block_weeks = max(1, end_week - start_week + 1)
            week_in_block = week - start_week + 1
        active_day = runtime.next_day
        if active_day:
            focus = str(active_day.get("focus") or "Training")
            is_rest_day = False
            for exercise in active_day.get("exercises", []):
                name = str(exercise.get("name") or "?")
                rep_range = str(exercise.get("rep_range") or "?")
                best = bests_norm.get(normalise_name(name))
                rows.append(
                    {
                        "name": name,
                        "planned": (
                            exercise.get("scheme")
                            or f"{exercise.get('sets', 0)} × {rep_range}"
                        ),
                        "note": exercise.get("note", ""),
                        "last": _format_best(best, unit_system),
                        "nudge": _overload_nudge(rep_range, best, unit_system),
                        "role": exercise.get("role"),
                    }
                )
        else:
            focus = (
                "Programme review" if runtime.status == "needs_review"
                else runtime.status.replace("_", " ").capitalize()
            )

    metrics = get_body_metrics(db_path=DB_PATH, user_id=user_id)
    latest_weight = metrics[-1]["weight_kg"] if metrics else None
    recovery_like = {"weight_kg": latest_weight} if latest_weight else None
    guidance = lifestyle.daily_guidance(
        None if is_rest_day else focus,
        is_rest_day,
        recovery_like,
    )

    levels = _training_levels(user_id=user_id)
    streak = _current_streak(levels, today)
    weight_spark = charts.sparkline(
        [metric["weight_kg"] for metric in metrics if metric["weight_kg"]]
    )
    fat_spark = charts.sparkline(
        [metric["body_fat_pct"] for metric in metrics if metric["body_fat_pct"]],
        colour=charts.WARN,
    )
    latest_fat = next(
        (
            metric["body_fat_pct"]
            for metric in reversed(metrics)
            if metric["body_fat_pct"]
        ),
        None,
    )
    review = insights.build_insights(
        get_progress_history(db_path=DB_PATH, user_id=user_id),
        metrics,
        None,
        unit_system=unit_system,
    )

    cycle_progress = week / cycle_weeks * 100 if cycle_weeks else 0
    block_progress = week_in_block / block_weeks * 100 if week_in_block else 0
    block_label = str(block.get("name") or "Setup").split(" ")[0]

    setup_payload = (
        _setup_payload(user_id, "dashboard", programme_status=runtime.status)
        if user_id
        else None
    )
    coach_setup_payload = (
        _setup_payload(user_id, "coach") if user_id else None
    )

    return {
        "active": "today",
        "programme_status": runtime.status,
        "rotation_message": runtime.message,
        "last_completed_at": runtime.last_completed_at,
        "next_routine_id": runtime.next_day.get("routine_id") if runtime.next_day else None,
        "setup_required": setup_required,
        "setup": setup_payload,
        "coach_setup": coach_setup_payload,
        "onboarding": _onboarding_payload(user_id) if user_id else None,
        "active_programme_name": runtime.name or None,
        "quote": _daily_quote(today),
        "week": week,
        "cycle_weeks": cycle_weeks,
        "block": block,
        "week_in_block": week_in_block,
        "block_weeks": block_weeks,
        "focus": focus,
        "is_rest_day": is_rest_day,
        "weekday": today.strftime("%A"),
        "rows": rows,
        "lifestyle": guidance.as_lines(),
        "cycle_ring": charts.progress_ring(
            cycle_progress,
            label=f"Wk {week}" if week else "Setup",
            sub=f"of {cycle_weeks}" if cycle_weeks else "Hevy",
        ),
        "block_ring": charts.progress_ring(
            block_progress,
            label=block_label,
            sub=(
                f"wk {week_in_block}/{block_weeks}" if week_in_block else "not active"
            ),
            colour=charts.ACCENT_2,
        ),
        "streak": streak,
        "calendar": charts.calendar_heatmap(levels),
        "weight": format_weight(latest_weight, unit_system) if latest_weight else None,
        "weight_unit": weight_unit(unit_system),
        "weight_spark": weight_spark,
        "body_fat": f"{latest_fat:g} %" if latest_fat else None,
        "fat_spark": fat_spark,
        "review_headline": review.headline,
        "review_recovery": review.recovery.as_text(),
        "review_lifts": review.lifts,
        "dashboard_insight": get_dashboard_insight(db_path=DB_PATH, user_id=user_id),
        "programme_provenance": (
            programme_view(get_active_programme(user_id, db_path=DB_PATH))
            if user_id
            else None
        ),
        "dashboard_insight_provenance": (
            get_record_provenance(
                user_id, RecordType.DASHBOARD_INSIGHT, "current", DB_PATH
            )
            if user_id
            else None
        ),
    }


@app.get("/")
def dashboard(request: Request):
    return FileResponse(FRONTEND_DIST / "index.html")


@app.get("/api/checkins")
def api_checkins(request: Request):
    user_id = _check_api_auth(request)
    checkins = get_checkins(db_path=DB_PATH, user_id=user_id)
    return JSONResponse(
        jsonable_encoder(
            {
                "checkins": checkins,
                "loading_svg": charts._empty_chart(
                    "Check-in cycle in progress", 300, 100
                )
                if not checkins
                else None,
                "setup": _setup_payload(user_id, "checkins"),
            }
        )
    )


@app.get("/api/setup/state")
def api_setup_state(request: Request) -> JSONResponse:
    """Return this tenant's setup state for every surface.

    The navigation shell uses it to show which surfaces still need setup, and
    the coach uses it to explain why it has nothing to answer from.
    """

    from setup_state import SURFACE_REQUIREMENTS

    user_id = _check_api_auth(request)
    account = _account_setup(user_id)
    surfaces = {
        surface: surface_setup_payload(account, surface)
        for surface in sorted(SURFACE_REQUIREMENTS)
    }
    return JSONResponse(
        jsonable_encoder({"account": account.as_dict(), "surfaces": surfaces})
    )


class OnboardingStepSelection(BaseModel):
    """One step chosen by the authenticated browser.

    Unknown fields (including any owner identifier) are rejected; the tenant is
    always the session identity.
    """

    model_config = ConfigDict(extra="forbid")

    step: str


@app.get("/api/onboarding")
def api_onboarding(request: Request) -> JSONResponse:
    """Return this tenant's resumable first-run setup sequence."""

    user_id = _check_api_auth(request)
    return JSONResponse(jsonable_encoder(_onboarding_payload(user_id)))


@app.post("/api/onboarding/skip")
def api_skip_onboarding_step(
    body: OnboardingStepSelection, request: Request
) -> JSONResponse:
    """Persist the caller's explicit skip of one optional setup step."""

    _check_rate_limit(request, limit=10)
    user_id = _check_api_auth(request)
    try:
        state = skip_step(
            user_id,
            body.step,
            db_path=DB_PATH,
            server_ai_key=os.environ.get("GEMINI_API_KEY", "").strip() or None,
        )
    except OnboardingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(jsonable_encoder(state.as_dict()))


@app.post("/api/onboarding/resume")
def api_resume_onboarding_step(
    body: OnboardingStepSelection, request: Request
) -> JSONResponse:
    """Forget a previous skip so the caller can revisit that setup step."""

    _check_rate_limit(request, limit=10)
    user_id = _check_api_auth(request)
    try:
        state = resume_step(
            user_id,
            body.step,
            db_path=DB_PATH,
            server_ai_key=os.environ.get("GEMINI_API_KEY", "").strip() or None,
        )
    except OnboardingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(jsonable_encoder(state.as_dict()))


@app.get("/api/dashboard")
def api_dashboard(request: Request) -> JSONResponse:
    user_id = _check_api_auth(request)

    from fastapi.encoders import jsonable_encoder

    ctx = _dashboard_context(user_id=user_id)
    return JSONResponse(content=jsonable_encoder(ctx))


def _body_charts(
    *, user_id: str | None = None, unit_system: str = UserSettings().unit_system
) -> dict[str, str | None]:
    readings = get_body_metrics(db_path=DB_PATH, user_id=user_id)

    def _series(key: str, unit: str, colour: str) -> str | None:
        weight = key == "weight_kg"
        shown = weight_unit(unit_system) if weight else unit
        points = [
            {
                "date": r["date"][5:],
                "value": convert_weight(r[key], unit_system) if weight else r[key],
                "label": f"{convert_weight(r[key], unit_system) if weight else r[key]:g} {shown}",
            }
            for r in readings
            if r[key] is not None
        ]
        if len(points) < 2:
            return None
        return charts.line_chart(points, unit=shown, colour=colour)

    return {
        "weight": _series("weight_kg", "kg", charts.ACCENT),
        "body_fat": _series("body_fat_pct", "%", charts.WARN),
        "muscle": _series("muscle_pct", "%", charts.ACCENT_2),
        "resting_hr": _series("resting_hr", "bpm", charts.PINK),
    }


def _project_lift(
    label: str,
    entries: list[Any],
    target_ordinal: int,
    *,
    metric: str = "auto",
    unit_system: str = UserSettings().unit_system,
) -> dict[str, Any] | None:
    """Build a projection card for a lift at the end of the cycle."""
    points: list[tuple[float, float]] = []
    unit = weight_unit(unit_system)
    use_e1rm = metric == "e1rm"
    if metric == "auto":
        use_e1rm = any(e["top_weight_kg"] for e in entries)
    for e in entries:
        value = (
            analytics.epley_1rm(e["top_weight_kg"], e["top_reps"])
            if use_e1rm
            else e["top_reps"]
        )
        if value is None:
            continue
        points.append((float(date.fromisoformat(e["date"]).toordinal()), float(value)))
    if len(points) < 2:
        return None
    if not use_e1rm:
        unit = "reps"
    current = points[-1][1]
    projected = analytics.project(points, target_ordinal)
    if projected is None:
        return None
    fit = analytics.linear_fit([p[0] for p in points], [p[1] for p in points])
    uncertainty = None
    if fit is not None and len(points) >= 3:
        slope, intercept = fit
        residuals = [y - (slope * x + intercept) for x, y in points]
        variance = sum(r * r for r in residuals) / max(1, len(points) - 2)
        uncertainty = round(variance**0.5, 1)
    if use_e1rm:
        # Fit and compare in kilograms; only the presented numbers are converted.
        current = convert_weight(current, unit_system) or 0.0
        projected = convert_weight(projected, unit_system) or 0.0
        if uncertainty:
            uncertainty = convert_weight(uncertainty, unit_system) or 0.0
    return {
        "label": label,
        "current": f"{current:g} {unit}",
        "projected": f"{projected:g} {unit}",
        "metric": "est. 1RM" if use_e1rm else "top reps",
        "model": "linear trend" if use_e1rm else "linear reps trend",
        "evidence_sessions": len(points),
        "uncertainty": f"±{uncertainty:g} {unit}" if uncertainty else "unquantified",
        "gain": round(projected - current, 1),
    }


def _block_lift_str(lift: dict | None) -> str:
    """Format a lift dict as 'sets x rep_range' or empty string."""
    if not lift:
        return ""
    sets = lift.get("sets", 0)
    rep_range = lift.get("rep_range", "")
    if not sets or not rep_range:
        return ""
    return f"{sets} x {rep_range}"


def _local_start_date(user_id: str) -> date:
    """The user's calendar day today: the default programme start date."""
    return local_today(get_user_settings(user_id, DB_PATH).timezone)


HEVY_SYNC_META_KEY = "hevy_source_sync"
HEVY_SYNC_STALE_HOURS = 24


def _read_hevy_sync_state(user_id: str) -> dict[str, Any]:
    """Return the tenant's stored Hevy sync metadata."""

    raw = get_meta(HEVY_SYNC_META_KEY, DB_PATH, user_id=user_id)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _record_hevy_sync_state(user_id: str, snapshot: HevySourceSnapshot) -> None:
    """Store the last successful Hevy sync for this tenant only.

    A partial read does not replace the last successful sync: the account was
    not read completely, so the previous complete sync stays the reference.
    """

    if snapshot.is_partial:
        return
    set_meta(
        HEVY_SYNC_META_KEY,
        json.dumps(
            {
                "synced_at": snapshot.fetched_at,
                "status": snapshot.status,
                "counts": snapshot.counts,
            },
            default=str,
        ),
        DB_PATH,
        user_id=user_id,
    )


def _hevy_sync_is_stale(sync_state: dict[str, Any]) -> bool:
    """True when the last successful sync is missing or older than a day."""

    synced_at = sync_state.get("synced_at")
    if not synced_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(synced_at))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    age = datetime.now(tz=UTC) - parsed
    return age > timedelta(hours=HEVY_SYNC_STALE_HOURS)


def _connection_state_for_status(status_code: int) -> str:
    """Map an API status code onto a builder connection state."""

    if status_code == 400:
        return "disconnected"
    if status_code == 401:
        return "auth_error"
    if status_code == 429:
        return "rate_limited"
    return "error"


def _authenticated_owner(user_id: str) -> str | None:
    """Return the authenticated owner's account label for the builder."""

    user = get_user_by_id(user_id, DB_PATH)
    if not user:
        return None
    return str(user.get("email") or user.get("display_name") or "") or None


def _active_definition(user_id: str) -> dict[str, Any] | None:
    """Return the active programme definition for drift comparison."""

    active = get_active_programme(user_id, db_path=DB_PATH)
    if not active:
        return None
    definition = active.get("definition")
    return definition if isinstance(definition, dict) else None


def _blocking_violation_detail(violation: Any) -> str:
    """Render a blocking violation for an API error, accepting both shapes.

    The shared validator emits structured dicts; the preview validator emits
    code/message dicts. Both must produce a readable 422 detail.
    """
    if isinstance(violation, dict):
        if {"exercise", "prescription", "rule", "permitted_remediation"} <= set(
            violation
        ):
            return (
                f"Activation blocked by profile constraint: {violation['exercise']} "
                f"({violation['prescription']}) violates '{violation['rule']}'. "
                f"Permitted remediation: {violation['permitted_remediation']}"
            )
        message = violation.get("message")
        if message:
            return str(message)
        return f"Activation blocked by training rules: {violation}"
    return f"Activation blocked by training rules: {violation}"


@app.post("/api/programmes/preview")
def preview_programme(
    payload: ProgrammePreviewRequest,
    request: Request,
) -> JSONResponse:
    """Generate a deterministic preview from selected Hevy routines."""

    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if "start_date" not in payload.model_fields_set:
        payload = payload.model_copy(update={"start_date": _local_start_date(user_id)})
    training_data = _load_hevy_training_for_user(user_id)
    prefs = get_user_preferences(user_id, DB_PATH)
    profile_rules = prefs.get("constraints")
    payload.rules = get_training_rules(user_id, DB_PATH)
    active = get_active_programme(user_id, db_path=DB_PATH)
    active_definition = active.get("definition") if active else None
    try:
        preview = build_programme_preview(
            training_data,
            payload,
            history_overrides=get_routine_match_overrides(user_id, DB_PATH),
            active_definition=active_definition,
            profile_rules=profile_rules,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return JSONResponse(jsonable_encoder({"preview": preview}))


@app.post("/api/programmes/activate")
def activate_programme(
    payload: ProgrammeActivationRequest,
    request: Request,
) -> JSONResponse:
    """Reconfirm Hevy source data and activate the generated programme."""

    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if "start_date" not in payload.model_fields_set:
        payload = payload.model_copy(update={"start_date": _local_start_date(user_id)})
    training_data = _load_hevy_training_for_user(user_id)
    prefs = get_user_preferences(user_id, DB_PATH)
    payload.rules = get_training_rules(user_id, DB_PATH)
    preview_request = ProgrammePreviewRequest.model_validate(
        payload.model_dump(
            exclude={"preview_token", "source_hashes"},
        )
    )
    profile_rules = prefs.get("constraints")
    try:
        preview = build_programme_preview(
            training_data,
            preview_request,
            history_overrides=get_routine_match_overrides(user_id, DB_PATH),
            active_definition=_active_definition(user_id),
            profile_rules=profile_rules,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        active = activate_generated_programme(
            user_id,
            preview,
            db_path=DB_PATH,
            presented_token=payload.preview_token,
            presented_source_hashes=[
                {"routine_id": item.routine_id, "source_hash": item.source_hash}
                for item in payload.source_hashes
            ],
        )
    except ProgrammeConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": exc.message, **exc.details},
        ) from exc
    except ProgrammeValidationError as exc:
        violations = exc.details.get("violations", [])
        first_violation = violations[0] if violations else None
        if isinstance(first_violation, dict) and {
            "exercise",
            "prescription",
            "rule",
            "permitted_remediation",
        } <= set(first_violation):
            raise HTTPException(
                status_code=422,
                detail=_blocking_violation_detail(first_violation),
            ) from exc
        raise HTTPException(
            status_code=422,
            detail={"message": exc.message, "violations": violations},
        ) from exc

    return JSONResponse(
        jsonable_encoder(
            {
                "status": "ok",
                "active_programme": active,
            }
        )
    )


def _hevy_api_key_for_user(user_id: str) -> str:
    """Return the tenant's Hevy API key or raise a useful API error."""

    credential = _vault_credential(user_id, "hevy", purpose="programme_import")
    hevy_key = (
        credential.api_key.reveal()
        if credential is not None and credential.api_key is not None
        else ""
    )
    if not hevy_key:
        raise HTTPException(
            status_code=400,
            detail="No Hevy API key configured. Add your key in Settings first.",
        )
    return hevy_key


def _load_hevy_snapshot_for_user(
    user_id: str,
    *,
    workout_limit: int = 56,
) -> HevySourceSnapshot:
    """Fetch the current user's Hevy source data or raise a useful API error.

    Provider failures are mapped onto distinct status codes so the builder can
    show authentication, rate-limit and outage states separately.
    """

    hevy_key = _hevy_api_key_for_user(user_id)

    from hevy_reader import fetch_user_training_snapshot

    try:
        snapshot = fetch_user_training_snapshot(
            hevy_key,
            workout_limit=workout_limit,
        )
    except HevyAuthError as exc:
        logger.warning("Hevy rejected the stored credential for user %s.", user_id)
        raise HTTPException(
            status_code=401,
            detail=(
                "Hevy rejected your API key. Update it in Settings and refresh."
            ),
        ) from exc
    except HevyRateLimitError as exc:
        logger.warning("Hevy rate limited user %s.", user_id)
        raise HTTPException(
            status_code=429,
            detail="Hevy is rate limiting requests. Try again in a few minutes.",
        ) from exc
    except HevyError as exc:
        logger.exception(
            "Could not fetch Hevy source data for user %s.",
            user_id,
        )
        raise HTTPException(
            status_code=502,
            detail=("Hevy could not be reached. Check the connection and try again."),
        ) from exc

    from hevy_history import merge_workout_history, parse_cached_workouts

    # The incremental local cache from the full-history sync is the canonical
    # evidence source. Provider calls stay bounded to recent workouts.
    try:
        cached = parse_cached_workouts(
            get_cached_workout_payloads(user_id, db_path=DB_PATH),
            snapshot.data.exercise_templates,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        logger.warning("Ignoring malformed cached Hevy history for user %s.", user_id)
        cached = []
    if cached:
        snapshot.data.recent_workouts = merge_workout_history(
            snapshot.data.recent_workouts,
            cached,
        )
    return snapshot


def _load_hevy_training_for_user(
    user_id: str,
    *,
    workout_limit: int = 56,
) -> HevyTrainingData:
    """Fetch the current user's Hevy training data through the snapshot loader."""

    return _load_hevy_snapshot_for_user(
        user_id,
        workout_limit=workout_limit,
    ).data


@app.get("/api/xai_reasoning/{context_id}")
def xai_reasoning(context_id: str, request: Request) -> dict[str, Any]:
    _check_rate_limit(request)
    user_id = _check_api_auth(request)

    existing = get_reasoning_log(context_id, db_path=DB_PATH, user_id=user_id)
    if existing:
        return {
            "reasoning": existing,
            "provenance": get_record_provenance(
                user_id, RecordType.REASONING, context_id, DB_PATH
            ),
        }

    parts = context_id.split("_", 1)
    if len(parts) != 2:
        return {"reasoning": "Invalid context ID"}

    when, ex_name = parts

    config = get_config()
    provider = _resolve_provider_for_request(request, config)

    history = get_progress_history(db_path=DB_PATH, user_id=user_id).get(ex_name, [])
    context = _regional_prompt_context(user_id)

    prompt = f"Why did my volume/performance change for {ex_name} around {when}? Here is my history: {json.dumps(history)}. Provide a clear causal explanation in a few sentences.\n\n{context}"
    try:
        response = provider.generate(prompt)
        response_text = response if isinstance(response, str) else "".join(response)
        reasoning = (response_text or "Could not determine reasoning.").strip()
    except Exception:
        logger.exception("AI reasoning generation failed.")
        reasoning = "Could not determine reasoning."

    save_reasoning_log(
        context_id,
        ex_name,
        reasoning,
        db_path=DB_PATH,
        user_id=user_id,
        provenance=ai_provenance(provider, "xai-reasoning/1"),
    )
    return {
        "reasoning": reasoning,
        "provenance": get_record_provenance(
            user_id, RecordType.REASONING, context_id, DB_PATH
        ),
    }


@app.get("/api/project_peak")
def project_peak(request: Request) -> dict[str, Any]:
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    config = get_config()
    provider = _resolve_provider_for_request(request, config)

    from programme_runtime import load_programme_runtime

    series = get_progress_history(db_path=DB_PATH, user_id=user_id)
    runtime = load_programme_runtime(user_id, DB_PATH)
    priority_names: list[str] = []
    for day in runtime.days:
        for exercise in day.get("exercises") or []:
            name = str(exercise.get("name") or "")
            if (
                name
                and name in series
                and exercise.get("role")
                in {"primary_strength", "secondary_compound"}
                and name not in priority_names
            ):
                priority_names.append(name)
    target = runtime.end_date or datetime.now(tz=timezone.utc).date()
    deterministic = []
    for name in priority_names[:3]:
        entries = series[name]
        points = [
            (
                float(date.fromisoformat(entry["date"]).toordinal()),
                float(
                    analytics.epley_1rm(
                        entry["top_weight_kg"], entry["top_reps"]
                    )
                    or entry["top_reps"]
                ),
            )
            for entry in entries
            if entry.get("top_reps") is not None
        ]
        projected = analytics.project(points, float(target.toordinal()))
        if projected is not None:
            deterministic.append(
                {
                    "metric": name,
                    "model": "linear trend",
                    "evidence_sessions": len(points),
                    "projected": projected,
                }
            )
    if not deterministic:
        return {
            "projections": [],
            "validation": "Insufficient priority-outcome history to project.",
        }

    prompt = (
        "Explain these deterministic programme projections. Do not change any "
        f"number or invent a metric: {json.dumps(deterministic)}. "
        "Return JSON with a single 'validation' string.\n\n"
        f"{_regional_prompt_context(user_id)}"
    )
    try:
        response = provider.generate(prompt)
        text = (response if isinstance(response, str) else "".join(response)).strip()
        text = text.removeprefix("```json").removeprefix("```")
        text = text.removesuffix("```")
        parsed = json.loads(text.strip())
        if isinstance(parsed, dict):
            parsed.setdefault("projections", deterministic)
            return parsed
        return {"projections": deterministic}
    except Exception:  # noqa: BLE001
        return {
            "projections": deterministic,
            "validation": "Explanation unavailable; deterministic projections shown.",
        }


@app.get("/api/coach/capabilities")
@app.get("/api/chat/capabilities")
def coach_capabilities(request: Request) -> JSONResponse:
    user_id = _check_api_auth(request)
    state = get_coaching_capabilities(user_id, db_path=DB_PATH, config=get_config())
    return JSONResponse(state.to_dict())


def _parse_observed_at(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _latest_observed_at(values: list[Any]) -> datetime | None:
    parsed = [
        observed
        for value in values
        if (observed := _parse_observed_at(value)) is not None
    ]
    return max(parsed) if parsed else None


def _coach_capability_for_user(
    user_id: str,
    *,
    logs: list[dict[str, Any]] | None = None,
    history: dict[str, list[dict[str, Any]]] | None = None,
    body_metrics: list[dict[str, Any]] | None = None,
    personal_records: list[dict[str, Any]] | None = None,
    programme_available: bool | None = None,
    provider_resolved: bool = False,
) -> CoachCapabilityState:
    """Derive Coach capability from this tenant's records and provider state.

    ``provider_resolved`` is set by a caller that has just built the tenant's
    provider: a provider that resolved is configured by definition, so the
    capability agrees with the resolver instead of guessing from key presence.
    Verification is never assumed: it comes only from the last connection test
    of the tenant's own stored credential.
    """

    provider = ai_provider_status(
        user_id=user_id,
        db_path=DB_PATH,
        server_api_key=get_config().gemini_api_key,
    )
    if provider_resolved and not provider.configured and provider.supported:
        provider = AiProviderStatus(
            provider=provider.provider,
            supported=True,
            configured=True,
            source="server",
            state=ProviderState.UNVERIFIED,
        )
    account = _account_setup(user_id)

    if logs is None:
        logs = get_daily_logs(limit=1, db_path=DB_PATH, user_id=user_id)
    if history is None:
        history = get_progress_history(
            limit_per_exercise=1, db_path=DB_PATH, user_id=user_id
        )
    if body_metrics is None:
        body_metrics = get_body_metrics(limit=1, db_path=DB_PATH, user_id=user_id)
    if personal_records is None:
        personal_records = get_personal_records(db_path=DB_PATH, user_id=user_id)
    if programme_available is None:
        programme_available = get_active_programme(user_id, DB_PATH) is not None

    history_dates = [
        entry.get("date")
        for entries in history.values()
        for entry in entries
    ]
    observed_at = {
        "training_logs": _latest_observed_at(
            [log.get("date") for log in logs]
        ),
        "progress_history": _latest_observed_at(history_dates),
        "personal_records": _latest_observed_at(
            [record.get("date") for record in personal_records]
        ),
        "body_metrics": _latest_observed_at(
            [metric.get("date") for metric in body_metrics]
        ),
    }

    return derive_coach_capability(
        provider_name=provider.provider,
        provider_configured=provider.configured,
        provider_supported=provider.supported,
        provider_test_status=provider.last_test_status,
        provider_source=provider.source,
        sync_configured=account.checks["hevy_connected"],
        readiness=account.state.value,
        observed_at=observed_at,
        programme_available=programme_available,
        now=datetime.now(tz=UTC),
    )


def _run_hevy_sync(user_id: str, hevy_key: str) -> dict[str, int | str]:
    """Rebuild one tenant's history inside that tenant's connector lease.

    Runs in a worker thread, so the lease starts and ends with the work itself:
    a client that disconnects mid-sync cannot release the lease early or record
    a failure for a sync that is still running.
    """

    from scripts.sync_history import sync_all

    with tracked_operation(
        user_id, OperationPhase.SYNCING, db_path=DB_PATH
    ) as operation:
        result = sync_all(hevy_key, DB_PATH, user_id=user_id)
        if "error" in result:
            operation.failed(str(result.get("error_code") or "sync_failed"))
        else:
            operation.succeeded(records=int(result.get("workouts_found", 0)))
    return result


def _verify_hevy_tracked(user_id: str, api_key: str) -> HevyCredentialTest:
    """Check a Hevy key inside the tenant's lease, so "connecting" is observable."""

    with tracked_operation(user_id, OperationPhase.CONNECTING, db_path=DB_PATH):
        return verify_hevy_credentials(api_key)


def _operation_busy(exc: OperationBusyError) -> HTTPException:
    """The refusal for a connect or sync while another one is still running."""

    return HTTPException(
        status_code=409,
        detail=(
            f"A Hevy {exc.phase.value} is already running for this account. "
            "Try again when it finishes."
        ),
    )


def _coach_unavailable(capability: CoachCapabilityState) -> HTTPException:
    """The refusal Coach gives when the state the UI shows says it cannot answer."""

    return HTTPException(
        status_code=409,
        detail={
            "code": capability.status.value,
            "message": capability.blocked_message()
            or "Coach is not available for this account yet.",
        },
    )


@app.get("/api/coach/capability")
def coach_capability(request: Request) -> JSONResponse:
    """Return the authenticated user's truthful Coach capability state."""

    user_id = _check_api_auth(request)
    state = _coach_capability_for_user(user_id)
    return JSONResponse(state.as_dict())


@app.get("/api/chat/history")
def chat_history(request: Request):
    user_id = _check_api_auth(request)
    messages = get_chat_messages(limit=50, db_path=DB_PATH, user_id=user_id)
    return JSONResponse(
        jsonable_encoder(
            {
                "messages": messages,
                "setup": _setup_payload(user_id, "coach"),
            }
        )
    )


@app.post("/api/chat/clear")
def chat_clear(request: Request):
    user_id = _check_api_auth(request)
    clear_chat_messages(db_path=DB_PATH, user_id=user_id)
    _audit(
        request,
        AuditEventType.DATA_DELETED,
        AuditOutcome.SUCCESS,
        AuditSubjectType.DATA,
        "chat_history",
        user_id=user_id,
    )
    return {"status": "ok"}


def _sse(event: str, data: Any) -> str:
    """Encode one server-sent event payload."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _parse_window_bound(raw: str | None, *, name: str, fallback: date) -> date:
    if raw is None or not raw.strip():
        return fallback
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be an ISO date (YYYY-MM-DD)",
        ) from exc


def _retrieval_data_sources(user_id: str) -> dict[str, Any]:
    """Section-level provenance for AI context where records are not annotated."""
    summary = get_provenance_summary(user_id, RecordType.EXERCISE_PROGRESS, DB_PATH)
    history: dict[str, Any] = {
        "origin": "imported",
        "source": "Hevy",
        "authoritative": True,
    }
    if summary["total"]:
        history["sessions_with_provenance"] = summary["total"]
        history["last_ingested"] = str(summary["latest_ingested_at"])[:10]
    return {
        "history": history,
        "prs": {
            "origin": "derived",
            "source": "calculated from the imported Hevy history (Epley estimate)",
            "authoritative": False,
        },
    }


@app.get("/api/rag_search", response_model=None)
def rag_search(
    request: Request,
    q: str = Query(...),
    start_date: str | None = Query(None, alias="start"),
    end_date: str | None = Query(None, alias="end"),
    start_date_long: str | None = Query(None, alias="start_date"),
    end_date_long: str | None = Query(None, alias="end_date"),
) -> Any:
    _check_rate_limit(request, limit=15)
    user_id = _check_api_auth(request)
    config = get_config()
    provider = _resolve_provider_for_request(request, config)

    now = datetime.now(tz=timezone.utc)
    today = now.date()
    start_raw = start_date if start_date is not None else start_date_long
    end_raw = end_date if end_date is not None else end_date_long
    window_end = _parse_window_bound(end_raw, name="end", fallback=today)
    try:
        default_start = window_end - timedelta(days=90)
    except OverflowError:
        default_start = date.min
    window_start = _parse_window_bound(
        start_raw,
        name="start",
        fallback=default_start,
    )
    if window_start > window_end:
        raise HTTPException(
            status_code=400,
            detail="start must not be after end",
        )
    # Future data is unavailable. Never let a window request cite it.
    window_end = min(window_end, today)
    window_start = min(window_start, window_end)

    settings = get_user_settings(user_id, DB_PATH)
    cap_state = get_coaching_capabilities(
        user_id, db_path=DB_PATH, config=config, provider=provider
    )
    mode, required_cap = classify_coaching_query(q)

    # Gather training context. Every record carries its provenance so the model
    # can tell source data, user corrections and derived recommendations apart.
    logs = get_daily_logs(
        limit=30, db_path=DB_PATH, user_id=user_id, include_provenance=True
    )
    history = get_progress_history(db_path=DB_PATH, user_id=user_id)
    biometrics = get_body_metrics(
        db_path=DB_PATH, user_id=user_id, include_provenance=True
    )
    prs = get_personal_records(db_path=DB_PATH, user_id=user_id)
    active_prog = (
        get_active_programme(user_id=user_id, db_path=DB_PATH) if user_id else None
    )

    evidence = build_evidence_bundle(
        user_id=user_id,
        logs=logs,
        history=history,
        biometrics=biometrics[-10:],
        personal_records=prs[:10],
        as_of=now,
        window_start=window_start,
        window_end=window_end,
    )

    capability = _coach_capability_for_user(
        user_id,
        logs=logs,
        history=history,
        body_metrics=biometrics,
        personal_records=prs,
        programme_available=active_prog is not None,
        provider_resolved=True,
    )
    # Enforce exactly what the UI shows: when the capability says Coach cannot
    # answer, nothing is saved and no model is called.
    if not capability.can_chat:
        raise _coach_unavailable(capability)
    access_block = capability_prompt_block(capability)

    # If data-specific prompt and prerequisites failed:
    if mode == "data_grounded" and required_cap:
        cap_item = cap_state.capabilities.get(required_cap)
        if not cap_item or not cap_item.enabled:
            save_chat_message("user", q, db_path=DB_PATH, user_id=user_id)
            explanation_msg = (
                f"**Data-Grounded Coaching Unavailable**\n\n"
                f"{cap_item.explanation if cap_item else 'Required training inputs are unavailable.'}\n\n"
                f"To prevent fabricated user history, personalised analysis is disabled until "
                f"the prerequisite data is connected and current. You may still ask general educational "
                f"questions about fitness, technique, and programming."
            )
            save_chat_message("assistant", explanation_msg, db_path=DB_PATH, user_id=user_id)

            def generate_disabled_explanation() -> Iterator[str]:
                yield _sse("evidence", evidence.to_dict())
                yield _sse("delta", {"text": explanation_msg})
                yield _sse(
                    "status",
                    {
                        "grounding": "incomplete",
                        "cited": [],
                        "evidence_count": len(evidence.items),
                    },
                )
                yield _sse("done", {})

            return StreamingResponse(
                generate_disabled_explanation(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

    # Build multi-turn conversation from chat history
    chat_history_msgs = get_chat_messages(limit=20, db_path=DB_PATH, user_id=user_id)
    conversation_lines = []
    for msg in chat_history_msgs:
        role_label = "User" if msg["role"] == "user" else "Coach"
        conversation_lines.append(f"{role_label}: {msg['content']}")
    conversation_text = (
        "\n".join(conversation_lines) if conversation_lines else "No previous messages."
    )

    # Save user message
    save_chat_message("user", q, db_path=DB_PATH, user_id=user_id)

    if mode == "educational":
        prompt = f"""You are Coach, an elite strength and conditioning AI coach providing general educational guidance.
{access_block}

Important constraints:
- This is a general educational inquiry. Do NOT fabricate, assume, or invent any user-specific training history, past workouts, personal records, or biometrics.
- Keep your answer clear, evidence-based, concise, and structured. Use {spelling_for(settings.locale)}.
- Start your response exactly with this header line:
**Educational Guidance** (General advice — not personalised to your training history)

{evidence_prompt(evidence)}

{prompt_context(settings.timezone, settings.locale, settings.unit_system)}

Conversation so far:
{conversation_text}

User's question: {q}

Respond naturally as Coach. If the question is about their training data, cite the supporting evidence. If it is a general fitness question, answer from expertise but relate it back to their programme where possible."""
    else:
        active_def = (active_prog.get("definition") if active_prog else None) or {}
        prog_name = active_def.get("name") or "active programme"
        spec = active_def.get("programme_spec") or {}
        prog_goal = spec.get("goal_label") or spec.get("goal") or "strength and conditioning"
        rules = get_training_rules(user_id, DB_PATH)
        rules_text = format_rules_for_prompt(rules)
        context = json.dumps(
            {
                "logs": annotate_for_retrieval(logs),
                "history": history,
                "biometrics": annotate_for_retrieval(biometrics[-10:]),
                "prs": prs[:10],
                "active_programme": prog_name if active_prog else None,
                "data_sources": _retrieval_data_sources(user_id),
            },
            default=str,
        )

        prompt = f"""You are Coach, an elite {prog_goal} AI coach embedded in the user's private training dashboard.
The user's active programme is "{prog_name}".
{access_block}

Important constraints:
- All analysis must be strictly grounded in the verified data context below.
- Do NOT fabricate or hallucinate any numbers, dates, exercises, sets, or reps not present in the context.
- Start your response exactly with this header line:
**Personalised Analysis** (Grounded in your training data)
- Reference specific numbers, dates, and exercises from the context.
- Keep answers concise but insightful. You can use **bold** for emphasis and bullet lists for structure.
- Use {spelling_for(settings.locale)}.

{evidence_prompt(evidence)}

{prompt_context(settings.timezone, settings.locale, settings.unit_system)}

{rules_text}

Conversation so far:
{conversation_text}

{RETRIEVAL_GUIDANCE}

Verified training data context (recent logs, progress history, biometrics, PRs, active programme):
{context[:25000]}

User's question: {q}"""

    def generate() -> Iterator[str]:
        yield _sse("evidence", evidence.to_dict())
        collected: list[str] = []
        try:
            response = provider.generate(prompt, stream=True)
            chunks = (response,) if isinstance(response, str) else response
            for chunk in chunks:
                if chunk:
                    collected.append(chunk)
        except Exception:
            logger.exception("AI chat streaming failed.")
            collected.append(
                "Sorry, Coach is currently unavailable or encountered an error. "
                "Please try again."
            )

        answer, cited = validate_citations("".join(collected), evidence)
        grounding = resolve_grounding(answer, evidence, cited)
        if answer:
            save_chat_message(
                "assistant",
                answer,
                db_path=DB_PATH,
                user_id=user_id,
                evidence_json=serialise_bundle(
                    evidence,
                    grounding=grounding,
                    cited=cited,
                ),
            )
        for offset in range(0, len(answer), 140):
            yield _sse("delta", {"text": answer[offset : offset + 140]})
        yield _sse(
            "status",
            {
                "grounding": grounding.value,
                "cited": cited,
                "evidence_count": len(evidence.items),
            },
        )
        yield _sse("done", {})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _gh_redirect_uri(request: Request) -> str:
    """The OAuth redirect URI, explicit env override or derived from the request."""
    return GH_REDIRECT_URI or str(request.url_for("google_health_callback"))


def _audit_key_change(
    request: Request,
    user_id: str,
    provider: str,
    known_providers: set[str],
    *,
    saved: bool,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Audit a stored-key change, plus the connector change when it is Hevy's key.

    Only a recognised provider id is recorded; an arbitrary client string is never
    written to the audit trail. The key itself is never passed to the audit log.
    """
    recorded = provider if provider in known_providers else "unknown"
    is_connector = recorded == "hevy"
    _audit(
        request,
        AuditEventType.CREDENTIAL_SAVED if saved else AuditEventType.CREDENTIAL_DELETED,
        AuditOutcome.SUCCESS,
        AuditSubjectType.CREDENTIAL,
        recorded,
        user_id=user_id,
        metadata={
            "provider": recorded,
            "credential_kind": "connector" if is_connector else "ai_provider",
            **(metadata or {}),
        },
    )
    if is_connector:
        _audit(
            request,
            AuditEventType.CONNECTOR_CONNECTED
            if saved
            else AuditEventType.CONNECTOR_DISCONNECTED,
            AuditOutcome.SUCCESS,
            AuditSubjectType.CONNECTOR,
            recorded,
            user_id=user_id,
            metadata={"provider": recorded, "method": "api_key"},
        )


def _hevy_verification_status(state: HevyCredentialState) -> int:
    """Map a verification outcome to an HTTP status code."""
    if state is HevyCredentialState.UNVERIFIED:
        # Hevy could not be reached; the credential was not disproved.
        return 502
    return 400


@app.post("/api/settings/key")
async def save_api_key(request: Request) -> dict[str, Any]:
    """Save or update an API key for the current user.

    A Hevy credential is verified before it is stored: an unusable key is
    rejected and never activated. The secret is only ever persisted through the
    tenant-scoped vault, never returned to the browser.
    """
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    provider_value = body.get("provider") if isinstance(body, dict) else None
    api_key_value = body.get("api_key") if isinstance(body, dict) else None
    provider = provider_value.strip().lower() if isinstance(provider_value, str) else ""
    api_key = api_key_value.strip() if isinstance(api_key_value, str) else ""

    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="Provider and api_key are required")
    try:
        provider_spec(provider)
    except UnknownProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    model_value = body.get("model")
    if model_value is not None and not isinstance(model_value, str):
        raise HTTPException(status_code=400, detail="Model must be a string")
    model = model_value.strip() if isinstance(model_value, str) else ""
    if len(model) > 200:
        raise HTTPException(status_code=400, detail="Model is too long")

    verification: HevyCredentialTest | None = None
    if provider == "hevy":
        # The verification performs a bounded set of outbound requests; run it
        # off the event loop so a slow provider cannot stall other tenants.
        # The candidate is proven usable before it is ever persisted.
        try:
            verification = await run_in_threadpool(
                _verify_hevy_tracked, user_id, api_key
            )
        except OperationBusyError as exc:
            raise _operation_busy(exc) from exc
        if not verification.connected:
            raise HTTPException(
                status_code=_hevy_verification_status(verification.state),
                detail=verification.detail,
            )

    vault = _vault()
    try:
        if vault.describe(user_id, provider).configured:
            summary = vault.rotate(user_id, provider, api_key=api_key, model=model or None)
        else:
            summary = vault.store(user_id, provider, api_key=api_key, model=model or None)
    except CredentialExistsError:
        try:
            summary = vault.rotate(
                user_id, provider, api_key=api_key, model=model or None
            )
        except VaultError as exc:
            raise _vault_http_error(exc) from exc
    except VaultError as exc:
        raise _vault_http_error(exc) from exc

    if verification is not None:
        save_hevy_connection(
            user_id,
            state=verification.state.value,
            api_version=verification.api_version,
            capabilities=dict(verification.capabilities),
            tested_at=verification.tested_at.isoformat(),
            account_username=verification.account_username,
            account_id=verification.account_id,
            db_path=DB_PATH,
        )
        # A newly verified credential starts afresh: a failure recorded for the
        # previous key says nothing about this one.
        clear_sync_outcome(user_id, db_path=DB_PATH)
        _audit_key_change(
            request,
            user_id,
            provider,
            {"hevy", *(item["id"] for item in available_providers())},
            saved=True,
            metadata={"model_configured": bool(model)},
        )
        return {
            "status": "ok",
            "provider": provider,
            "connection": verification.as_public_metadata(),
            "credential": _settings_key_payload(summary),
        }

    ai_provider_defaults = {
        item["id"]: item["default_model"] for item in available_providers()
    }
    if provider in ai_provider_defaults:
        preferences = get_user_preferences(user_id, DB_PATH)
        if preferences.get("preferred_ai") == provider:
            save_user_preferences(
                user_id,
                ai_model=model or ai_provider_defaults[provider],
                db_path=DB_PATH,
            )
    _audit_key_change(
        request,
        user_id,
        provider,
        {"hevy", *(item["id"] for item in available_providers())},
        saved=True,
        metadata={"model_configured": bool(model)},
    )
    return {
        "status": "ok",
        "provider": provider,
        "credential": _settings_key_payload(summary),
    }


@app.post("/api/settings/key/rotate")
async def rotate_api_key(request: Request) -> dict[str, Any]:
    """Replace an existing credential, keeping its audit history."""
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    provider_value = body.get("provider")
    api_key_value = body.get("api_key")
    provider = provider_value.strip().lower() if isinstance(provider_value, str) else ""
    api_key = api_key_value.strip() if isinstance(api_key_value, str) else ""
    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="Provider and api_key are required")

    model_value = body.get("model")
    model = model_value.strip() if isinstance(model_value, str) else None
    try:
        summary = _vault().rotate(user_id, provider, api_key=api_key, model=model)
    except VaultError as exc:
        raise _vault_http_error(exc) from exc
    _audit_key_change(
        request,
        user_id,
        provider,
        {"hevy", *(item["id"] for item in available_providers())},
        saved=True,
        metadata={"model_configured": bool(model)},
    )
    return {
        "status": "ok",
        "provider": provider,
        "credential": _settings_key_payload(summary),
    }


@app.post("/api/settings/key/revoke")
async def revoke_api_key(request: Request) -> dict[str, Any]:
    """Invalidate a credential and destroy its secret material."""
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    provider_value = body.get("provider")
    provider = provider_value.strip().lower() if isinstance(provider_value, str) else ""
    if not provider:
        raise HTTPException(status_code=400, detail="Provider is required")

    try:
        summary = _vault().revoke(user_id, provider)
    except VaultError as exc:
        raise _vault_http_error(exc) from exc
    _audit_key_change(
        request,
        user_id,
        provider,
        {"hevy", *(item["id"] for item in available_providers())},
        saved=False,
    )
    return {
        "status": "ok",
        "provider": provider,
        "credential": _settings_key_payload(summary),
    }


@app.post("/api/settings/key/delete")
async def remove_api_key(request: Request) -> dict[str, Any]:
    """Remove a stored API key for the current user."""
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    provider_value = body.get("provider")
    provider = provider_value.strip().lower() if isinstance(provider_value, str) else ""
    if not provider:
        raise HTTPException(status_code=400, detail="Provider is required")

    try:
        deleted = _vault().delete(user_id, provider)
    except UnknownProviderError as exc:
        # Reject an unrecognised provider id, but keep a safe record of the
        # attempt. The client-supplied string is never written to the trail.
        _audit(
            request,
            AuditEventType.CREDENTIAL_DELETED,
            AuditOutcome.FAILURE,
            AuditSubjectType.CREDENTIAL,
            "unknown",
            user_id=user_id,
            metadata={"provider": "unknown", "credential_kind": "ai_provider"},
        )
        raise _vault_http_error(exc) from exc
    except VaultError as exc:
        raise _vault_http_error(exc) from exc
    if provider == "hevy":
        delete_hevy_connection(user_id, db_path=DB_PATH)
    _audit_key_change(
        request,
        user_id,
        provider,
        {"hevy", *(item["id"] for item in available_providers())},
        saved=False,
    )
    return {"status": "ok", "provider": provider, "deleted": deleted}


@app.post("/api/settings/key/test")
async def test_api_key(request: Request) -> dict[str, Any]:
    """Test a stored or candidate credential without returning it."""
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    provider_value = body.get("provider")
    provider = provider_value.strip().lower() if isinstance(provider_value, str) else ""
    if not provider:
        raise HTTPException(status_code=400, detail="Provider is required")

    api_key_value = body.get("api_key")
    api_key = api_key_value.strip() if isinstance(api_key_value, str) else ""
    model_value = body.get("model")
    model = model_value.strip() if isinstance(model_value, str) else None
    try:
        return _test_credential(user_id, provider, api_key, model)
    except VerificationUnsupportedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except VaultError as exc:
        raise _vault_http_error(exc) from exc


@app.post("/api/settings/verify-hevy")
async def verify_hevy_key(request: Request) -> dict[str, Any]:
    """Verify a Hevy API key and record the resulting connection metadata.

    Without an ``api_key`` in the body the stored key of the authenticated
    user is verified. A credential is only ever activated when the check
    succeeds, and the plaintext key is never echoed in the response.
    """
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    candidate = body.get("api_key") if isinstance(body, dict) else None
    api_key = candidate.strip() if isinstance(candidate, str) else ""
    verifies_stored_key = not api_key
    if verifies_stored_key:
        credential = _vault_credential(user_id, "hevy", purpose="credential_test")
        api_key = (
            credential.api_key.reveal()
            if credential is not None and credential.api_key is not None
            else ""
        )
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No Hevy API key is stored for this account.",
        )

    try:
        verification = await run_in_threadpool(_verify_hevy_tracked, user_id, api_key)
    except OperationBusyError as exc:
        raise _operation_busy(exc) from exc
    if verification.connected:
        if verifies_stored_key:
            save_hevy_connection(
                user_id,
                state=verification.state.value,
                api_version=verification.api_version,
                capabilities=dict(verification.capabilities),
                tested_at=verification.tested_at.isoformat(),
                account_username=verification.account_username,
                account_id=verification.account_id,
                db_path=DB_PATH,
            )
        _audit(
            request,
            AuditEventType.CONNECTOR_TEST,
            AuditOutcome.SUCCESS,
            AuditSubjectType.CONNECTOR,
            "hevy",
            user_id=user_id,
            metadata={"provider": "hevy"},
        )
        return {
            "status": "ok",
            **verification.as_public_metadata(),
            "workout_count": verification.workout_count,
        }

    if verifies_stored_key:
        # The stored credential stopped working: record the truthful state
        # instead of leaving a stale "connected" record behind.
        save_hevy_connection(
            user_id,
            state=verification.state.value,
            api_version=verification.api_version,
            capabilities=dict(verification.capabilities),
            tested_at=verification.tested_at.isoformat(),
            db_path=DB_PATH,
        )
    _audit(
        request,
        AuditEventType.CONNECTOR_TEST,
        AuditOutcome.FAILURE,
        AuditSubjectType.CONNECTOR,
        "hevy",
        user_id=user_id,
        metadata={"provider": "hevy", "reason": "provider_rejected"},
    )
    raise HTTPException(
        status_code=_hevy_verification_status(verification.state),
        detail=verification.detail,
    )


@app.get("/api/settings/credentials/audit")
def credential_audit(request: Request, limit: int = Query(50, ge=1, le=200)):
    """Return the owner's credential lifecycle audit trail (metadata only)."""
    user_id = _check_api_auth(request)
    return {
        "events": [
            event.to_public_dict()
            for event in _vault().recent_events(user_id, limit=limit)
        ]
    }


@app.post("/api/settings/preferences")
async def save_preferences(request: Request) -> dict[str, Any]:
    """Save user training preferences and validate active programme."""
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    preferred_ai_value = body.get("preferred_ai")
    preferred_ai: str | None = None
    if preferred_ai_value is not None:
        if not isinstance(preferred_ai_value, str):
            raise HTTPException(
                status_code=400,
                detail="Preferred AI provider must be a string",
            )
        preferred_ai = preferred_ai_value.strip().lower()
        ai_provider_defaults = {
            item["id"]: item["default_model"] for item in available_providers()
        }
        if preferred_ai not in ai_provider_defaults:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown AI provider '{preferred_ai}'.",
            )

    ai_model_value = body.get("ai_model")
    if ai_model_value is not None and not isinstance(ai_model_value, str):
        raise HTTPException(status_code=400, detail="AI model must be a string")
    ai_model = ai_model_value.strip() if isinstance(ai_model_value, str) else None
    if ai_model and len(ai_model) > 200:
        raise HTTPException(status_code=400, detail="AI model is too long")
    if preferred_ai is not None and not ai_model:
        ai_model = ai_provider_defaults[preferred_ai]

    previous_ai = get_user_preferences(user_id, DB_PATH).get("preferred_ai")
    training_rules_payload = body.get("training_rules")
    if training_rules_payload is not None:
        try:
            StructuredTrainingRules.from_raw(training_rules_payload)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail=f"Invalid training rules: {exc}"
            )

    active_before = None
    if body.get("constraints") is not None or training_rules_payload is not None:
        active_before = get_active_programme(user_id, DB_PATH)
    save_user_preferences(
        user_id,
        goals=body.get("goals"),
        constraints=body.get("constraints"),
        experience_level=body.get("experience_level"),
        coaching_style=body.get("coaching_style"),
        preferred_ai=preferred_ai,
        ai_model=ai_model,
        custom_rules=body.get("custom_rules"),
        training_rules=training_rules_payload,
        db_path=DB_PATH,
    )
    if preferred_ai is not None and preferred_ai != previous_ai:
        # Choosing the AI provider decides which third party receives the user's
        # coaching context, so a real change is a permission change. Only the
        # validated provider id is recorded, never goals or constraints.
        _audit(
            request,
            AuditEventType.PERMISSION_CHANGED,
            AuditOutcome.SUCCESS,
            AuditSubjectType.PERMISSION,
            "ai_processor",
            user_id=user_id,
            metadata={"change": "provider_selected", "provider": preferred_ai},
        )

    # Edit lifecycle: validate the existing active programme against the edited
    # legacy constraints and the versioned structured training rules.
    response: dict[str, Any] = {"status": "ok"}
    if body.get("constraints") is not None or training_rules_payload is not None:
        effective_constraints = body.get("constraints")
        effective_training_rules = training_rules_payload
        if effective_constraints is None or effective_training_rules is None:
            stored_prefs = get_user_preferences(user_id, DB_PATH)
            if effective_constraints is None:
                effective_constraints = stored_prefs.get("constraints")
            if effective_training_rules is None:
                effective_training_rules = get_training_rules(user_id, DB_PATH)
        active = active_before
        if active and active.get("definition"):
            val = validate_programme(
                active["definition"],
                profile_rules=effective_constraints,
                training_rules=effective_training_rules,
            )
            if val.blocking_violations:
                response["blocking_violations"] = val.blocking_violations
                response["reconciliation"] = {
                    "status": "deactivated_requires_remediation",
                    "message": (
                        "The active programme contradicts the updated profile "
                        "constraints, was deactivated, and must be regenerated "
                        "before activation."
                    ),
                }
            else:
                response["reconciliation"] = {"status": "reconciled"}

    return response


@app.get("/api/settings/rules")
def get_user_training_rules(request: Request) -> JSONResponse:
    """Return the current user's versioned structured training rules."""
    user_id = _check_api_auth(request)
    rules = get_training_rules(user_id, DB_PATH)
    return JSONResponse(jsonable_encoder(rules))


@app.post("/api/settings/rules")
async def save_user_training_rules(request: Request) -> JSONResponse:
    """Validate and save user's structured training rules."""
    _check_rate_limit(request, limit=10)
    user_id = _check_api_auth(request)
    body = await request.json()
    try:
        validated = StructuredTrainingRules.model_validate(body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid training rules: {exc}")
    active = get_active_programme(user_id, DB_PATH)
    validation = None
    if active and active.get("definition"):
        prefs = get_user_preferences(user_id, DB_PATH)
        validation = validate_programme(
            active["definition"],
            profile_rules=prefs.get("constraints"),
            training_rules=validated,
        )
    saved = save_training_rules(user_id, validated, DB_PATH)

    response: dict[str, Any] = {"status": "ok", "rules": saved}
    if validation is not None:
        if validation.blocking_violations:
            response["blocking_violations"] = validation.blocking_violations
            response["reconciliation"] = {
                "status": "deactivated_requires_remediation",
                "message": (
                    "The active programme contradicts the saved training rules "
                    "and was deactivated. Regenerate it before activation."
                ),
            }
        else:
            response["reconciliation"] = {"status": "reconciled"}
    return JSONResponse(jsonable_encoder(response))


class DisplaySettingsUpdate(BaseModel):
    """Partial update of the caller's regional and accessibility preferences.

    Unknown fields (including any owner identifier) are rejected; the owner is
    always the authenticated session, never the request body.
    """

    model_config = ConfigDict(extra="forbid")

    timezone: str | None = None
    locale: str | None = None
    unit_system: str | None = None
    motion: str | None = None
    contrast: str | None = None
    text_size: str | None = None


@app.get("/api/settings/display")
def api_display_settings(request: Request) -> JSONResponse:
    """Return the caller's time zone, locale, units and accessibility choices."""
    user_id = _check_api_auth(request)
    return JSONResponse(
        jsonable_encoder(
            {
                "settings": get_user_settings(user_id, DB_PATH).as_dict(),
                "options": settings_options(),
            }
        )
    )


@app.post("/api/settings/display")
def save_display_settings(
    body: DisplaySettingsUpdate, request: Request
) -> dict[str, Any]:
    """Persist a partial settings update for the authenticated user.

    A time-zone change regroups only that user's derived workout days; stored
    source timestamps are never rewritten.
    """
    _check_rate_limit(request, limit=10)
    user_id = _check_api_auth(request)
    try:
        result = update_user_settings(
            user_id, body.model_dump(exclude_unset=True), DB_PATH
        )
    except SettingsValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc
    return {
        "status": "ok",
        "settings": result.settings.as_dict(),
        "changed": list(result.changed),
        "regrouped_days": result.regrouped_days,
    }


@app.post("/api/settings/push-subscribe")
async def save_push_subscription_route(request: Request) -> dict[str, str]:
    """Save a push subscription for the current user."""
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    endpoint = body.get("endpoint")
    keys = body.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Invalid push subscription data")

    save_push_subscription(
        user_id, endpoint=endpoint, p256dh=p256dh, auth=auth, db_path=DB_PATH
    )
    # The endpoint and keys are delivery credentials and are never audited.
    _audit(
        request,
        AuditEventType.PERMISSION_CHANGED,
        AuditOutcome.SUCCESS,
        AuditSubjectType.PERMISSION,
        "push_notifications",
        user_id=user_id,
        metadata={"change": "granted"},
    )
    return {"status": "ok"}


@app.get("/google-health/connect")
def google_health_connect(request: Request) -> RedirectResponse:
    """Start the Google Health OAuth flow and redirect to the consent screen."""
    _check_rate_limit(request, limit=5)
    if not (GH_CLIENT_ID and GH_CLIENT_SECRET):
        return RedirectResponse("/settings?gh=unconfigured", status_code=303)
    user_id = _check_api_auth(request)
    state = secrets.token_urlsafe(16)
    set_meta(_GH_STATE_KEY, state, DB_PATH, user_id=user_id)
    url = build_authorize_url(
        GH_CLIENT_ID,
        state,
        redirect_uri=_gh_redirect_uri(request),
    )
    return RedirectResponse(url, status_code=303)


@app.get("/google-health/callback", name="google_health_callback")
def google_health_callback(request: Request) -> RedirectResponse:
    """Receive Google's redirect, swap the code for a refresh token, store it."""
    if not (GH_CLIENT_ID and GH_CLIENT_SECRET):
        return RedirectResponse("/settings?gh=unconfigured", status_code=303)
    user_id = _check_api_auth(request)

    def audit_connect(outcome: AuditOutcome, reason: str | None = None) -> None:
        details: dict[str, Any] = {"provider": "google_health", "method": "oauth"}
        if reason:
            details["reason"] = reason
        _audit(
            request,
            AuditEventType.CONNECTOR_CONNECTED,
            outcome,
            AuditSubjectType.CONNECTOR,
            "google_health",
            user_id=user_id,
            metadata=details,
        )

    if request.query_params.get("error"):
        audit_connect(AuditOutcome.DENIED, "consent_denied")
        return RedirectResponse("/settings?gh=denied", status_code=303)
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    expected = get_meta(_GH_STATE_KEY, DB_PATH, user_id=user_id)
    set_meta(_GH_STATE_KEY, "", DB_PATH, user_id=user_id)  # one-time use
    if not code or not state or not expected or state != expected:
        audit_connect(AuditOutcome.FAILURE, "state_mismatch")
        return RedirectResponse("/settings?gh=error", status_code=303)
    tokens = exchange_code(
        GH_CLIENT_ID,
        GH_CLIENT_SECRET,
        code,
        redirect_uri=_gh_redirect_uri(request),
    )
    if not tokens or not tokens.get("refresh_token"):
        audit_connect(AuditOutcome.FAILURE, "token_exchange_failed")
        return RedirectResponse("/settings?gh=error", status_code=303)
    try:
        vault = _vault()
        if vault.describe(user_id, "google_health").configured:
            vault.rotate(
                user_id,
                "google_health",
                refresh_token=tokens["refresh_token"],
                actor="owner",
                source="oauth_callback",
            )
        else:
            try:
                vault.store(
                    user_id,
                    "google_health",
                    refresh_token=tokens["refresh_token"],
                    actor="owner",
                    source="oauth_callback",
                )
            except CredentialExistsError:
                vault.rotate(
                    user_id,
                    "google_health",
                    refresh_token=tokens["refresh_token"],
                    actor="owner",
                    source="oauth_callback",
                )
    except VaultError:
        logger.warning("Could not store a Google Health credential.")
        audit_connect(AuditOutcome.FAILURE, "credential_store_failed")
        return RedirectResponse("/settings?gh=error", status_code=303)
    audit_connect(AuditOutcome.SUCCESS)
    return RedirectResponse("/settings?gh=connected", status_code=303)


async def _disconnect_options(request: Request) -> tuple[DisconnectMode, bool]:
    """Read and validate the retention choice from a disconnect request."""

    content_type = request.headers.get("content-type", "").lower()
    raw_mode: Any = request.query_params.get("mode")
    confirmed = request.query_params.get("confirmed", "").lower() in {"1", "true", "yes"}

    if "application/json" in content_type:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if isinstance(body, dict):
            raw_mode = body.get("mode") or body.get("retention") or raw_mode
            confirmed = confirmed or bool(body.get("confirmed"))
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        raw_mode = form.get("mode") or form.get("retention") or raw_mode
        confirmed = confirmed or str(form.get("confirmed", "")).lower() in {
            "1",
            "true",
            "yes",
        }

    try:
        mode = parse_mode(raw_mode if raw_mode is not None else DisconnectMode.REVOKE_ONLY)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if mode.purges_imported_data and not confirmed:
        raise HTTPException(
            status_code=400,
            detail="Purging imported health data requires explicit confirmation.",
        )
    return mode, confirmed


@app.post("/google-health/disconnect")
async def google_health_disconnect(request: Request) -> RedirectResponse:
    """Revoke the stored Google Health token and apply the retention choice."""
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    mode, _confirmed = await _disconnect_options(request)
    try:
        _vault().revoke(user_id, "google_health")
    except CredentialNotFoundError:
        pass
    except VaultError:
        logger.warning("Could not revoke a Google Health credential.")
        _audit(
            request,
            AuditEventType.CONNECTOR_DISCONNECTED,
            AuditOutcome.FAILURE,
            AuditSubjectType.CONNECTOR,
            "google_health",
            user_id=user_id,
            metadata={"provider": "google_health", "method": "oauth"},
        )
        return RedirectResponse("/settings?gh=error", status_code=303)
    # Clear a token left by the pre-vault helper so it cannot be reused.
    set_meta(LEGACY_GOOGLE_HEALTH_META_KEY, "", DB_PATH, user_id=user_id)
    if mode.purges_imported_data:
        purge_imported_data_at(DB_PATH, user_id=user_id, provider="google_health")
    _audit(
        request,
        AuditEventType.CONNECTOR_DISCONNECTED,
        AuditOutcome.SUCCESS,
        AuditSubjectType.CONNECTOR,
        "google_health",
        user_id=user_id,
        metadata={"provider": "google_health", "method": "oauth"},
    )
    return RedirectResponse(
        f"/settings?gh=disconnected&retention="
        f"{(DisconnectMode.PURGE if mode.purges_imported_data else mode).value}",
        status_code=303,
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """Serve the service worker from the root so it can control every page.

    A worker only controls URLs within its own path, so it must be served from
    "/" (with the Service-Worker-Allowed header) rather than from /static.
    """
    return FileResponse(
        _BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


# --- AUTO-GENERATED API ROUTES FOR ANGULAR ---
from fastapi.encoders import jsonable_encoder


@app.get("/api/health/connections")
def api_health_connections(request: Request) -> JSONResponse:
    """List the authenticated tenant's connections and retention choices."""

    user_id = _check_api_auth(request)
    options = retention_options_payload()
    registry = build_builtin_registry()
    connections: list[dict[str, Any]] = []
    for row in list_connections(user_id, DB_PATH):
        entry: dict[str, Any] = {
            "id": row["id"],
            "provider": row["provider"],
            "state": row["state"],
            "last_sync_at": row["last_sync_at"],
            "retention_options": options,
        }
        try:
            connector = registry.get(row["provider"])
        except KeyError:
            connector = None
        if connector is not None:
            try:
                status = connector.status(
                    ConnectorContext(user_id=str(user_id), connection_id=row["id"])
                )
                entry["state"] = status.state.value
                entry["message"] = status.message
            except Exception:  # pragma: no cover - defensive status guard
                logger.exception(
                    "Could not read status for connector %s.", row["provider"]
                )
        connections.append(entry)
    return JSONResponse(
        jsonable_encoder(
            {"connections": connections, "retention_options": options}
        )
    )


@app.delete("/api/health/connections/{connection_id}")
def api_health_disconnect(request: Request, connection_id: str) -> JSONResponse:
    """Disconnect one tenant-owned connection using an explicit retention mode."""

    _check_rate_limit(request, limit=10)
    user_id = _check_api_auth(request)
    try:
        mode = parse_mode(
            request.query_params.get("mode") or DisconnectMode.REVOKE_ONLY.value
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    confirmed = request.query_params.get("confirmed", "").lower() in {
        "1",
        "true",
        "yes",
    }

    connection = get_connection(user_id, connection_id, DB_PATH)
    if connection is None:
        # Non-enumerating: another tenant's connection is indistinguishable
        # from a missing one.
        raise HTTPException(status_code=404, detail="Connection not found")

    provider = str(connection["provider"])
    disconnect_request = DisconnectRequest(
        user_id=user_id,
        connection_id=connection_id,
        mode=mode,
        confirmed=confirmed,
    )
    try:
        disconnect_request.validate(authenticated_user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    registry = build_builtin_registry()
    try:
        connector = registry.get(provider)
    except KeyError:
        connector = None

    purged = None
    if connector is not None:
        try:
            disconnect_connector(
                connector,
                ConnectorContext(user_id=str(user_id), connection_id=connection_id),
                disconnect_request,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if mode.purges_imported_data:
        purged = purge_imported_data_at(
            DB_PATH,
            user_id=user_id,
            provider=provider,
            connection_id=connection_id,
        )

    return JSONResponse(
        jsonable_encoder(
            {
                "status": "ok",
                "connection_id": connection_id,
                "provider": provider,
                "mode": (DisconnectMode.PURGE if mode.purges_imported_data else mode).value,
                "upstream_revoked": connector is not None,
                "purged": purged.as_dict() if purged else None,
            }
        )
    )


@app.get("/api/progress")
def api_progress(request: Request):
    user_id = _check_api_auth(request)
    unit_system = get_user_settings(user_id, DB_PATH).unit_system
    series = get_progress_history(db_path=DB_PATH, user_id=user_id)
    charts_data = []
    for name in sorted(series):
        entries = series[name]
        points = [
            {
                "date": e["date"][5:],
                "value": convert_weight(e["top_weight_kg"], unit_system),
                "label": _format_best(e, unit_system),
            }
            for e in entries
        ]
        e1rms = [_epley_1rm(e["top_weight_kg"], e["top_reps"]) for e in entries]
        e1rms_filtered = [v for v in e1rms if v is not None]
        best_e1rm = convert_weight(
            max(e1rms_filtered) if e1rms_filtered else None, unit_system
        )
        charts_data.append(
            {
                "name": name,
                "svg": charts.line_chart(points, unit=weight_unit(unit_system)),
                "best_e1rm": best_e1rm,
                "sessions": len(entries),
            }
        )
    body_readings = get_body_metrics(
        limit=14, db_path=DB_PATH, user_id=user_id, include_provenance=True
    )
    return JSONResponse(
        jsonable_encoder(
            {
                "charts": charts_data,
                "body": _body_charts(user_id=user_id, unit_system=unit_system),
                "weight_unit": weight_unit(unit_system),
                # Newest first, each with its source / correction provenance.
                "body_readings": list(reversed(body_readings)),
                "setup": _setup_payload(user_id, "progress"),
            }
        )
    )


class BodyMetricCorrection(BaseModel):
    """A user correction (or manual entry) for one day's body-metric reading.

    The owner is always the authenticated session; owner claims are rejected.
    Value bounds are enforced by ``database.validate_body_metric_changes``.
    """

    model_config = ConfigDict(extra="forbid")

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    weight_kg: float | None = None
    body_fat_pct: float | None = None
    muscle_pct: float | None = None
    resting_hr: float | None = None
    hrv: float | None = None
    reason: str | None = Field(default=None, max_length=280)


@app.post("/api/body-metrics/corrections")
def correct_body_metric(
    payload: BodyMetricCorrection, request: Request
) -> JSONResponse:
    """Correct a body-metric reading while preserving the original in its lineage."""
    _check_rate_limit(request, limit=20)
    user_id = _check_api_auth(request)
    changes = payload.model_dump(exclude={"date", "reason"}, exclude_none=True)
    try:
        result = correct_body_metrics(
            user_id, payload.date, changes, reason=payload.reason, db_path=DB_PATH
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["lineage"] = get_record_lineage(
        user_id, RecordType.BODY_METRIC, result["date"], DB_PATH
    )
    return JSONResponse(jsonable_encoder(result))


_RECORD_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:@+-]{1,200}$")


@app.get("/api/provenance/{record_type}/{record_key}")
def api_record_lineage(
    record_type: str, record_key: str, request: Request
) -> JSONResponse:
    """Return the authenticated user's revision history for one record."""
    _check_rate_limit(request, limit=60)
    user_id = _check_api_auth(request)
    try:
        family = RecordType(record_type)
    except ValueError:
        raise HTTPException(status_code=404, detail="Record not found") from None
    if not _RECORD_KEY_PATTERN.fullmatch(record_key):
        raise HTTPException(status_code=404, detail="Record not found")
    revisions = get_record_lineage(user_id, family, record_key, DB_PATH)
    if not revisions:
        raise HTTPException(status_code=404, detail="Record not found")
    current = next((item for item in revisions if item["applied"]), None)
    return JSONResponse(
        jsonable_encoder(
            {
                "record_type": family.value,
                "record_key": record_key,
                "current": current,
                "revisions": revisions,
            }
        )
    )


@app.get("/api/stats")
def api_stats(request: Request):
    user_id = _check_api_auth(request)
    settings = get_user_settings(user_id, DB_PATH)
    unit_system = settings.unit_system
    volumes = get_session_volumes(db_path=DB_PATH, user_id=user_id)
    prs = get_personal_records(db_path=DB_PATH, user_id=user_id)
    logs = get_daily_logs(limit=400, db_path=DB_PATH, user_id=user_id)
    series = get_progress_history(db_path=DB_PATH, user_id=user_id)
    today = local_today(settings.timezone)

    from programme_runtime import load_programme_runtime

    runtime = load_programme_runtime(user_id, DB_PATH, today=today)
    start = runtime.start_date

    total_sessions = len(volumes)
    total_volume = convert_weight(sum(v["volume"] for v in volumes), unit_system)
    days_on_programme = (today - start).days if start else 0

    # Prefer programme routine identifiers over free-text daily-log focus names.
    routine_focus = {
        str(day.get("routine_id")): str(day.get("focus") or "")
        for day in runtime.days
        if day.get("routine_id")
    }
    focus_names = {
        str(day.get("focus") or "").strip().lower()
        for day in runtime.days
        if day.get("focus")
    }
    focus_counts: dict[str, int] = {}
    try:
        workout_logs = get_recent_hevy_logs(
            limit=400, db_path=DB_PATH, user_id=user_id
        )
    except Exception:  # noqa: BLE001 - history is optional for statistics
        workout_logs = []
    for workout in workout_logs:
        if not isinstance(workout, dict):
            continue
        routine_id = str(workout.get("routine_id") or workout.get("routineId") or "")
        focus = routine_focus.get(routine_id)
        if not focus:
            title = str(workout.get("title") or "")
            if title.strip().lower() in focus_names:
                focus = title
        if focus:
            focus_counts[focus] = focus_counts.get(focus, 0) + 1

    if not focus_counts:
        # Legacy history without provider routine IDs still gets a distribution.
        for log in logs:
            f = (
                log.get("focus")
                if isinstance(log, dict)
                else getattr(log, "focus", None)
            )
            d = (
                log.get("day")
                if isinstance(log, dict)
                else getattr(log, "day", None)
            )
            if d is not None and f:
                focus_counts[f] = focus_counts.get(f, 0) + 1
    distribution = (
        charts.donut(
            [{"label": k, "value": v} for k, v in sorted(focus_counts.items())]
        )
        if focus_counts
        else None
    )

    groups = analytics.group_volumes(
        get_exercise_volumes(db_path=DB_PATH, user_id=user_id),
    )
    muscle_donut = (
        charts.donut(
            [
                {"label": g, "value": v}
                for g, v in sorted(groups.items(), key=lambda kv: -kv[1])
            ]
        )
        if groups
        else None
    )

    recent = volumes[-14:]
    volume_bars = (
        charts.bar_chart(
            [
                {
                    "label": v["date"][5:],
                    "value": convert_weight(v["volume"], unit_system),
                    "caption": v["date"],
                }
                for v in recent
            ],
            unit=weight_unit(unit_system),
        )
        if recent
        else None
    )

    biometrics = get_body_metrics(db_path=DB_PATH, user_id=user_id)
    block_phase_svg = ai_widgets.block_phase_tracker(volumes)
    recovery_grid_svg = ai_widgets.systemic_recovery_correlation(biometrics, volumes)
    vol_dist_svg = ai_widgets.volume_distribution(groups)

    weights = [(m["date"], m["weight_kg"]) for m in biometrics if m["weight_kg"]]
    priority_names: list[str] = []
    for day in runtime.days:
        for exercise in day.get("exercises") or []:
            name = str(exercise.get("name") or "")
            if (
                name
                and name in series
                and exercise.get("role")
                in {"primary_strength", "secondary_compound"}
                and name not in priority_names
            ):
                priority_names.append(name)
    strength_name = priority_names[0] if priority_names else None
    if strength_name is None:
        strength_name = next(
            (
                name
                for name, entries in series.items()
                if any(e.get("top_weight_kg") for e in entries)
            ),
            None,
        )
    strength_entries = series.get(strength_name, []) if strength_name else []
    dots_points, ratio_points = [], []
    for e in strength_entries:
        e1rm = analytics.epley_1rm(e["top_weight_kg"], e["top_reps"])
        bw = _weight_on_or_before(weights, e["date"])
        if not e1rm or not bw:
            continue
        score = analytics.dots_score(bw, e1rm)
        if score:
            dots_points.append(
                {"date": e["date"][5:], "value": score, "label": f"{score:g}"}
            )
        ratio_points.append(
            {
                "date": e["date"][5:],
                "value": round(e1rm / bw, 2),
                "label": f"{e1rm / bw:.2f}x",
            }
        )
    dots_chart = (
        charts.line_chart(dots_points, colour=charts.PURPLE)
        if len(dots_points) > 1
        else None
    )
    ratio_chart = (
        charts.line_chart(ratio_points, unit="x", colour=charts.ACCENT_2)
        if len(ratio_points) > 1
        else None
    )

    target_ordinal = (
        runtime.end_date.toordinal()
        if runtime.end_date and runtime.status == "active"
        else today.toordinal()
    )
    projections = [
        _project_lift(name, series[name], target_ordinal, unit_system=unit_system)
        for name in priority_names[:3]
    ]
    projections = [p for p in projections if p]

    pr_rows = [
        {
            "exercise": pr["exercise"],
            "e1rm": f"{convert_weight(pr['e1rm'], unit_system):g}",
            "detail": f"{format_weight(pr['weight_kg'], unit_system)} x {pr['reps']}",
            "date": pr["date"],
        }
        for pr in prs
    ]

    return JSONResponse(
        jsonable_encoder(
            {
                "total_sessions": total_sessions,
                "total_volume": total_volume,
                "weight_unit": weight_unit(unit_system),
                "days_on_programme": days_on_programme,
                "exercises_tracked": len(series),
                "has_distribution": bool(focus_counts),
                "distribution": distribution,
                "has_muscle": bool(groups),
                "muscle_donut": muscle_donut,
                "has_volume": bool(recent),
                "volume_bars": volume_bars,
                "block_phase_svg": block_phase_svg,
                "recovery_grid_svg": recovery_grid_svg,
                "vol_dist_svg": vol_dist_svg,
                "dots_chart": dots_chart,
                "ratio_chart": ratio_chart,
                "projections": projections,
                "prs": pr_rows,
                "setup": _setup_payload(user_id, "stats"),
            }
        )
    )


@app.get("/api/history")
def api_history(request: Request):
    user_id = _check_api_auth(request)
    logs = get_daily_logs(
        limit=30, db_path=DB_PATH, user_id=user_id, include_provenance=True
    )
    levels = _training_levels(user_id=user_id)
    return JSONResponse(
        jsonable_encoder(
            {
                "logs": logs,
                "calendar": charts.calendar_heatmap(levels),
                "setup": _setup_payload(user_id, "history"),
            }
        )
    )


@app.get("/api/plan")
def api_plan(request: Request) -> JSONResponse:
    """Render only the tenant's active dynamic programme; never a static fallback."""
    from programme_runtime import load_programme_runtime

    user_id = _check_api_auth(request)
    runtime = load_programme_runtime(user_id, DB_PATH)

    # Keep the raw activated definition available for backwards-compatible
    # clients, but every runtime decision below comes from the shared service.
    active = get_active_programme(user_id, db_path=DB_PATH)
    raw_definition = active.get("definition") if active else None
    definition: dict[str, Any] = (
        raw_definition if isinstance(raw_definition, dict) else {}
    )

    if runtime.setup_required:
        return JSONResponse(
            jsonable_encoder(
                {
                    "setup_required": True,
                    "is_active_programme": False,
                    "source": None,
                    "active_programme": None,
                    "split_name": "No active programme",
                    "week": 0,
                    "cycle_weeks": 0,
                    "current_block": None,
                    "blocks": [],
                    "days": [],
                    "rules": [],
                    "analysis": {},
                    "warnings": [],
                    "programme_spec": {},
                    "programme_status": runtime.status,
                    "start_date": None,
                    "end_date": None,
                    "requested_duration_weeks": 0,
                    "rotation_message": runtime.message,
                    "next_routine_id": None,
                    "setup": _setup_payload(
                        user_id,
                        "plan",
                        programme_status="setup_required",
                    ),
                }
            )
        )

    spec = definition.get("programme_spec") or {
        "start_date": runtime.start_date.isoformat() if runtime.start_date else None,
        "duration_weeks": runtime.cycle_weeks,
        "goal": runtime.goal,
    }
    split_name = (
        runtime.name
        or definition.get("name")
        or (active or {}).get("name")
        or "Hevy programme"
    )

    return JSONResponse(
        jsonable_encoder(
            {
                "setup_required": False,
                "is_active_programme": runtime.status == "active",
                "source": "hevy" if runtime.status != "setup_required" else None,
                "active_programme": active,
                "split_name": split_name,
                "week": runtime.week,
                "cycle_weeks": runtime.cycle_weeks,
                "current_block": (
                    runtime.block.get("name") if runtime.block else None
                ),
                "blocks": runtime.blocks,
                "days": runtime.days,
                "programme_status": runtime.status,
                "rotation_message": runtime.message,
                "next_session_date": runtime.next_session_date,
                "next_routine_id": (
                    runtime.next_day.get("routine_id") if runtime.next_day else None
                ),
                "start_date": runtime.start_date,
                "end_date": runtime.end_date,
                "requested_duration_weeks": runtime.cycle_weeks,
                "review_weeks": runtime.review_weeks,
                "provenance": programme_view(active),
                "rules": definition.get("rules") or [],
                "analysis": definition.get("analysis") or {},
                "warnings": definition.get("warnings") or [],
                "programme_spec": spec,
                "setup": _setup_payload(
                    user_id,
                    "plan",
                    programme_status=runtime.status,
                ),
            }
        )
    )


@app.get("/api/programmes")
def api_programmes(request: Request) -> JSONResponse:
    """Return the user's live Hevy routine library and active programme."""

    user_id = _check_api_auth(request)
    active = get_active_programme(user_id, db_path=DB_PATH)
    sync_state = _read_hevy_sync_state(user_id)

    empty_source: dict[str, Any] = {
        "username": None,
        "workout_count": 0,
        "routine_count": 0,
        "recent_workout_count": 0,
        "folders": [],
        "routines": [],
    }
    connection: dict[str, Any]
    source = empty_source

    try:
        _hevy_api_key_for_user(user_id)
    except HTTPException as exc:
        connection = {
            "state": _connection_state_for_status(exc.status_code),
            "detail": str(exc.detail),
            "username": None,
            "warnings": [],
            "counts": {},
        }
    else:
        try:
            snapshot = _load_hevy_snapshot_for_user(user_id)
            prefs = get_user_preferences(user_id, DB_PATH)
            source = serialise_hevy_source(
                snapshot.data,
                profile_rules=prefs.get("constraints"),
                training_rules=get_training_rules(user_id, DB_PATH),
            )
            connection = {
                "state": "partial" if snapshot.is_partial else "connected",
                "detail": None,
                "username": snapshot.data.username,
            }
            connection["warnings"] = list(snapshot.warnings)
            connection["counts"] = dict(snapshot.counts)
            connection["fetched_at"] = snapshot.fetched_at
            _record_hevy_sync_state(user_id, snapshot)
            sync_state = _read_hevy_sync_state(user_id)
        except HTTPException as exc:
            connection = {
                "state": _connection_state_for_status(exc.status_code),
                "detail": str(exc.detail),
                "username": None,
                "warnings": [],
                "counts": {},
            }

    connection.setdefault("warnings", [])
    connection.setdefault("counts", {})
    connection["owner"] = _authenticated_owner(user_id)
    connection["last_successful_sync"] = sync_state.get("synced_at")
    connection["stale"] = _hevy_sync_is_stale(sync_state)

    if active and active.get("source") == "hevy":
        if connection["state"] == "connected":
            source_drift = detect_source_drift(active.get("definition"), source)
        else:
            source_drift = {
                "status": "source_unavailable",
                "has_changes": False,
                "routines": [],
                "summary": {"renamed": 0, "content_changed": 0, "removed": 0},
                "detail": (
                    "Reconnect Hevy to check whether your selected routines have changed."
                ),
            }
    else:
        source_drift = detect_source_drift(None, source)

    return JSONResponse(
        jsonable_encoder(
            {
                "active_programme": active,
                "current_state": active,
                "source_drift": source_drift,
                "templates": [],
                "connection": connection,
                "source": source,
                "duration": {
                    "minimum_weeks": MIN_DURATION_WEEKS,
                    "maximum_weeks": MAX_DURATION_WEEKS,
                    "default_weeks": 12,
                },
                "goals": goal_options(),
                "cadence": {
                    "modes": [
                        {
                            "key": "rolling",
                            "label": "Rolling rotation",
                            "description": (
                                "Keep the routine order and advance one routine per "
                                "training day."
                            ),
                        },
                        {
                            "key": "weekly",
                            "label": "Weekly calendar",
                            "description": (
                                "Place routines on the weekdays you actually train."
                            ),
                        },
                    ],
                    "weekdays": [
                        {"index": index, "name": name}
                        for index, name in enumerate(
                            (
                                "Monday",
                                "Tuesday",
                                "Wednesday",
                                "Thursday",
                                "Friday",
                                "Saturday",
                                "Sunday",
                            )
                        )
                    ],
                    "roles": [
                        {"key": "required", "label": "Required"},
                        {"key": "optional", "label": "Optional"},
                        {"key": "rotating", "label": "Rotating"},
                    ],
                },
            }
        )
    )


@app.post("/api/programmes/refresh")
def api_programmes_refresh(request: Request) -> JSONResponse:
    """Re-read the tenant's Hevy source and record the sync result."""

    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    try:
        snapshot = _load_hevy_snapshot_for_user(user_id)
    except HTTPException as exc:
        return JSONResponse(
            jsonable_encoder(
                {
                    "state": _connection_state_for_status(exc.status_code),
                    "detail": str(exc.detail),
                    "source": None,
                    "counts": {},
                    "warnings": [],
                    "last_successful_sync": _read_hevy_sync_state(user_id).get(
                        "synced_at"
                    ),
                    "stale": True,
                }
            ),
            status_code=200,
        )

    _record_hevy_sync_state(user_id, snapshot)
    sync_state = _read_hevy_sync_state(user_id)
    return JSONResponse(
        jsonable_encoder(
            {
                "state": "partial" if snapshot.is_partial else "ok",
                "detail": None,
                "source": serialise_hevy_source(snapshot.data),
                "counts": dict(snapshot.counts),
                "warnings": list(snapshot.warnings),
                "last_successful_sync": sync_state.get("synced_at"),
                "stale": False,
            }
        )
    )


@app.get("/api/programmes/history")
def api_programme_history(
    request: Request,
    routine_ids: str | None = None,
) -> JSONResponse:
    """Expose deterministic workout matches and exercise baselines."""

    user_id = _check_api_auth(request)
    training_data = _load_hevy_training_for_user(user_id)
    selected = (
        [item.strip() for item in routine_ids.split(",") if item.strip()]
        if routine_ids
        else None
    )
    overrides = get_routine_match_overrides(user_id, DB_PATH)
    try:
        analysis = analyse_history(
            training_data,
            selected,
            overrides=overrides,
            user_id=user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        jsonable_encoder({"history": analysis, "overrides": overrides})
    )


@app.post("/api/programmes/history/override")
async def api_programme_history_override(request: Request) -> JSONResponse:
    """Confirm or correct an ambiguous workout-to-routine match."""

    _check_rate_limit(request, limit=10)
    user_id = _check_api_auth(request)
    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError):
        body = {}

    pattern_type = str(body.get("pattern_type") or "").strip().lower()
    pattern_value = str(body.get("pattern_value") or "").strip()
    routine_id = str(body.get("routine_id") or "").strip()
    if pattern_type not in {"workout", "title", "exercise_signature"}:
        raise HTTPException(
            status_code=422,
            detail=(
                "pattern_type must be one of: workout, title, exercise_signature."
            ),
        )
    if not pattern_value or not routine_id:
        raise HTTPException(
            status_code=422,
            detail="pattern_value and routine_id are required.",
        )

    training_data = _load_hevy_training_for_user(user_id)
    routine = next(
        (item for item in training_data.routines if item.id == routine_id),
        None,
    )
    if routine is None:
        raise HTTPException(
            status_code=422,
            detail="The selected routine is not available in the current Hevy source.",
        )
    if pattern_type == "workout" and not any(
        workout.id == pattern_value for workout in training_data.recent_workouts
    ):
        raise HTTPException(
            status_code=422,
            detail="The selected workout is not available in the current history.",
        )

    snapshot = build_snapshot(
        routine,
        training_data.exercise_templates,
        user_id=user_id,
    )
    save_routine_snapshots(user_id, [snapshot.to_dict()], DB_PATH)
    save_routine_match_override(
        user_id,
        pattern_type=pattern_type,
        pattern_value=pattern_value,
        routine_id=routine.id,
        routine_snapshot_id=snapshot.id,
        db_path=DB_PATH,
    )
    overrides = get_routine_match_overrides(user_id, DB_PATH)
    analysis = analyse_history(
        training_data,
        overrides=overrides,
        user_id=user_id,
    )
    save_routine_workout_matches(user_id, analysis["matches"], DB_PATH)
    return JSONResponse(
        jsonable_encoder(
            {
                "status": "ok",
                "snapshot": snapshot.to_dict(),
                "history": analysis,
                "overrides": overrides,
            }
        )
    )


# ---------------------------------------------------------------------------
# Programme lifecycle, adaptation, overrides and source drift (#975)
#
# Every route derives the owner from the authenticated session; request bodies
# forbid unknown fields so a client-supplied owner identifier is rejected, and
# every domain call is scoped by that tenant.  Adaptation is deterministic: no
# route calls a language model.
# ---------------------------------------------------------------------------


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProgrammeResumeBody(_StrictBody):
    extend: bool = True


class ProgrammeExtendBody(_StrictBody):
    weeks: int = Field(ge=1, le=8)


class ProgrammeReviewBody(_StrictBody):
    deleted_workout_ids: list[str] = Field(default_factory=list, max_length=200)


class DecisionAdjustmentBody(_StrictBody):
    adjustment: dict[str, Any] | None = None


class ProgrammeOverrideBody(_StrictBody):
    kind: str = Field(min_length=1, max_length=40)
    exercise_template_id: str = Field(default="", max_length=120)
    value: dict[str, Any] = Field(default_factory=dict)
    weeks: int | None = Field(default=None, ge=1, le=52)
    ends_on: date | None = None
    note: str = Field(default="", max_length=500)


class DriftResolutionBody(_StrictBody):
    routine_id: str = Field(min_length=1, max_length=120)
    action: Literal["keep", "merge", "rebase", "replace"]
    accept: list[str] = Field(default_factory=list, max_length=200)
    replacement_routine_id: str | None = Field(default=None, max_length=120)
    expected_hash: str | None = Field(default=None, max_length=128)
    acknowledge_unsupported: bool = False


class DriftResolveBody(_StrictBody):
    resolutions: list[DriftResolutionBody] = Field(min_length=1, max_length=20)


class WorkoutMatchConfirmBody(_StrictBody):
    workout_id: str = Field(min_length=1, max_length=120)
    routine_position: int = Field(ge=1, le=50)


def _programme_summary(programme: Any) -> dict[str, Any]:
    revision = programme.current_revision
    return {
        "id": programme.id,
        "name": programme.name,
        "status": programme.status,
        "goal": programme.goal,
        "duration_weeks": programme.duration_weeks,
        "start_date": programme.start_date.isoformat() if programme.start_date else None,
        "end_date": programme.end_date.isoformat() if programme.end_date else None,
        "revision_id": revision.id if revision else None,
        "revision_number": revision.revision_number if revision else None,
        "revision_reason": revision.reason if revision else None,
        "predecessor_programme_id": programme.predecessor_programme_id,
        "activated_at": programme.activated_at,
    }


def _override_view(override: Any) -> dict[str, Any]:
    return {
        "id": override.id,
        "programme_id": override.programme_id,
        "kind": override.kind,
        "exercise_template_id": override.exercise_template_id,
        "value": override.value,
        "starts_on": override.starts_on,
        "ends_on": override.ends_on,
        "status": override.status,
        "note": override.note,
        "created_at": override.created_at,
        "revoked_at": override.revoked_at,
    }


def _domain_call(action: Any) -> Any:
    """Run a domain operation and map its errors onto HTTP responses."""

    from programme_domain import (
        DriftConflictError,
        ProgrammeNotFoundError,
    )
    from programme_domain import (
        ProgrammeValidationError as DomainValidationError,
    )

    try:
        return action()
    except DriftConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProgrammeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _local_now(user_id: str) -> tuple[datetime, str]:
    timezone_name = get_user_settings(user_id, DB_PATH).timezone
    return datetime.now(tz=UTC), timezone_name


@app.get("/api/programmes/active/state")
def api_active_programme_state(request: Request) -> JSONResponse:
    """The active programme, its current week, pending decisions and overrides."""

    from programme_domain import (
        AdaptationService,
        LifecycleService,
        ProgrammeService,
        decision_view,
    )
    from programme_domain.weeks import local_date_of, week_for_date

    user_id = _check_api_auth(request)
    service = ProgrammeService(DB_PATH)
    programme = service.get_active_programme(user_id)
    if programme is None:
        return JSONResponse({"programme": None, "setup_required": True})
    now, _tz = _local_now(user_id)
    today = local_date_of(now, get_user_settings(user_id, DB_PATH).timezone) or now.date()
    week_number = (
        week_for_date(programme.start_date, int(programme.duration_weeks or 0), today)
        if programme.start_date
        else None
    )
    week = (
        _domain_call(
            lambda: LifecycleService(DB_PATH).materialise_week(
                user_id, programme.id, week_number, now=now
            )
        )
        if week_number
        else None
    )
    decisions = service.list_adaptation_decisions(user_id, programme.id)
    return JSONResponse(
        jsonable_encoder(
            {
                "programme": _programme_summary(programme),
                "week": week,
                "pending_decisions": [
                    decision_view(item)
                    for item in decisions
                    if item.application_state == "pending_confirmation"
                ],
                "overrides": [
                    _override_view(item)
                    for item in AdaptationService(DB_PATH).list_overrides(user_id, programme.id)
                    if item.status == "active"
                ],
                "revisions": [
                    {
                        "revision_number": item.revision_number,
                        "reason": item.reason,
                        "created_at": item.created_at,
                    }
                    for item in service.list_revision_history(user_id, programme.id)
                ],
            }
        )
    )


@app.get("/api/programmes/{programme_id}/weeks/{week_number}")
def api_programme_week(programme_id: int, week_number: int, request: Request) -> JSONResponse:
    """A dated week with effective prescriptions; reading never writes."""

    from programme_domain import LifecycleService

    user_id = _check_api_auth(request)
    now, _tz = _local_now(user_id)
    week = _domain_call(
        lambda: LifecycleService(DB_PATH).materialise_week(
            user_id, programme_id, week_number, now=now
        )
    )
    return JSONResponse(jsonable_encoder({"week": week}))


@app.get("/api/programmes/{programme_id}/next-session")
def api_programme_next_session(programme_id: int, request: Request) -> JSONResponse:
    from programme_domain import LifecycleService, ProgrammeService
    from programme_domain.weeks import local_date_of, week_for_date

    user_id = _check_api_auth(request)
    programme = ProgrammeService(DB_PATH).get_programme(user_id, programme_id)
    if programme is None or programme.start_date is None:
        raise HTTPException(status_code=404, detail="Programme not found for this user.")
    now, tz_name = _local_now(user_id)
    today = local_date_of(now, tz_name) or now.date()
    week_number = week_for_date(programme.start_date, int(programme.duration_weeks or 0), today)
    if week_number is None:
        return JSONResponse({"programme_id": programme_id, "week": None, "next_exposure": None})
    week = _domain_call(
        lambda: LifecycleService(DB_PATH).materialise_week(
            user_id, programme_id, week_number, now=now
        )
    )
    return JSONResponse(
        jsonable_encoder(
            {
                "programme_id": programme_id,
                "week": week_number,
                "cadence": week["cadence"],
                "next_exposure": week["next_exposure"],
            }
        )
    )


@app.post("/api/programmes/{programme_id}/pause")
def api_programme_pause(programme_id: int, request: Request) -> JSONResponse:
    from programme_domain import LifecycleService

    _check_rate_limit(request, limit=10)
    user_id = _check_api_auth(request)
    now, _tz = _local_now(user_id)
    programme = _domain_call(
        lambda: LifecycleService(DB_PATH).pause(user_id, programme_id, now=now)
    )
    return JSONResponse(jsonable_encoder({"programme": _programme_summary(programme)}))


@app.post("/api/programmes/{programme_id}/resume")
def api_programme_resume(
    programme_id: int, payload: ProgrammeResumeBody, request: Request
) -> JSONResponse:
    from programme_domain import LifecycleService

    _check_rate_limit(request, limit=10)
    user_id = _check_api_auth(request)
    now, _tz = _local_now(user_id)
    programme = _domain_call(
        lambda: LifecycleService(DB_PATH).resume(
            user_id, programme_id, now=now, extend=payload.extend
        )
    )
    return JSONResponse(jsonable_encoder({"programme": _programme_summary(programme)}))


@app.post("/api/programmes/{programme_id}/extend")
def api_programme_extend(
    programme_id: int, payload: ProgrammeExtendBody, request: Request
) -> JSONResponse:
    from programme_domain import LifecycleService

    _check_rate_limit(request, limit=10)
    user_id = _check_api_auth(request)
    now, _tz = _local_now(user_id)
    programme = _domain_call(
        lambda: LifecycleService(DB_PATH).extend(
            user_id, programme_id, payload.weeks, now=now
        )
    )
    return JSONResponse(jsonable_encoder({"programme": _programme_summary(programme)}))


@app.post("/api/programmes/{programme_id}/review")
def api_programme_review(
    programme_id: int, payload: ProgrammeReviewBody, request: Request
) -> JSONResponse:
    """Run the deterministic adaptation review now (an early review request)."""

    from programme_domain import AdaptationService, ProgrammeService
    from programme_review_runner import run_review

    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    active = ProgrammeService(DB_PATH).get_active_programme(user_id)
    if active is None or active.id != programme_id:
        raise HTTPException(status_code=404, detail="Active programme not found for this user.")
    now, _tz = _local_now(user_id)
    _domain_call(lambda: AdaptationService(DB_PATH).request_review(user_id, programme_id, now=now))
    result = _domain_call(
        lambda: run_review(
            user_id,
            DB_PATH,
            now=now,
            deleted_workout_ids=payload.deleted_workout_ids,
        )
    )
    return JSONResponse(jsonable_encoder({"review": result.to_dict() if result else None}))


@app.get("/api/programmes/{programme_id}/decisions")
def api_programme_decisions(
    programme_id: int,
    request: Request,
    state: str | None = None,
    decision_type: str | None = None,
    exercise: str | None = None,
) -> JSONResponse:
    from programme_domain import ProgrammeService, decision_view

    user_id = _check_api_auth(request)
    service = ProgrammeService(DB_PATH)
    if service.get_programme(user_id, programme_id) is None:
        raise HTTPException(status_code=404, detail="Programme not found for this user.")
    decisions = service.repository.list_adaptation_decisions(
        user_id,
        programme_id,
        states=(state,) if state else None,
        decision_types=(decision_type,) if decision_type else None,
        exercise_template_id=exercise,
    )
    return JSONResponse(
        jsonable_encoder({"decisions": [decision_view(item) for item in decisions]})
    )


@app.get("/api/programmes/{programme_id}/decisions/{decision_id}")
def api_programme_decision(
    programme_id: int, decision_id: int, request: Request
) -> JSONResponse:
    """Why a decision happened, rebuilt from its stored inputs."""

    from programme_domain import ProgrammeService, decision_view

    user_id = _check_api_auth(request)
    decision = ProgrammeService(DB_PATH).get_adaptation_decision(
        user_id, programme_id, decision_id
    )
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found for this user.")
    return JSONResponse(jsonable_encoder({"decision": decision_view(decision)}))


def _decision_action(
    programme_id: int, decision_id: int, request: Request, action: str, adjustment: Any = None
) -> JSONResponse:
    from programme_domain import AdaptationService, decision_view

    _check_rate_limit(request, limit=20)
    user_id = _check_api_auth(request)
    service = AdaptationService(DB_PATH)
    now, _tz = _local_now(user_id)
    if action == "confirm":
        decision = _domain_call(
            lambda: service.confirm(
                user_id, programme_id, decision_id, adjustment=adjustment, now=now
            )
        )
    elif action == "reject":
        decision = _domain_call(lambda: service.reject(user_id, programme_id, decision_id))
    elif action == "revert":
        decision = _domain_call(lambda: service.revert(user_id, programme_id, decision_id))
    else:
        decision = _domain_call(
            lambda: service.override(user_id, programme_id, decision_id, adjustment or {})
        )
    return JSONResponse(jsonable_encoder({"decision": decision_view(decision)}))


@app.post("/api/programmes/{programme_id}/decisions/{decision_id}/confirm")
def api_programme_decision_confirm(
    programme_id: int, decision_id: int, payload: DecisionAdjustmentBody, request: Request
) -> JSONResponse:
    return _decision_action(programme_id, decision_id, request, "confirm", payload.adjustment)


@app.post("/api/programmes/{programme_id}/decisions/{decision_id}/reject")
def api_programme_decision_reject(
    programme_id: int, decision_id: int, request: Request
) -> JSONResponse:
    return _decision_action(programme_id, decision_id, request, "reject")


@app.post("/api/programmes/{programme_id}/decisions/{decision_id}/revert")
def api_programme_decision_revert(
    programme_id: int, decision_id: int, request: Request
) -> JSONResponse:
    return _decision_action(programme_id, decision_id, request, "revert")


@app.post("/api/programmes/{programme_id}/decisions/{decision_id}/override")
def api_programme_decision_override(
    programme_id: int, decision_id: int, payload: DecisionAdjustmentBody, request: Request
) -> JSONResponse:
    return _decision_action(programme_id, decision_id, request, "override", payload.adjustment)


@app.get("/api/programmes/{programme_id}/overrides")
def api_programme_overrides(programme_id: int, request: Request) -> JSONResponse:
    from programme_domain import AdaptationService, ProgrammeService

    user_id = _check_api_auth(request)
    if ProgrammeService(DB_PATH).get_programme(user_id, programme_id) is None:
        raise HTTPException(status_code=404, detail="Programme not found for this user.")
    items = AdaptationService(DB_PATH).list_overrides(user_id, programme_id)
    return JSONResponse(jsonable_encoder({"overrides": [_override_view(item) for item in items]}))


@app.post("/api/programmes/{programme_id}/overrides")
def api_programme_override_create(
    programme_id: int, payload: ProgrammeOverrideBody, request: Request
) -> JSONResponse:
    """Record a durable pin, report, time limit or substitution for this tenant."""

    from programme_domain import AdaptationService
    from programme_domain.weeks import local_date_of

    _check_rate_limit(request, limit=20)
    user_id = _check_api_auth(request)
    now, tz_name = _local_now(user_id)
    override = _domain_call(
        lambda: AdaptationService(DB_PATH).add_override(
            user_id,
            programme_id,
            kind=payload.kind,
            exercise_template_id=payload.exercise_template_id,
            value=payload.value,
            today=local_date_of(now, tz_name) or now.date(),
            weeks=payload.weeks,
            ends_on=payload.ends_on,
            note=payload.note,
        )
    )
    return JSONResponse(jsonable_encoder({"override": _override_view(override)}))


@app.post("/api/programmes/{programme_id}/overrides/{override_id}/revoke")
def api_programme_override_revoke(
    programme_id: int, override_id: int, request: Request
) -> JSONResponse:
    from programme_domain import AdaptationService

    _check_rate_limit(request, limit=20)
    user_id = _check_api_auth(request)
    override = _domain_call(
        lambda: AdaptationService(DB_PATH).revoke_override(user_id, programme_id, override_id)
    )
    return JSONResponse(jsonable_encoder({"override": _override_view(override)}))


@app.post("/api/programmes/{programme_id}/workout-matches/confirm")
def api_programme_confirm_match(
    programme_id: int, payload: WorkoutMatchConfirmBody, request: Request
) -> JSONResponse:
    """Confirm or correct which routine a completed workout belonged to."""

    from hevy_history import parse_cached_workouts
    from programme_domain import AdaptationService

    _check_rate_limit(request, limit=20)
    user_id = _check_api_auth(request)
    workouts = parse_cached_workouts(get_cached_workout_payloads(user_id, db_path=DB_PATH))
    workout = next((item for item in workouts if item.id == payload.workout_id), None)
    if workout is None:
        raise HTTPException(
            status_code=422, detail="The workout is not available in the cached history."
        )
    now, _tz = _local_now(user_id)
    result = _domain_call(
        lambda: AdaptationService(DB_PATH).confirm_match(
            user_id, programme_id, workout, payload.routine_position, now=now
        )
    )
    return JSONResponse(jsonable_encoder({"review": result.to_dict()}))


@app.get("/api/programmes/{programme_id}/drift")
def api_programme_drift(programme_id: int, request: Request) -> JSONResponse:
    """Exact provider-versus-programme diff; nothing is changed or written to Hevy."""

    from programme_domain import DriftService

    user_id = _check_api_auth(request)
    training_data = _load_hevy_training_for_user(user_id)
    live = serialise_hevy_source(training_data)
    report = _domain_call(lambda: DriftService(DB_PATH).diff(user_id, programme_id, live))
    return JSONResponse(jsonable_encoder({"drift": report}))


@app.post("/api/programmes/{programme_id}/drift/resolve")
def api_programme_drift_resolve(
    programme_id: int, payload: DriftResolveBody, request: Request
) -> JSONResponse:
    """Keep, merge, rebase or replace; a change appends a validated revision."""

    from programme_domain import DriftService, Resolution

    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    training_data = _load_hevy_training_for_user(user_id)
    live = serialise_hevy_source(training_data)
    now, _tz = _local_now(user_id)
    result = _domain_call(
        lambda: DriftService(DB_PATH).resolve(
            user_id,
            programme_id,
            live,
            [
                Resolution(
                    routine_id=item.routine_id,
                    action=item.action,
                    accept=tuple(item.accept),
                    replacement_routine_id=item.replacement_routine_id,
                    expected_hash=item.expected_hash,
                    acknowledge_unsupported=item.acknowledge_unsupported,
                )
                for item in payload.resolutions
            ],
            now=now,
        )
    )
    return JSONResponse(jsonable_encoder({"result": result}))


@app.get("/api/settings")
def api_settings(request: Request):
    # Read-only and allowed while an account is locked for deletion: the Settings
    # page hosts the deletion progress view and must stay reachable to show it.
    user_id = _check_api_auth(request, allow_closing=True)

    prefs = get_user_preferences(user_id, DB_PATH)
    ai_providers = available_providers()
    vault = _vault()
    summaries = vault.list_credentials(user_id)

    user_keys: dict[str, dict[str, Any]] = {}
    known_providers = ["hevy", *[item["id"] for item in ai_providers]]
    for provider_id in known_providers:
        summary = summaries[provider_id]
        public = summary.to_public_dict()
        user_keys[provider_id] = {
            "has_key": public["has_key"],
            "masked": public["masked"],
            "model": public["model"],
            "status": public["status"],
            "version": public["version"],
            "last_tested_at": public["last_tested_at"],
            "last_test_status": public["last_test_status"],
            "revoked_at": public["revoked_at"],
        }

    gh_connected = summaries["google_health"].configured
    credential_availability = vault.availability().to_public_dict()
    try:
        credential_events = [
            event.to_public_dict() for event in vault.recent_events(user_id, limit=20)
        ]
    except VaultError:
        credential_events = []
    gh_configured = bool(GH_CLIENT_ID and GH_CLIENT_SECRET)
    gh_status = request.query_params.get("gh")

    hevy_connection = get_hevy_connection(user_id, DB_PATH)
    hevy_connection_payload: dict[str, Any] = {
        "state": "not_connected",
        "account_username": None,
        "account_id": None,
        "api_version": None,
        "capabilities": {},
        "tested_at": None,
    }
    if hevy_connection:
        hevy_connection_payload = {
            "state": hevy_connection.get("state") or "unverified",
            "account_username": hevy_connection.get("account_username"),
            "account_id": hevy_connection.get("account_id"),
            "api_version": hevy_connection.get("api_version"),
            "capabilities": hevy_connection.get("capabilities") or {},
            "tested_at": hevy_connection.get("tested_at"),
        }

    registry = build_builtin_registry()
    ctx = ConnectorContext(user_id=user_id)
    health_connections = {
        str(row["provider"]): row for row in list_connections(user_id, DB_PATH)
    }
    retention_choices = retention_options_payload()
    connectors_info = []
    for connector in registry.all():
        try:
            status = connector.status(ctx)
            if status.state == "pending":
                continue
            active_connection = health_connections.get(connector.provider)
            connectors_info.append(
                {
                    "provider": connector.provider,
                    "name": connector.provider.title().replace("_", " "),
                    "state": status.state,
                    "message": status.message,
                    "authorize_supported": connector.capabilities.authorize,
                    "purge_supported": connector.capabilities.purge,
                    "connection_id": (
                        active_connection["id"] if active_connection else None
                    ),
                    "retention_options": retention_choices,
                }
            )
        except Exception:
            logger.exception(
                "Could not read status for connector %s.", connector.provider
            )

    return JSONResponse(
        jsonable_encoder(
            {
                "user_prefs": prefs,
                "training_rules": get_training_rules(user_id, DB_PATH),
                "user_keys": user_keys,
                "hevy_connection": hevy_connection_payload,
                "ai_providers": ai_providers,
                "gh_connected": gh_connected,
                "gh_configured": gh_configured,
                "credential_vault": credential_availability,
                "credential_events": credential_events,
                "gh_status": gh_status,
                "connectors_info": connectors_info,
                "retention_options": retention_choices,
                "vapid_public_key": os.environ.get("VAPID_PUBLIC_KEY"),
                "key_status": request.query_params.get("key_status"),
                "pref_status": request.query_params.get("pref_status"),
            }
        )
    )


@app.post("/api/settings/sync-history")
async def sync_history_endpoint(request: Request) -> dict[str, Any]:
    """Rebuild user's workout history from Hevy."""
    _check_rate_limit(request, limit=2)
    user_id = _check_api_auth(request)

    credential = _vault_credential(user_id, "hevy", purpose="hevy_sync")
    hevy_key = (
        credential.api_key.reveal()
        if credential is not None and credential.api_key is not None
        else ""
    )
    if not hevy_key:
        raise HTTPException(
            status_code=400,
            detail="No Hevy API key configured. Save your Hevy API key in Settings first.",
        )

    # The rebuild runs off the event loop, so a long sync never stalls other
    # tenants and the "syncing" state stays observable while it runs.  The lease
    # also keeps two rebuilds of one tenant's history from interleaving.
    try:
        result = await run_in_threadpool(_run_hevy_sync, user_id, hevy_key)
    except OperationBusyError as exc:
        raise _operation_busy(exc) from exc
    sync_failed = "error" in result
    # The provider's error text is never audited; only a fixed reason code.
    _audit(
        request,
        AuditEventType.CONNECTOR_SYNC,
        AuditOutcome.FAILURE if sync_failed else AuditOutcome.SUCCESS,
        AuditSubjectType.CONNECTOR,
        "hevy",
        user_id=user_id,
        metadata=(
            {"provider": "hevy", "reason": "provider_error"}
            if sync_failed
            else {
                "provider": "hevy",
                "records_found": int(result.get("workouts_found", 0)),
                "records_processed": int(result.get("processed", 0)),
            }
        ),
    )
    if sync_failed:
        err_msg = str(result["error"])
        save_notification(
            user_id,
            title="⚠️ Hevy Sync Failed",
            message=f"Could not sync history: {err_msg}",
            type="sync",
            link="/settings",
            db_path=DB_PATH,
        )
        raise HTTPException(status_code=400, detail=err_msg)

    workouts_found = int(result.get("workouts_found", 0))
    processed = int(result.get("processed", 0))
    save_notification(
        user_id,
        title="✓ Hevy History Synced",
        message=f"Successfully synced and rebuilt {processed} of {workouts_found} workouts from Hevy.",
        type="sync",
        link="/history",
        db_path=DB_PATH,
    )
    return {"status": "ok", **result}


class AccountDeletionBody(BaseModel):
    """A confirmed deletion request. The owner is always the signed-in session."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["account", "health_data"]
    confirmation: str = Field(min_length=1, max_length=320)


_DELETION_HTTP_STATUS = {
    "invalid_scope": 422,
    "confirmation_mismatch": 400,
    "not_eligible": 403,
    "already_requested": 409,
    "not_cancellable": 409,
    "not_found": 404,
}


def _deletion_http_error(exc: account_deletion.DeletionError) -> HTTPException:
    return HTTPException(
        status_code=_DELETION_HTTP_STATUS.get(exc.code, 400), detail=exc.code
    )


@app.get("/api/account/deletion")
def api_account_deletion_status(request: Request) -> JSONResponse:
    """Eligibility, the exact confirmation text, and the latest deletion request."""
    user_id = _check_api_auth(request, allow_closing=True)
    return JSONResponse(jsonable_encoder(account_deletion.describe(DB_PATH, user_id)))


@app.post("/api/account/deletion")
def api_account_deletion_request(
    body: AccountDeletionBody, request: Request
) -> JSONResponse:
    """Confirm and start deleting health data or closing the signed-in account."""
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request)
    try:
        status = account_deletion.request_deletion(
            DB_PATH, user_id, body.scope, body.confirmation
        )
        # Runs now only when the request is already due (health data, or a
        # zero-day window); otherwise the scheduler executes it after the window.
        status = (
            account_deletion.execute_deletion(DB_PATH, status["request_id"]) or status
        )
    except account_deletion.DeletionError as exc:
        raise _deletion_http_error(exc) from exc

    finished = status["state"] == account_deletion.DeletionState.COMPLETED
    response = JSONResponse(
        jsonable_encoder(status), status_code=200 if finished else 202
    )
    if finished and status["scope"] == account_deletion.DeletionScope.ACCOUNT:
        request.session.clear()
        response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
    return response


@app.post("/api/account/deletion/cancel")
def api_account_deletion_cancel(request: Request) -> JSONResponse:
    """Cancel the signed-in user's pending closure while its window is open."""
    _check_rate_limit(request, limit=5)
    user_id = _check_api_auth(request, allow_closing=True)
    try:
        status = account_deletion.cancel_deletion(DB_PATH, user_id)
    except account_deletion.DeletionError as exc:
        raise _deletion_http_error(exc) from exc
    return JSONResponse(jsonable_encoder(status))


@app.get("/api/notifications")
def api_get_notifications(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    unread_only: bool = Query(False),
):
    user_id = _check_api_auth(request)
    notifs = get_notifications(
        user_id, limit=limit, unread_only=unread_only, db_path=DB_PATH
    )
    unread_count = get_unread_notification_count(user_id, db_path=DB_PATH)
    return JSONResponse(
        jsonable_encoder(
            {
                "notifications": notifs,
                "unread_count": unread_count,
            }
        )
    )


@app.post("/api/notifications/{notification_id}/read")
def api_mark_notification_read(notification_id: int, request: Request):
    user_id = _check_api_auth(request)
    success = mark_notification_read(notification_id, user_id, db_path=DB_PATH)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")
    unread_count = get_unread_notification_count(user_id, db_path=DB_PATH)
    return {"status": "ok", "unread_count": unread_count}


@app.post("/api/notifications/read-all")
def api_mark_all_notifications_read(request: Request):
    user_id = _check_api_auth(request)
    updated = mark_all_notifications_read(user_id, db_path=DB_PATH)
    return {"status": "ok", "updated": updated, "unread_count": 0}


@app.delete("/api/notifications/{notification_id}")
def api_delete_notification(notification_id: int, request: Request):
    user_id = _check_api_auth(request)
    success = delete_notification(notification_id, user_id, db_path=DB_PATH)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")
    unread_count = get_unread_notification_count(user_id, db_path=DB_PATH)
    return {"status": "ok", "unread_count": unread_count}


@app.post("/api/notifications/clear")
@app.delete("/api/notifications")
def api_clear_notifications(request: Request):
    user_id = _check_api_auth(request)
    cleared = clear_all_notifications(user_id, db_path=DB_PATH)
    return {"status": "ok", "cleared": cleared, "unread_count": 0}


@app.post("/api/notifications/test-coach")
def api_test_coach_notification(request: Request):
    user_id = _check_api_auth(request)
    nid = save_notification(
        user_id,
        title="Test notification",
        message="Notifications are working. This is a test message, not coaching advice.",
        type="coach",
        link="/dashboard",
        db_path=DB_PATH,
    )
    unread_count = get_unread_notification_count(user_id, db_path=DB_PATH)
    return {"status": "ok", "id": nid, "unread_count": unread_count}


@app.get("/api/chat-history")
def api_chat_history(request: Request):
    user_id = _check_api_auth(request)
    return JSONResponse(
        jsonable_encoder(
            {"messages": get_chat_messages(db_path=DB_PATH, user_id=user_id)}
        )
    )


def _frontend_asset(full_path: str) -> Path | None:
    """Return a real file inside the SPA bundle, rejecting path traversal.

    ``full_path`` comes straight from the request target, so ``..`` segments (or
    an encoded equivalent) must never be allowed to escape the bundled frontend
    directory. The resolved candidate is checked for containment as well as
    existence so symlinks cannot be used as an escape hatch.
    """

    root = FRONTEND_DIST.resolve()
    try:
        candidate = (root / full_path).resolve()
    except (OSError, ValueError):
        return None
    if root in candidate.parents and candidate.is_file():
        return candidate
    return None


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
async def catch_all(full_path: str, request: Request):
    if full_path.startswith(
        ("api/", "static/", "login/google", "auth", "google-health/")
    ):
        raise HTTPException(status_code=404, detail="Not found")

    if full_path:
        file_path = _frontend_asset(full_path)
        if file_path is not None:
            return FileResponse(file_path)

    return FileResponse(FRONTEND_DIST / "index.html")
