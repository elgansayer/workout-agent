"""Static model policy: reject model IDs a provider will not usefully serve.

The live connection check (``ai_connection``) is the authority on whether a
key can reach a model. This module rejects what is already known to be wrong
before any provider call is spent, with an actionable reason:

* ``model_invalid``      - not a plausible model identifier;
* ``model_unsupported``  - a non-text family (embeddings, speech, images, ...);
* ``model_deprecated``   - the provider announced a shutdown date that has not
  arrived yet, so activating the model would break coaching soon;
* ``model_retired``      - the shutdown date has passed.

Anything the policy does not recognise is allowed through to the live check
rather than guessed at, so new models work as soon as a provider serves them.

The lifecycle table is reference data, not logic. It was last reviewed on
``POLICY_REVIEWED_ON`` against the providers' own pages:

* Anthropic  https://platform.claude.com/docs/en/about-claude/model-deprecations
* OpenAI     https://developers.openai.com/api/docs/deprecations
* Google     https://ai.google.dev/gemini-api/docs/deprecations
* DeepSeek   https://api-docs.deepseek.com/updates

``docs/AI_PROVIDER_CONNECTION_TESTS.md`` describes how to refresh it. Entries
are matched case-insensitively against the whole identifier and only ever make
the policy stricter for IDs the provider itself has announced as going away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ai_provider import PROVIDERS, AIProviderErrorCode

POLICY_REVIEWED_ON = date(2026, 11, 11)
MAX_MODEL_ID_LENGTH = 200

# Letters, digits and the punctuation real identifiers use (``gpt-4o``,
# ``models/gemini-3.8-flash``, ``ft:gpt-4o-mini:org::id``). Path segments of
# ``.`` / ``..`` or empty segments are rejected so an identifier can never
# rewrite the URL an SDK builds from it.
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")


@dataclass(frozen=True, slots=True)
class ModelDecision:
    """Why a model ID was rejected, with a replacement when one is known."""

    code: AIProviderErrorCode
    replacement: str | None = None
    retires_on: date | None = None


@dataclass(frozen=True, slots=True)
class _Lifecycle:
    pattern: re.Pattern[str]
    retires_on: date
    deprecated_on: date = date.min


def _patterns(*sources: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(source) for source in sources)


def _life(
    source: str,
    retires_on: date,
    *,
    deprecated_on: date = date.min,
) -> _Lifecycle:
    return _Lifecycle(re.compile(source), retires_on, deprecated_on)


# Families that exist at the provider but cannot produce coaching text through
# the chat interface this app uses.
_UNSUPPORTED: dict[str, tuple[re.Pattern[str], ...]] = {
    "openai": _patterns(
        r"embedding",
        r"^(?:whisper|tts|dall-e|sora|omni-moderation|text-moderation|gpt-image|chatgpt-image)",
        r"-(?:realtime|audio|transcribe|tts|diarize)(?:-|$)",
    ),
    "gemini": _patterns(
        r"embedding",
        r"^(?:imagen|veo|aqa)(?:-|$)",
        r"^gemini-live-",
        r"-(?:tts|live|image)(?:-|$)",
        r"native-audio",
        r"nano-banana",
        r"robotics",
    ),
    "claude": (),
    "deepseek": (),
}

# ``retires_on`` is the first day requests to the model fail. Before it the
# model is "deprecated" (still served, but scheduled to go); from it, "retired".
_LIFECYCLE: dict[str, tuple[_Lifecycle, ...]] = {
    "claude": (
        _life(r"^claude-(?:instant|1)(?:[.-]|$)", date(2024, 11, 6)),
        _life(r"^claude-2(?:[.-]|$)", date(2025, 7, 21)),
        _life(r"^claude-3-sonnet", date(2025, 7, 21)),
        _life(r"^claude-3-5-sonnet", date(2025, 10, 28)),
        _life(r"^claude-3-opus", date(2026, 1, 5)),
        _life(r"^claude-3-(?:7-sonnet|5-haiku)", date(2026, 2, 19)),
        _life(r"^claude-3-haiku", date(2026, 4, 20)),
        _life(r"^claude-(?:sonnet|opus)-4-(?:0|20250514)$", date(2026, 6, 15)),
        _life(r"^claude-opus-4-1(?:-20250805)?$", date(2026, 8, 5)),
        _life(
            r"^claude-sonnet-4-5(?:-20250929)?$",
            date(2026, 11, 30),
            deprecated_on=date(2026, 9, 30),
        ),
    ),
    "openai": (
        _life(
            r"^(?:text|code)-(?:ada|babbage|curie|davinci|cushman)-(?:edit-)?\d+$",
            date(2024, 1, 4),
        ),
        _life(r"^(?:ada|babbage|curie|davinci)$", date(2024, 1, 4)),
        _life(r"^(?:babbage|davinci)-002$", date(2026, 9, 28)),
        _life(r"^gpt-3\.5-turbo", date(2026, 10, 23)),
        # The original GPT-4 line only: ``gpt-4o``, ``gpt-4.1`` etc. are not
        # matched because the character after ``gpt-4`` must be ``-`` or end.
        _life(r"^gpt-4(?:-|$)", date(2026, 10, 23)),
        _life(r"^gpt-4\.5-preview", date(2025, 7, 14)),
        _life(r"^chatgpt-4o-latest$", date(2026, 2, 17)),
        _life(r"^gpt-4o-2024-05-13$", date(2026, 10, 23)),
        _life(r"^gpt-4\.1-nano(?:-2025-04-14)?$", date(2026, 10, 23)),
        _life(r"^o1(?:-|$)", date(2026, 10, 23)),
        _life(r"^o3-mini(?:-|$)", date(2026, 10, 23)),
        _life(r"^o4-mini(?:-|$)", date(2026, 10, 23)),
        _life(
            r"^(?:gpt-5(?:-(?:mini|nano|pro))?|o3(?:-pro)?)(?:-\d{4}-\d{2}-\d{2})?$",
            date(2026, 12, 11),
            deprecated_on=date(2026, 6, 11),
        ),
        _life(
            r"^(?:gpt-4o-2024-(?:08-06|11-20)|gpt-4o-mini-2024-07-18"
            r"|gpt-4\.1(?:-mini)?-2025-04-14)$",
            date(2027, 1, 14),
            deprecated_on=date(2026, 10, 14),
        ),
    ),
    "gemini": (
        _life(r"^gemini-2\.5-pro-preview-", date(2025, 12, 2)),
        _life(r"^gemini-2\.5-flash-preview-05-20$", date(2025, 11, 18)),
        _life(r"^gemini-2\.5-flash-preview-09-2025$", date(2026, 2, 17)),
        _life(r"^gemini-2\.0-flash(?:-001|-lite(?:-001)?)?$", date(2026, 6, 1)),
        _life(r"^gemini-3-pro-preview$", date(2026, 3, 9)),
        _life(
            r"^gemini-2\.5-(?:pro|flash)$",
            date(2026, 12, 1),
            deprecated_on=date(2025, 6, 17),
        ),
    ),
    "deepseek": (
        _life(
            r"^deepseek-(?:chat|reasoner)$",
            date(2026, 7, 24),
            deprecated_on=date(2026, 4, 24),
        ),
        _life(
            r"^deepseek-v4-flash(?:-vision-exp)?$",
            date(2026, 9, 10),
            deprecated_on=date(2026, 9, 10),
        ),
    ),
}


def is_plausible_model_id(model: str) -> bool:
    """True when *model* is shaped like an identifier an SDK can safely send."""
    if not 0 < len(model) <= MAX_MODEL_ID_LENGTH:
        return False
    if not _MODEL_ID.fullmatch(model):
        return False
    return all(segment not in ("", ".", "..") for segment in model.split("/"))


def evaluate_model(
    provider: str,
    model: str,
    *,
    today: date | None = None,
) -> ModelDecision | None:
    """Return why *model* must not be activated for *provider*, or ``None``.

    ``None`` means "no objection": the live connection check decides whether the
    key can actually use it.
    """
    provider_id = provider.strip().lower()
    candidate = model.strip()
    if not is_plausible_model_id(candidate):
        return ModelDecision(AIProviderErrorCode.MODEL_INVALID)

    normalized = candidate.lower()
    if provider_id == "gemini":
        normalized = normalized.removeprefix("models/")
    # User-named assets (Gemini tuned models, OpenAI fine-tunes) are opaque:
    # their names say nothing about the base model's family or lifecycle.
    if normalized.startswith(("tunedmodels/", "ft:")):
        return None

    replacement = PROVIDERS.get(provider_id, {}).get("default_model")
    if any(p.search(normalized) for p in _UNSUPPORTED.get(provider_id, ())):
        return ModelDecision(AIProviderErrorCode.MODEL_UNSUPPORTED, replacement)

    today = today or datetime.now(timezone.utc).date()
    for rule in _LIFECYCLE.get(provider_id, ()):
        if not rule.pattern.search(normalized):
            continue
        if today >= rule.retires_on:
            return ModelDecision(
                AIProviderErrorCode.MODEL_RETIRED,
                replacement=replacement,
            )
        if today >= rule.deprecated_on:
            return ModelDecision(
                AIProviderErrorCode.MODEL_DEPRECATED,
                replacement=replacement,
                retires_on=rule.retires_on,
            )
        return None
    return None
