"""User-initiated AI provider connection checks.

Saving an API key and a free-text model string does not prove the provider can
serve them. This module proves it, safely, before anything is activated:

1. the model ID is screened by :mod:`ai_model_policy` (no provider call spent);
2. a fresh result for the same tenant/provider/model/key is reused briefly;
3. otherwise one minimal request is sent through the repository provider
   abstraction (``ai_provider.get_provider(...).probe()``) and any failure is
   mapped to a stable code with a fixed, actionable, non-secret message.

Guarantees callers rely on:

* A check never persists anything. Callers decide to store a key only after a
  successful result, so an invalid key is never written.
* Results are cached per ``(user_id, provider, model, key fingerprint)``. The
  fingerprint is a keyed HMAC, so the raw key is never held in the cache, and a
  different key or model is always a cache miss (= automatic revalidation).
  Tenants can never read each other's entries.
* Live provider calls are limited per user. Cache hits and static rejections
  cost nothing and are not counted.
* No provider text, key material or exception message leaves this module.

State is in-process: with several web replicas each enforces its own window and
cache, which only makes the limits per-replica, never looser per user.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any

import ai_provider
from ai_model_policy import evaluate_model
from ai_provider import (
    RETRYABLE_ERROR_CODES,
    AIProviderError,
    AIProviderErrorCode,
    classify_provider_error,
    provider_display_name,
)

logger = logging.getLogger(__name__)

SUCCESS_TTL_SECONDS = 300.0
FAILURE_TTL_SECONDS = 30.0
LIVE_CHECKS_PER_WINDOW = 5
WINDOW_SECONDS = 60.0
MAX_CACHE_ENTRIES = 512
MAX_TRACKED_USERS = 2048

OK_CODE = "ok"
TOO_MANY_CHECKS_CODE = "too_many_checks"

# One printable-ASCII token. Real keys are far shorter than 512 characters;
# whitespace or control characters can never be part of a valid key and would
# otherwise surface as opaque transport errors (or header injection).
_API_KEY = re.compile(r"[\x21-\x7e]{8,512}")

_MESSAGES: dict[AIProviderErrorCode, str] = {
    AIProviderErrorCode.MISSING_CREDENTIALS: (
        "Enter an API key to test, or save one first."
    ),
    AIProviderErrorCode.CREDENTIALS_MALFORMED: (
        "That is not a valid API key format. Paste only the key itself, with no "
        "spaces or line breaks."
    ),
    AIProviderErrorCode.INVALID_CREDENTIALS: (
        "{provider} rejected the API key. Check that you pasted the whole key, "
        "that it has not been revoked, and that it belongs to {provider}."
    ),
    AIProviderErrorCode.PERMISSION_DENIED: (
        "{provider} recognised the key but refused access to '{model}'. The key "
        "may be restricted, or your account or region may not include this model."
    ),
    AIProviderErrorCode.QUOTA_EXCEEDED: (
        "{provider} reports no credit or quota left on this account. Add credit "
        "or check billing with {provider}, then test again."
    ),
    AIProviderErrorCode.RATE_LIMITED: (
        "{provider} is rate limiting this key. Wait a minute and test again; if "
        "it keeps happening, check your plan limits and quota with {provider}."
    ),
    AIProviderErrorCode.MODEL_INVALID: (
        "'{model}' is not a valid model name. Use letters, numbers and . _ : / - "
        "only."
    ),
    AIProviderErrorCode.MODEL_UNSUPPORTED: (
        "'{model}' is not a text chat model, so it cannot power coaching. "
        "Choose a text-generation model{replacement}."
    ),
    AIProviderErrorCode.MODEL_DEPRECATED: (
        "'{model}' is deprecated by {provider} and stops working on {date}. "
        "Choose a current model{replacement}."
    ),
    AIProviderErrorCode.MODEL_RETIRED: (
        "'{model}' has been retired by {provider} and no longer works. Choose a "
        "current model{replacement}."
    ),
    AIProviderErrorCode.MODEL_NOT_FOUND: (
        "{provider} does not offer a model named '{model}' to this key. Check "
        "the spelling or choose another model."
    ),
    AIProviderErrorCode.INVALID_REQUEST: (
        "{provider} rejected the test request. Check the model name and try again."
    ),
    AIProviderErrorCode.TIMEOUT: (
        "{provider} did not answer in time. Try again shortly."
    ),
    AIProviderErrorCode.PROVIDER_UNAVAILABLE: (
        "{provider} could not be reached or is unavailable right now. Try again "
        "shortly."
    ),
    AIProviderErrorCode.SDK_UNAVAILABLE: (
        "This server cannot talk to {provider} because its client library is "
        "missing. Ask the operator to check the deployment."
    ),
    AIProviderErrorCode.UNKNOWN: (
        "{provider} returned an unexpected error. Try again; if it persists, "
        "check the provider's status page."
    ),
}


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    """Outcome of one check. Contains no secret and no provider-supplied text."""

    provider: str
    model: str
    ok: bool
    code: str
    message: str
    retryable: bool
    checked_at: str
    cached: bool = False
    replacement_model: str | None = None
    retry_after: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.ok else "error",
            "provider": self.provider,
            "model": self.model,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "cached": self.cached,
            "checked_at": self.checked_at,
            "replacement_model": self.replacement_model,
            "retry_after": self.retry_after,
        }


class ConnectionCheckRateLimited(Exception):
    """The user used up their live provider checks for the current window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(TOO_MANY_CHECKS_CODE)
        self.retry_after = retry_after


def http_status_for(check: ConnectionCheck) -> int:
    """HTTP status for routes that must refuse to proceed after a failed check."""
    if check.ok:
        return 200
    if check.code == AIProviderErrorCode.RATE_LIMITED:
        return 429
    if check.retryable or check.code == AIProviderErrorCode.SDK_UNAVAILABLE:
        return 503
    return 422


def _format_date(value: date) -> str:
    return f"{value.day} {value:%B %Y}"


def describe(
    code: AIProviderErrorCode,
    *,
    provider: str,
    model: str,
    replacement: str | None = None,
    retires_on: date | None = None,
) -> str:
    """Fixed user-facing text for *code*. Never includes provider output."""
    return _MESSAGES[code].format(
        provider=provider_display_name(provider),
        model=model,
        replacement=f", for example '{replacement}'" if replacement else "",
        date=_format_date(retires_on) if retires_on else "an upcoming date",
    )


class AIConnectionChecker:
    """Rate-limited, briefly-caching front door to ``AIProvider.probe``."""

    def __init__(
        self,
        *,
        success_ttl: float = SUCCESS_TTL_SECONDS,
        failure_ttl: float = FAILURE_TTL_SECONDS,
        max_live_checks: int = LIVE_CHECKS_PER_WINDOW,
        window: float = WINDOW_SECONDS,
        max_entries: int = MAX_CACHE_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_live_checks < 1 or window <= 0 or max_entries < 1:
            raise ValueError("limits must be positive")
        self._success_ttl = success_ttl
        self._failure_ttl = failure_ttl
        self._max_live_checks = max_live_checks
        self._window = window
        self._max_entries = max_entries
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        # Per-process secret: fingerprints are useless outside this process.
        self._salt = secrets.token_bytes(32)
        self._cache: OrderedDict[
            tuple[str, str, str, str], tuple[float, ConnectionCheck]
        ] = OrderedDict()
        self._live: dict[str, deque[float]] = {}

    # -- public API --------------------------------------------------------

    def validate_model(self, provider: str, model: str) -> ConnectionCheck | None:
        """Static screening only (no provider call). ``None`` means "no objection"."""
        provider_id = provider.strip().lower()
        decision = evaluate_model(
            provider_id,
            model,
            today=self._now().astimezone(timezone.utc).date(),
        )
        if decision is None:
            return None
        return self._failure(
            provider_id,
            model.strip(),
            decision.code,
            replacement=decision.replacement,
            retires_on=decision.retires_on,
        )

    def check(
        self,
        user_id: str,
        provider: str,
        model: str,
        api_key: str,
    ) -> ConnectionCheck:
        """Check *api_key* against *model* for *user_id*.

        Raises ``ValueError`` for programmer errors (no user, unknown provider)
        and :class:`ConnectionCheckRateLimited` when the user's live-check
        budget is spent. Every provider-side problem is returned, not raised.
        """
        if not user_id:
            raise ValueError("A user id is required to scope connection checks.")
        provider_id = provider.strip().lower()
        if provider_id not in ai_provider.PROVIDERS:
            raise ValueError(f"Unknown AI provider '{provider}'.")
        model = model.strip()
        api_key = api_key.strip()

        if not api_key:
            return self._failure(provider_id, model, AIProviderErrorCode.MISSING_CREDENTIALS)
        if not _API_KEY.fullmatch(api_key):
            return self._failure(provider_id, model, AIProviderErrorCode.CREDENTIALS_MALFORMED)
        rejected = self.validate_model(provider_id, model)
        if rejected is not None:
            return rejected

        cache_key = (user_id, provider_id, model, self._fingerprint(api_key))
        cached = self._cached(cache_key)
        if cached is not None:
            return replace(cached, cached=True)

        self._reserve_live_check(user_id)
        result = self._probe(provider_id, model, api_key)
        self._store(cache_key, result)
        return result

    def invalidate(self, user_id: str, provider: str | None = None) -> int:
        """Forget cached results for *user_id* (optionally one provider only)."""
        provider_id = provider.strip().lower() if provider else None
        with self._lock:
            stale = [
                key
                for key in self._cache
                if key[0] == user_id and (provider_id is None or key[1] == provider_id)
            ]
            for key in stale:
                del self._cache[key]
        return len(stale)

    # -- internals ---------------------------------------------------------

    def _fingerprint(self, api_key: str) -> str:
        return hmac.new(self._salt, api_key.encode(), hashlib.sha256).hexdigest()[:32]

    def _probe(self, provider_id: str, model: str, api_key: str) -> ConnectionCheck:
        try:
            provider = ai_provider.get_provider(provider_id, api_key=api_key, model=model)
            provider.probe()
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            error = exc if isinstance(exc, AIProviderError) else classify_provider_error(exc)
            logger.info(
                "AI connection check failed: provider=%s code=%s status=%s",
                provider_id,
                error.code.value,
                error.status_code,
            )
            return self._failure(
                provider_id,
                model,
                error.code,
                retry_after=error.retry_after,
            )
        logger.info("AI connection check passed: provider=%s", provider_id)
        return ConnectionCheck(
            provider=provider_id,
            model=model,
            ok=True,
            code=OK_CODE,
            message=(
                f"Connected: {provider_display_name(provider_id)} accepted this key "
                f"and the model '{model}'."
            ),
            retryable=False,
            checked_at=self._timestamp(),
        )

    def _failure(
        self,
        provider_id: str,
        model: str,
        code: AIProviderErrorCode,
        *,
        replacement: str | None = None,
        retires_on: date | None = None,
        retry_after: int | None = None,
    ) -> ConnectionCheck:
        return ConnectionCheck(
            provider=provider_id,
            model=model,
            ok=False,
            code=code.value,
            message=describe(
                code,
                provider=provider_id,
                model=model,
                replacement=replacement,
                retires_on=retires_on,
            ),
            retryable=code in RETRYABLE_ERROR_CODES,
            checked_at=self._timestamp(),
            replacement_model=replacement,
            retry_after=retry_after,
        )

    def _timestamp(self) -> str:
        return self._now().astimezone(timezone.utc).isoformat(timespec="seconds")

    def _cached(self, key: tuple[str, str, str, str]) -> ConnectionCheck | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, result = entry
            if self._clock() >= expires_at:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return result

    def _store(self, key: tuple[str, str, str, str], result: ConnectionCheck) -> None:
        if result.ok:
            ttl = self._success_ttl
        elif result.retryable:
            # Outages and provider throttling are exactly when a retry may work.
            return
        else:
            ttl = self._failure_ttl
        if ttl <= 0:
            return
        now = self._clock()
        with self._lock:
            self._cache[key] = (now + ttl, result)
            self._cache.move_to_end(key)
            if len(self._cache) > self._max_entries:
                for expired in [k for k, (exp, _) in self._cache.items() if exp <= now]:
                    del self._cache[expired]
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)

    def _reserve_live_check(self, user_id: str) -> None:
        now = self._clock()
        with self._lock:
            if len(self._live) > MAX_TRACKED_USERS:
                for idle in [
                    uid
                    for uid, stamps in self._live.items()
                    if not stamps or now - stamps[-1] >= self._window
                ]:
                    del self._live[idle]
            stamps = self._live.setdefault(user_id, deque())
            while stamps and now - stamps[0] >= self._window:
                stamps.popleft()
            if len(stamps) >= self._max_live_checks:
                retry_after = max(1, math.ceil(stamps[0] + self._window - now))
                raise ConnectionCheckRateLimited(retry_after)
            stamps.append(now)


#: Process-wide instance used by the web app.
checker = AIConnectionChecker()
