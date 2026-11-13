"""AI provider abstraction layer.

Supports Gemini, Claude (Anthropic), OpenAI, and DeepSeek behind a common
interface so users can bring their own API key for whichever service they
prefer. See the ai-provider-wiring skill (.agents/skills/) for how call
sites should resolve a user's chosen provider before calling ``generate()``.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from enum import StrEnum
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

# Connection probes send this fixed, synthetic prompt: never user data.
PROBE_PROMPT = "Reply with the single word: ok"
PROBE_MAX_TOKENS = 16
PROBE_TIMEOUT_SECONDS = 15.0
MAX_RETRY_AFTER_SECONDS = 300


# ---------------------------------------------------------------------------
# Provider failure taxonomy
# ---------------------------------------------------------------------------


class AIProviderErrorCode(StrEnum):
    """Stable, non-secret reasons a provider/model pair cannot serve requests.

    ``classify_provider_error`` derives most members from provider failures;
    ``MODEL_*`` lifecycle members come from :mod:`ai_model_policy`, and the two
    credential-shape members from request validation.
    """

    MISSING_CREDENTIALS = "missing_credentials"
    CREDENTIALS_MALFORMED = "credentials_malformed"
    INVALID_CREDENTIALS = "invalid_credentials"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"
    MODEL_INVALID = "model_invalid"
    MODEL_UNSUPPORTED = "model_unsupported"
    MODEL_DEPRECATED = "model_deprecated"
    MODEL_RETIRED = "model_retired"
    MODEL_NOT_FOUND = "model_not_found"
    INVALID_REQUEST = "invalid_request"
    TIMEOUT = "timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    SDK_UNAVAILABLE = "sdk_unavailable"
    UNKNOWN = "unknown"


# Failures worth retrying without the user changing anything. ``UNKNOWN`` is
# included so an unclassified failure is never remembered as a verdict.
RETRYABLE_ERROR_CODES = frozenset(
    {
        AIProviderErrorCode.RATE_LIMITED,
        AIProviderErrorCode.TIMEOUT,
        AIProviderErrorCode.PROVIDER_UNAVAILABLE,
        AIProviderErrorCode.UNKNOWN,
    },
)


class AIProviderError(Exception):
    """A provider failure reduced to a stable code.

    It deliberately carries no provider text. SDK error messages and bodies can
    echo request details (some quote part of the API key), so only the
    classification crosses this boundary.
    """

    def __init__(
        self,
        code: AIProviderErrorCode,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_ERROR_CODES


# SDK exception hierarchies are matched by class name so this module does not
# import every SDK's error module just to classify a failure.
_TIMEOUT_TYPE_NAMES = frozenset(
    {
        "APITimeoutError",
        "ConnectTimeout",
        "DeadlineExceeded",
        "DeadlineExceededError",
        "GatewayTimeout",
        "PoolTimeout",
        "ReadTimeout",
        "TimeoutException",
        "WriteTimeout",
    },
)
_CONNECTION_TYPE_NAMES = frozenset(
    {
        "APIConnectionError",
        "ConnectError",
        "NetworkError",
        "RemoteProtocolError",
        "TransportError",
    },
)
_BAD_KEY_HINTS = (
    "api key not valid",
    "api_key_invalid",
    "api key expired",
    "invalid api key",
    "invalid x-api-key",
)
_QUOTA_HINTS = (
    "insufficient_quota",
    "insufficient quota",
    "credit balance",
    "insufficient balance",
    "insufficient funds",
    "out of credits",
)
_NOT_CHAT_HINTS = (
    "not a chat model",
    "not supported in the v1/chat/completions",
)


def _status_code(exc: BaseException) -> int | None:
    """HTTP status carried by an SDK exception, whatever the SDK calls it."""
    response = getattr(exc, "response", None)
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(response, "status_code", None),
    ):
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            continue
        if 100 <= candidate <= 599:
            return int(candidate)
    return None


def _text_of(value: object) -> str:
    """Best-effort ``str(value)``: a broken ``__str__`` must not mask the failure."""
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return ""


def _error_hints(exc: BaseException) -> str:
    """Lower-cased provider text used ONLY for substring tests, never returned."""
    parts = [_text_of(exc)]
    for attribute in ("code", "type", "message"):
        value = getattr(exc, attribute, None)
        if isinstance(value, str):
            parts.append(value)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        parts.append(json.dumps(body, default=_text_of))
    return " ".join(parts)[:2000].lower()


def _retry_after(exc: BaseException) -> int | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    getter = getattr(headers, "get", None)
    raw = getter("retry-after") if callable(getter) else None
    if not isinstance(raw, (str, int, float)):
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(math.ceil(seconds), MAX_RETRY_AFTER_SECONDS)


def _code_for_status(status: int, hints: str) -> AIProviderErrorCode | None:
    if status == 401:
        return AIProviderErrorCode.INVALID_CREDENTIALS
    if status == 402:
        return AIProviderErrorCode.QUOTA_EXCEEDED
    if status == 403:
        return AIProviderErrorCode.PERMISSION_DENIED
    if status == 404:
        if any(hint in hints for hint in _NOT_CHAT_HINTS):
            return AIProviderErrorCode.MODEL_UNSUPPORTED
        return AIProviderErrorCode.MODEL_NOT_FOUND
    if status in (408, 504):
        return AIProviderErrorCode.TIMEOUT
    if status == 429:
        if any(hint in hints for hint in _QUOTA_HINTS):
            return AIProviderErrorCode.QUOTA_EXCEEDED
        return AIProviderErrorCode.RATE_LIMITED
    if status >= 500:
        return AIProviderErrorCode.PROVIDER_UNAVAILABLE
    if status == 400:
        # Gemini reports a bad key, and Anthropic an empty balance, as 400s.
        if any(hint in hints for hint in _BAD_KEY_HINTS):
            return AIProviderErrorCode.INVALID_CREDENTIALS
        if any(hint in hints for hint in _QUOTA_HINTS):
            return AIProviderErrorCode.QUOTA_EXCEEDED
        return AIProviderErrorCode.INVALID_REQUEST
    if 400 < status < 500:
        return AIProviderErrorCode.INVALID_REQUEST
    return None


def classify_provider_error(exc: BaseException) -> AIProviderError:
    """Map any SDK/transport exception to a stable, secret-free classification."""
    if isinstance(exc, AIProviderError):
        return exc
    if isinstance(exc, ImportError):
        return AIProviderError(AIProviderErrorCode.SDK_UNAVAILABLE)

    type_names = {cls.__name__ for cls in type(exc).__mro__}
    status = _status_code(exc)
    if isinstance(exc, TimeoutError) or type_names & _TIMEOUT_TYPE_NAMES:
        return AIProviderError(AIProviderErrorCode.TIMEOUT, status_code=status)

    code = _code_for_status(status, _error_hints(exc)) if status is not None else None
    if code is None:
        if type_names & _CONNECTION_TYPE_NAMES or isinstance(exc, OSError):
            code = AIProviderErrorCode.PROVIDER_UNAVAILABLE
        else:
            code = AIProviderErrorCode.UNKNOWN
    retry_after = (
        _retry_after(exc)
        if code
        in (AIProviderErrorCode.RATE_LIMITED, AIProviderErrorCode.PROVIDER_UNAVAILABLE)
        else None
    )
    return AIProviderError(code, status_code=status, retry_after=retry_after)


class AIProvider(ABC):
    """Common interface for LLM providers."""

    #: Registry key (``PROVIDERS``) for log lines; empty for ad-hoc subclasses.
    provider_id: ClassVar[str] = ""

    @abstractmethod
    def generate(self, prompt: str, *, stream: bool = False) -> str | Iterator[str]:
        """Generate a response from the given prompt.

        When *stream* is ``False`` (default), returns the full response as a
        string.  When ``True``, returns an iterator that yields chunks as they
        arrive.
        """

    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""

    def probe(self, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> None:
        """Prove the key can use the model with one minimal, bounded request.

        Returns normally only when the provider accepted the request. Every
        failure is raised as :class:`AIProviderError`; the original exception
        (which may quote request details) is intentionally not chained.
        """
        try:
            self._probe(timeout)
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - each SDK has its own hierarchy
            error = classify_provider_error(exc)
            logger.warning(
                "AI provider probe failed: provider=%s code=%s status=%s",
                self.provider_id or type(self).__name__,
                error.code.value,
                error.status_code,
            )
            raise error from None

    def _probe(self, timeout: float) -> None:
        """Default probe for adapters without a cheaper bounded request."""
        self.generate(PROBE_PROMPT)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


class GeminiProvider(AIProvider):
    """Google Gemini via the ``google-generativeai`` SDK."""

    provider_id = "gemini"
    _configuration_lock = threading.RLock()

    def __init__(self, api_key: str, model: str = "gemini-3.8-flash") -> None:
        import google.generativeai as genai  # type: ignore[import-untyped]

        self._genai = genai
        self._api_key = api_key
        with self._configuration_lock:
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(model)
        self._model_name = model

    def generate(self, prompt: str, *, stream: bool = False) -> str | Iterator[str]:
        if stream:
            return self._stream(prompt)
        # google-generativeai resolves a model's client lazily from process-global
        # configuration. Keep configuration and first use atomic so concurrent
        # tenants cannot bind a model to another user's API-key client.
        with self._configuration_lock:
            self._genai.configure(api_key=self._api_key)
            response = self._model.generate_content(prompt)
        return (response.text or "").strip()

    def _stream(self, prompt: str) -> Iterator[str]:
        # Hold the lock for the stream lifetime because the SDK uses global
        # configuration when creating its default client.
        with self._configuration_lock:
            self._genai.configure(api_key=self._api_key)
            response = self._model.generate_content(prompt, stream=True)
            for chunk in response:
                if chunk.text:
                    yield chunk.text

    def _probe(self, timeout: float) -> None:
        # Same lock/configure pairing as generation so a probe can never bind
        # to another tenant's key. ``retry=None`` stops the SDK's default
        # retry policy from stretching one probe past ``timeout``.
        with self._configuration_lock:
            self._genai.configure(api_key=self._api_key)
            self._model.generate_content(
                PROBE_PROMPT,
                generation_config={"max_output_tokens": PROBE_MAX_TOKENS},
                # The stubs type ``retry`` as a Retry; None is the documented way
                # to turn the SDK retry policy off.
                request_options={"timeout": timeout, "retry": None},  # type: ignore[arg-type]
            )

    def name(self) -> str:
        return f"Gemini ({self._model_name})"


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------


class ClaudeProvider(AIProvider):
    """Anthropic Claude via the ``anthropic`` SDK."""

    provider_id = "claude"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5-5") -> None:
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "The `anthropic` package is required for Claude. "
                "Install it with: pip install anthropic",
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def generate(self, prompt: str, *, stream: bool = False) -> str | Iterator[str]:
        if stream:
            return self._stream(prompt)
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if hasattr(block, "text"):
                return block.text.strip()
        return ""

    def _stream(self, prompt: str) -> Iterator[str]:
        with self._client.messages.stream(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            yield from stream.text_stream

    def _probe(self, timeout: float) -> None:
        self._client.with_options(timeout=timeout, max_retries=0).messages.create(
            model=self._model,
            max_tokens=PROBE_MAX_TOKENS,
            messages=[{"role": "user", "content": PROBE_PROMPT}],
        )

    def name(self) -> str:
        return f"Claude ({self._model})"


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class OpenAIProvider(AIProvider):
    """OpenAI GPT via the ``openai`` SDK."""

    provider_id = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        try:
            import openai
        except ImportError:
            raise ImportError(
                "The `openai` package is required for OpenAI. "
                "Install it with: pip install openai",
            )
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def generate(self, prompt: str, *, stream: bool = False) -> str | Iterator[str]:
        if stream:
            return self._stream(prompt)
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
        )
        return (response.choices[0].message.content or "").strip()

    def _stream(self, prompt: str) -> Iterator[str]:
        stream = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    def _probe(self, timeout: float) -> None:
        # ``max_completion_tokens`` is the only limit newer reasoning models
        # accept; ``max_tokens`` would be rejected and fail a healthy key.
        self._client.with_options(
            timeout=timeout,
            max_retries=0,
        ).chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": PROBE_PROMPT}],
            max_completion_tokens=PROBE_MAX_TOKENS,
        )

    def name(self) -> str:
        return f"OpenAI ({self._model})"


# ---------------------------------------------------------------------------
# DeepSeek
# ---------------------------------------------------------------------------


class DeepSeekProvider(AIProvider):
    """DeepSeek via its OpenAI-compatible API.

    DeepSeek exposes the same request/response shape as OpenAI's chat
    completions API, so this reuses the ``openai`` SDK client pointed at
    DeepSeek's own base URL rather than needing a bespoke HTTP client.
    """

    provider_id = "deepseek"
    BASE_URL = "https://api.deepseek.com"

    def __init__(self, api_key: str, model: str = "deepseek-flash") -> None:
        try:
            import openai
        except ImportError:
            raise ImportError(
                "The `openai` package is required for DeepSeek (it uses an "
                "OpenAI-compatible API). Install it with: pip install openai",
            )
        self._client = openai.OpenAI(api_key=api_key, base_url=self.BASE_URL)
        self._model = model

    def generate(self, prompt: str, *, stream: bool = False) -> str | Iterator[str]:
        if stream:
            return self._stream(prompt)
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
        )
        return (response.choices[0].message.content or "").strip()

    def _stream(self, prompt: str) -> Iterator[str]:
        stream = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    def _probe(self, timeout: float) -> None:
        # DeepSeek's OpenAI-compatible API documents ``max_tokens``.
        self._client.with_options(
            timeout=timeout,
            max_retries=0,
        ).chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": PROBE_PROMPT}],
            max_tokens=PROBE_MAX_TOKENS,
        )

    def name(self) -> str:
        return f"DeepSeek ({self._model})"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Recognised provider names mapped to their default model.
#
# Defaults must never be a deprecated or retired model: ai_model_policy rejects
# those, and tests/test_ai_model_policy.py asserts every default passes it.
PROVIDERS: dict[str, dict[str, Any]] = {
    "gemini": {"class": GeminiProvider, "default_model": "gemini-3.8-flash"},
    "claude": {"class": ClaudeProvider, "default_model": "claude-sonnet-5-5"},
    "openai": {"class": OpenAIProvider, "default_model": "gpt-4o"},
    "deepseek": {"class": DeepSeekProvider, "default_model": "deepseek-flash"},
}


def get_provider(
    provider_name: str,
    api_key: str,
    model: str | None = None,
) -> AIProvider:
    """Instantiate the right AI provider from a name and key.

    Raises ``ValueError`` for unknown providers and ``ImportError`` when the
    provider's SDK is not installed.
    """
    key = provider_name.lower().strip()
    spec = PROVIDERS.get(key)
    if spec is None:
        raise ValueError(
            f"Unknown AI provider '{provider_name}'. "
            f"Choose from: {', '.join(PROVIDERS)}",
        )
    cls = spec["class"]
    effective_model = model or spec["default_model"]
    return cls(api_key=api_key, model=effective_model)


_DISPLAY_NAMES = {
    "gemini": "Google Gemini",
    "claude": "Anthropic Claude",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
}


def resolve_provider(
    user_id: str | None = None,
    *,
    server_gemini_key: str | None = None,
    server_gemini_model: str | None = None,
    fallback_api_key: str | None = None,
    fallback_model: str | None = None,
    db_path: str = "workout_agent.db",
) -> AIProvider:
    """Compatibility wrapper for the canonical :mod:`ai_resolver` boundary."""
    from ai_resolver import resolve_provider as resolve

    return resolve(
        user_id=user_id,
        server_gemini_key=server_gemini_key,
        server_gemini_model=server_gemini_model,
        fallback_api_key=fallback_api_key,
        fallback_model=fallback_model,
        db_path=db_path,
    )


def provider_display_name(provider_id: str) -> str:
    """Human-readable name for a registry key (falls back to the key itself)."""
    key = provider_id.lower().strip()
    return _DISPLAY_NAMES.get(key, key.title())


def available_providers() -> list[dict[str, str]]:
    """Return metadata for each registered provider (for the settings UI)."""
    return [
        {
            "id": key,
            "name": _DISPLAY_NAMES.get(key, key.title()),
            "default_model": spec["default_model"],
        }
        for key, spec in PROVIDERS.items()
    ]
