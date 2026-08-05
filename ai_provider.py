"""AI provider abstraction layer.

Supports Gemini, Claude (Anthropic), OpenAI, and DeepSeek behind a common
interface so users can bring their own API key for whichever service they
prefer. See the ai-provider-wiring skill (.agents/skills/) for how call
sites should resolve a user's chosen provider before calling ``generate()``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


class AIProvider(ABC):
    """Common interface for LLM providers."""

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


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


class GeminiProvider(AIProvider):
    """Google Gemini via the ``google-generativeai`` SDK."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model)
        self._model_name = model

    def generate(self, prompt: str, *, stream: bool = False) -> str | Iterator[str]:
        if stream:
            return self._stream(prompt)
        response = self._model.generate_content(prompt)
        return (response.text or "").strip()

    def _stream(self, prompt: str) -> Iterator[str]:
        response = self._model.generate_content(prompt, stream=True)
        for chunk in response:
            if chunk.text:
                yield chunk.text

    def name(self) -> str:
        return f"Gemini ({self._model_name})"


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------


class ClaudeProvider(AIProvider):
    """Anthropic Claude via the ``anthropic`` SDK."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514") -> None:
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "The `anthropic` package is required for Claude. "
                "Install it with: pip install anthropic"
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

    def name(self) -> str:
        return f"Claude ({self._model})"


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class OpenAIProvider(AIProvider):
    """OpenAI GPT via the ``openai`` SDK."""

    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        try:
            import openai
        except ImportError:
            raise ImportError(
                "The `openai` package is required for OpenAI. "
                "Install it with: pip install openai"
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

    BASE_URL = "https://api.deepseek.com"

    def __init__(self, api_key: str, model: str = "deepseek-chat") -> None:
        try:
            import openai
        except ImportError:
            raise ImportError(
                "The `openai` package is required for DeepSeek (it uses an "
                "OpenAI-compatible API). Install it with: pip install openai"
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

    def name(self) -> str:
        return f"DeepSeek ({self._model})"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Recognised provider names mapped to their default model.
PROVIDERS: dict[str, dict[str, Any]] = {
    "gemini": {"class": GeminiProvider, "default_model": "gemini-2.5-flash"},
    "claude": {"class": ClaudeProvider, "default_model": "claude-sonnet-4-20250514"},
    "openai": {"class": OpenAIProvider, "default_model": "gpt-4o"},
    "deepseek": {"class": DeepSeekProvider, "default_model": "deepseek-chat"},
}


def get_provider(
    provider_name: str, api_key: str, model: str | None = None
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
            f"Choose from: {', '.join(PROVIDERS)}"
        )
    cls = spec["class"]
    effective_model = model or spec["default_model"]
    return cls(api_key=api_key, model=effective_model)


_DISPLAY_NAMES = {
    "gemini": "Gemini",
    "claude": "Claude",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
}


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
