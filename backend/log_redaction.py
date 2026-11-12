"""Central redaction for log records, diagnostics and error responses.

Connector payloads, request headers, prompts, forms and exception messages all
reach the log stream, and a provider SDK or HTTP library will happily put a bearer
token, an API key in a URL or an OAuth authorisation code into an exception
message. Rather than trusting each call site, :func:`install_log_redaction`
hardens every ``LogRecord`` the process creates, whichever logger, library or
handler produced it:

* each positional argument is redacted (structures by field classification,
  strings by pattern) while keeping its type and arity, because some formatters
  (uvicorn's access formatter) read ``record.args`` directly;
* the fully formatted message is redacted again when it is rendered, so a
  ``key=value`` pair split across the template and its arguments is still caught;
* the traceback and stack text are rendered once, redacted and cached on the
  record, so no standard formatter can re-render the raw exception.

Classification comes from :mod:`data_classification`, the single source of truth
for what is sensitive; this module adds the pattern layer for values that arrive
as free text. Opaque internal identifiers (account ids, request ids, event
hashes) are deliberately *not* redacted: operators correlate on them and the
repository already treats them as non-secret.

Redaction is best effort for *unknown* free text: it cannot recognise a prompt or
a health reading that carries no key name or recognisable shape. Call sites must
still avoid logging payloads. It never raises and fails closed.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import traceback
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from functools import lru_cache
from itertools import islice
from typing import Any
from urllib.parse import quote, quote_plus

from data_classification import DataClass, classify_field

CREDENTIAL_MARKER = "[REDACTED:CREDENTIAL]"
QUERY_MARKER = "[REDACTED:QUERY]"
EMAIL_MARKER = "[REDACTED:EMAIL]"
WITHHELD_MESSAGE = "[log message withheld: redaction failed]"

_MARKER_PREFIX = "[REDACTED"
_MAX_TEXT_LENGTH = 200_000
_MAX_DEPTH = 8
_MAX_ITEMS = 200
_MAX_REGISTERED_SECRETS = 256
_MIN_SECRET_LENGTH = 8

# ---------------------------------------------------------------------------
# Field classification for log keys
# ---------------------------------------------------------------------------

# ``data_classification`` matches fragments of a field name. These tokens add
# names that only show up in connector, OAuth and LLM payloads. They are matched
# on whole ``_``-separated tokens so ``submit`` never matches ``bmi``.
_EXTRA_TOKEN_CLASSES: dict[DataClass, frozenset[str]] = {
    DataClass.CREDENTIAL: frozenset(
        {
            "verifier",
            "assertion",
            "sid",
            "csrf",
            "xsrf",
            "otp",
            "passcode",
            "passphrase",
            "passwd",
            "nonce",
            "signature",
            "credential",
            "credentials",
            "apikey",
            "bearer",
            "jwt",
        }
    ),
    DataClass.HEALTH: frozenset(
        {
            "weight",
            "bodyweight",
            "bmi",
            "steps",
            "calories",
            "spo2",
            "vo2",
            "vo2max",
            "glucose",
            "biometric",
            "biometrics",
            "readiness",
            "bpm",
        }
    ),
    DataClass.PROMPT: frozenset(
        {
            "messages",
            "contents",
            "parts",
            "completion",
            "conversation",
            "transcript",
            "instruction",
            "instructions",
        }
    ),
}
_REDACTED_CLASSES = frozenset(
    {
        DataClass.IDENTIFIER,
        DataClass.HEALTH,
        DataClass.WORKOUT,
        DataClass.PROMPT,
        DataClass.ANALYTICS,
        DataClass.CREDENTIAL,
    }
)
# Classes whose values are also scrubbed from ``key=value`` pairs found in free
# text. Identifier-class fields are excluded except for the few listed below: the
# product logs opaque account ids (``account=<id>``) for correlation.
_TEXT_PAIR_CLASSES = frozenset(
    {DataClass.CREDENTIAL, DataClass.HEALTH, DataClass.WORKOUT, DataClass.PROMPT}
)
# Identifier fields that are personal data or capability URLs rather than opaque
# correlation ids, so they are scrubbed from free text too.
_TEXT_IDENTIFIER_KEYS = frozenset(
    {
        "email",
        "display_name",
        "ip_address",
        "endpoint",
        "device_id",
        "external_user_id",
        "subject",
    }
)
# ``code`` and ``state`` are OAuth authorisation material in a callback but plain
# status vocabulary elsewhere (``code=404``, ``state=syncing``). They are
# redacted unless the value is clearly a short status word or number.
_AMBIGUOUS_KEYS = frozenset({"code", "state"})
_STATUS_VALUE = re.compile(r"[A-Za-z][A-Za-z_.-]{0,39}|\d{1,4}")


def _normalise_key(key: str) -> str:
    return key.strip().lower().replace("-", "_").replace(".", "_")


@lru_cache(maxsize=4096)
def _label_for_key(key: str) -> DataClass | None:
    """Return the redaction class for a field name, or ``None`` when it is safe."""

    normalised = _normalise_key(key)
    classification = classify_field(normalised)
    if classification in _REDACTED_CLASSES:
        return classification
    tokens = set(normalised.split("_"))
    for data_class, vocabulary in _EXTRA_TOKEN_CLASSES.items():
        if tokens & vocabulary:
            return data_class
    return None


def _marker_for(data_class: DataClass) -> str:
    return f"[REDACTED:{data_class.value.upper()}]"


def _ambiguous_value_is_safe(value: object) -> bool:
    """Whether an ``code`` / ``state`` value is plainly a status, not a secret."""

    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return 0 <= value <= 9999
    if isinstance(value, str):
        return _STATUS_VALUE.fullmatch(value.strip()) is not None
    # A container is walked entry by entry, so its leaves are judged on their own.
    return isinstance(value, (Mapping, Sequence, AbstractSet)) and not isinstance(
        value, (bytes, bytearray)
    )


# ---------------------------------------------------------------------------
# Known-secret registry
# ---------------------------------------------------------------------------

_secrets_lock = threading.Lock()
_known_secrets: OrderedDict[str, None] = OrderedDict()
_longest_first: tuple[str, ...] | None = None
_SECRET_ENV_NAME = re.compile(
    r"(?:^|_)(?:SECRET|TOKEN|PASSWORD|PASSPHRASE|PRIVATE_KEY|API_KEY|ENCRYPTION_KEY|HMAC_KEY)$"
)


def register_secret(value: object) -> None:
    """Remember a secret so its exact value is removed from every later log line.

    Pattern matching cannot recognise a credential with no distinctive shape (a
    provider's UUID-style API key, an opaque OAuth token). The vault registers each
    value it decrypts or accepts, and :func:`install_log_redaction` registers the
    secret-bearing environment variables, so those exact values are masked even
    when they appear without a key name. The registry is bounded: the oldest
    entries are evicted first. Values shorter than eight characters are ignored
    because masking them would corrupt ordinary text.
    """

    global _longest_first
    if not isinstance(value, str) or len(value) < _MIN_SECRET_LENGTH:
        return
    variants = {value, quote(value, safe=""), quote_plus(value)}
    with _secrets_lock:
        changed = False
        for variant in variants:
            if len(variant) < _MIN_SECRET_LENGTH:
                continue
            changed = changed or variant not in _known_secrets
            _known_secrets[variant] = None
            _known_secrets.move_to_end(variant)
        while len(_known_secrets) > _MAX_REGISTERED_SECRETS:
            _known_secrets.popitem(last=False)
            changed = True
        if changed:
            # Re-registering a known value (the vault does so on every use) must
            # not throw away the cached longest-first ordering.
            _longest_first = None


def register_environment_secrets(environ: Mapping[str, str] | None = None) -> None:
    """Register every secret-bearing variable of *environ* (default ``os.environ``)."""

    source = os.environ if environ is None else environ
    for name, value in source.items():
        if _SECRET_ENV_NAME.search(name.upper()):
            register_secret(value)


def forget_registered_secrets() -> None:
    """Empty the registry. Intended for tests that need an isolated state."""

    global _longest_first
    with _secrets_lock:
        _known_secrets.clear()
        _longest_first = None


def _registered_secrets() -> tuple[str, ...]:
    """Registered secrets, longest first so a secret never survives as a suffix."""

    global _longest_first
    with _secrets_lock:
        if _longest_first is None:
            _longest_first = tuple(sorted(_known_secrets, key=len, reverse=True))
        return _longest_first


# ---------------------------------------------------------------------------
# Text patterns
# ---------------------------------------------------------------------------

_HEADER_NAMES = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "x-access-token",
    "x-goog-api-key",
    "x-csrf-token",
    "x-xsrf-token",
    "x-amz-security-token",
)
_HEADER_LINE = re.compile(
    r"(?i)(?<![\w-])(?P<name>"
    + "|".join(re.escape(name) for name in _HEADER_NAMES)
    + r")(?P<sep>[ \t]*:[ \t]*)(?P<value>[^\r\n]*)"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_URL_USERINFO = re.compile(
    r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.-]{1,15}://)(?P<userinfo>[^/\s@?#]{1,200})@"
)
_BEARER = re.compile(r"(?i)\b(?P<scheme>bearer)(?P<space>\s+)[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*")
_KEY_SHAPES = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}"  # OpenAI, Anthropic, DeepSeek
    r"|\bAIza[0-9A-Za-z_-]{30,}"  # Google API key
    r"|\bya29\.[0-9A-Za-z_-]{20,}"  # Google OAuth access token
    r"|\bGOCSPX-[0-9A-Za-z_-]{16,}"  # Google OAuth client secret
    r"|(?<![\w/])1//[0-9A-Za-z_-]{20,}"  # Google OAuth refresh token
    r"|(?<![\w/])4/[0-9A-Za-z_-]{30,}"  # Google OAuth authorisation code
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"  # GitHub tokens
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"  # Slack tokens
    r"|\bAKIA[0-9A-Z]{16}\b"  # AWS access key id
)
# A literal that every ``_KEY_SHAPES`` alternative contains; checking for it first
# keeps the common case (a line with no key in it) off the regex engine.
_KEY_HINTS = (
    "sk-",
    "AIza",
    "ya29.",
    "GOCSPX-",
    "1//",
    "4/",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "xox",
    "AKIA",
)
_TELEGRAM_TOKEN = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")  # Telegram bot token
_QUERY_PARAMETER = re.compile(
    r"(?P<lead>[?&#])(?P<name>[A-Za-z0-9_.\-\[\]%]{1,64})=(?P<value>[^\s&#\"'<>]+)"
)
_EMAIL = re.compile(
    r"(?i)\b[A-Z0-9._%+-]{1,64}@[A-Z0-9-]{1,63}(?:\.[A-Z0-9-]{1,63}){1,8}\b"
)
_SEPARATOR = re.compile(r"[:=]")
_KEY_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)
_MAX_KEY_LENGTH = 64
_BARE_VALUE = re.compile(r"[^\s,;&)\]}\"'<>]+")
_AUTH_SCHEMES = frozenset({"bearer", "basic", "digest", "negotiate", "token", "hmac"})
_MAX_BRACKET_DEPTH = 32


def _scan_quoted(text: str, start: int, quote_char: str) -> int:
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            index += 2
            continue
        if char == quote_char:
            return index + 1
        index += 1
    return length


def _scan_escaped_quoted(text: str, start: int, quote_char: str) -> int:
    """Scan ``\\"value\\"`` (JSON that was itself placed inside a string)."""

    index = start
    length = len(text)
    while index < length:
        if text[index] == "\\" and text[index + 1 : index + 2] == quote_char:
            return index + 2
        index += 1
    return length


def _scan_bracketed(text: str, start: int) -> int:
    closing = {"[": "]", "{": "}", "(": ")"}
    stack: list[str] = []
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if char in "\"'":
            index = _scan_quoted(text, index + 1, char)
            continue
        if char in closing:
            stack.append(closing[char])
            if len(stack) > _MAX_BRACKET_DEPTH:
                return length
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return index + 1
        index += 1
    return length


def _value_end(text: str, start: int, data_class: DataClass) -> int:
    """Return the index just past the value starting at *start*."""

    length = len(text)
    if start >= length:
        return start
    if text.startswith(_MARKER_PREFIX, start):
        close = text.find("]", start)
        return length if close == -1 else close + 1
    char = text[start]
    if char == "\\" and text[start + 1 : start + 2] in {'"', "'"}:
        return _scan_escaped_quoted(text, start + 2, text[start + 1])
    if char in "\"'":
        return _scan_quoted(text, start + 1, char)
    if char in "[{(":
        return _scan_bracketed(text, start)
    if data_class is DataClass.PROMPT:
        # An unquoted prompt is free text: it ends with the line, not the word.
        newline = text.find("\n", start)
        return length if newline == -1 else newline
    match = _BARE_VALUE.match(text, start)
    if match is None:
        return start
    end = match.end()
    if match.group(0).lower() in _AUTH_SCHEMES:
        # ``Authorization=Bearer <token>``: the scheme word is not the secret.
        following = _BARE_VALUE.match(text, _skip_spaces(text, end))
        if following is not None:
            end = following.end()
    return end


def _skip_spaces(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _wrap_like(value: str, marker: str) -> str:
    """Put *marker* inside the same quotes as *value* so JSON-ish text stays readable."""

    for quote_text in ('\\"', "\\'", '"', "'"):
        if value.startswith(quote_text) and value.endswith(quote_text) and len(value) >= 2 * len(quote_text):
            return f"{quote_text}{marker}{quote_text}"
    return marker


def _key_before(text: str, separator: int, floor: int) -> tuple[str, int] | None:
    """Return the field name written just before the separator at *separator*.

    Scans backwards over optional blanks and a closing quote (plain or escaped)
    and then over name characters, never into text before *floor*.
    """

    end = separator
    while end > floor and text[end - 1] in " \t":
        end -= 1
    if end > floor and text[end - 1] in "\"'":
        end -= 1
        if end > floor and text[end - 1] == "\\":
            end -= 1
    start = end
    while (
        start > floor
        and end - start < _MAX_KEY_LENGTH
        and text[start - 1] in _KEY_CHARACTERS
    ):
        start -= 1
    while start < end and not (text[start].isalpha() or text[start] == "_"):
        start += 1
    if start >= end:
        return None
    return text[start:end], start


def _redact_pairs(text: str) -> str:
    """Redact the value of every ``key=value`` / ``"key": value`` pair that is sensitive."""

    pieces: list[str] = []
    cursor = 0
    position = 0
    while True:
        match = _SEPARATOR.search(text, position)
        if match is None:
            break
        position = match.end()
        found = _key_before(text, match.start(), cursor)
        if found is None:
            continue
        key, key_start = found
        value_start = _skip_spaces(text, match.end())
        quoted = key_start > 0 and text[key_start - 1] in "\"'"
        prose = text[match.start()] == ":" and not quoted
        normalised = _normalise_key(key)
        ambiguous = normalised in _AMBIGUOUS_KEYS
        data_class = _label_for_key(key)
        if ambiguous:
            data_class = DataClass.CREDENTIAL
        elif data_class is DataClass.IDENTIFIER:
            if normalised not in _TEXT_IDENTIFIER_KEYS:
                data_class = None
        elif data_class not in _TEXT_PAIR_CLASSES:
            data_class = None
        if data_class is None:
            continue
        # Unquoted ``key: value`` is ordinary prose ("Plan: ready"); only quoted
        # keys (JSON / repr) and ``key=value`` pairs are treated as data. A
        # credential keeps its stricter handling because ``token: abc`` is
        # exactly how headers and error messages spell one.
        if prose and (ambiguous or data_class is not DataClass.CREDENTIAL):
            continue
        end = _value_end(text, value_start, data_class)
        if end <= value_start:
            continue
        value = text[value_start:end]
        inner = value.strip("\"'\\ ")
        if inner.startswith(_MARKER_PREFIX):
            continue
        if ambiguous and _ambiguous_value_is_safe(inner):
            continue
        pieces.append(text[cursor:value_start])
        marker = QUERY_MARKER if ambiguous else _marker_for(data_class)
        pieces.append(_wrap_like(value, marker))
        cursor = end
        position = end
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)


def _redact_query(match: re.Match[str]) -> str:
    # Always re-mask, even when the value is already a marker: an argument is
    # redacted on its own before it reaches the template, so a registered secret
    # arrives here as ``CREDENTIAL`` and must still become the uniform ``QUERY``
    # marker. Replacing a marker with a marker keeps the pipeline idempotent.
    return f"{match.group('lead')}{match.group('name')}={QUERY_MARKER}"


def redact_text(text: object, *, known_secrets: bool = True) -> str:
    """Return *text* with credentials, query values and addresses removed.

    Idempotent and linear in the length of the input. Covers: registered and
    environment secrets, PEM private keys, ``Authorization`` / ``Cookie`` style
    header lines, URL user-info, ``Bearer`` tokens, JWTs, well-known provider key
    shapes (OpenAI/Anthropic/DeepSeek, Google, GitHub, Slack, AWS, Telegram),
    the *value* of every URL query or form parameter (OAuth ``code`` and
    ``state`` included, names are kept), e-mail addresses and the value of any
    ``key=value`` or ``"key": value`` pair whose key is a credential, health,
    workout or prompt field.

    ``known_secrets=False`` skips the exact-value registry. Text that is returned
    to a client must use it: the registry is shared by every tenant, so masking a
    registered value in a response would tell a caller who echoes a guess back
    whether that guess is somebody's live credential. Logs are never shown to
    tenants, so they keep the registry.
    """

    value = text if isinstance(text, str) else str(text)
    if len(value) > _MAX_TEXT_LENGTH:
        omitted = len(value) - _MAX_TEXT_LENGTH
        value = f"{value[:_MAX_TEXT_LENGTH]}...[truncated {omitted} characters]"
    # Query values are masked before the exact-secret registry so a registered
    # token or OAuth code that appears in a URL keeps the uniform ``QUERY``
    # marker. Otherwise the access line's shape would depend on whether the
    # value happened to be registered in this process.
    value = _QUERY_PARAMETER.sub(_redact_query, value)
    if known_secrets:
        for secret in _registered_secrets():
            value = value.replace(secret, CREDENTIAL_MARKER)
    if "PRIVATE KEY" in value:
        value = _PRIVATE_KEY_BLOCK.sub("[REDACTED:PRIVATE_KEY]", value)
    if ":" in value:
        value = _HEADER_LINE.sub(
            lambda m: f"{m.group('name')}{m.group('sep')}{CREDENTIAL_MARKER}", value
        )
    if "://" in value:
        value = _URL_USERINFO.sub(
            lambda m: f"{m.group('scheme')}{CREDENTIAL_MARKER}@", value
        )
    if "bearer" in value.lower():
        value = _BEARER.sub(
            lambda m: f"{m.group('scheme')}{m.group('space')}{CREDENTIAL_MARKER}", value
        )
    if "eyJ" in value:
        value = _JWT.sub(CREDENTIAL_MARKER, value)
    if any(hint in value for hint in _KEY_HINTS):
        value = _KEY_SHAPES.sub(CREDENTIAL_MARKER, value)
    if ":" in value:
        value = _TELEGRAM_TOKEN.sub(CREDENTIAL_MARKER, value)
    if "@" in value:
        value = _EMAIL.sub(EMAIL_MARKER, value)
    return _redact_pairs(value)


# ---------------------------------------------------------------------------
# Structured values
# ---------------------------------------------------------------------------


def redact_value(value: Any, *, field_name: str | None = None) -> Any:
    """Return a redacted copy of an arbitrary logged value.

    Mapping keys drive classification (see :mod:`data_classification`), so a
    credential, identifier, health, workout or prompt field is replaced by a
    class marker whatever it contains; a sequence under a sensitive key is
    replaced as a whole. String leaves go through :func:`redact_text`. Scalars
    keep their type, and unknown objects are returned unchanged because their
    rendered text is redacted when the log message is formatted.
    """

    return _redact(value, field_name, 0)


def _redact(value: Any, field_name: str | None, depth: int) -> Any:
    if field_name is not None:
        if _normalise_key(field_name) in _AMBIGUOUS_KEYS:
            if not _ambiguous_value_is_safe(value):
                return CREDENTIAL_MARKER
        else:
            data_class = _label_for_key(field_name)
            if data_class is not None:
                return _marker_for(data_class)
    if depth >= _MAX_DEPTH:
        return "[REDACTED:DEPTH]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[REDACTED:BYTES:{len(value)}]"
    if isinstance(value, Mapping):
        # ``islice`` bounds the work: a huge mapping or ``range`` is never walked
        # (or copied) past the cap.
        entries = list(islice(value.items(), _MAX_ITEMS + 1))
        redacted = {
            str(key): _redact(item, str(key), depth + 1)
            for key, item in entries[:_MAX_ITEMS]
        }
        if len(entries) > _MAX_ITEMS:
            redacted["..."] = "[more entries omitted]"
        return redacted
    if isinstance(value, (Sequence, AbstractSet)) and not isinstance(value, (str, bytes)):
        members = list(islice(value, _MAX_ITEMS + 1))
        shown = [_redact(item, None, depth + 1) for item in members[:_MAX_ITEMS]]
        if len(members) > _MAX_ITEMS:
            shown.append("[more items omitted]")
        if isinstance(value, tuple):
            return tuple(shown)
        return shown
    return value


# ---------------------------------------------------------------------------
# Log records
# ---------------------------------------------------------------------------


def _redact_argument(value: Any, field_name: str | None = None) -> Any:
    """Redact one ``LogRecord`` argument, keeping scalars (and ``%d``) intact."""

    if field_name is None and (isinstance(value, (bool, int, float)) or value is None):
        return value
    return _redact(value, field_name, 0)


_FRAME_LINE = re.compile(r'^\s+File ".*", line \d+')


def redact_traceback(text: str) -> str:
    """Redact a rendered traceback, leaving the source lines of each frame intact.

    The exception messages are where values leak. The indented source snippets
    are code: redacting them would turn ``api_key = value`` into noise and make a
    trace useless to the operator it is kept for.
    """

    rendered: list[str] = []
    pending: list[str] = []
    in_frame = False

    def flush() -> None:
        if pending:
            rendered.append(redact_text("\n".join(pending)))
            pending.clear()

    for line in text.split("\n"):
        if _FRAME_LINE.match(line):
            flush()
            rendered.append(redact_text(line))
            in_frame = True
        elif in_frame and line.startswith("    "):
            rendered.append(line)
        else:
            in_frame = False
            pending.append(line)
    flush()
    return "\n".join(rendered)


def _format_exception(exc_info: Any) -> str:
    try:
        return "".join(traceback.format_exception(*exc_info)).rstrip("\n")
    except Exception:  # noqa: BLE001 - a broken __str__ must not break logging
        return "[exception text unavailable]"


class _RedactedMessageMixin:
    """Redacts the rendered message of a record whatever formatter consumes it."""

    def getMessage(self) -> str:
        message = super().getMessage()  # type: ignore[misc]
        try:
            return redact_text(message)
        except Exception:  # noqa: BLE001 - fail closed rather than leak
            return WITHHELD_MESSAGE


_redacting_classes: dict[type, type] = {}
_redacting_classes_lock = threading.Lock()


def _redacting_class(base: type) -> type:
    if issubclass(base, _RedactedMessageMixin):
        return base
    with _redacting_classes_lock:
        derived = _redacting_classes.get(base)
        if derived is None:
            derived = type(f"Redacting{base.__name__}", (_RedactedMessageMixin, base), {})
            _redacting_classes[base] = derived
        return derived


def harden_record(record: logging.LogRecord) -> logging.LogRecord:
    """Redact *record* in place and return it. Never raises."""

    try:
        arguments = record.args
        if isinstance(arguments, Mapping):
            record.args = {
                key: _redact_argument(item, str(key)) for key, item in arguments.items()
            }
        elif arguments:
            record.args = tuple(_redact_argument(item) for item in arguments)
        elif isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_traceback(_format_exception(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact_traceback(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_traceback(record.stack_info)
        record.__class__ = _redacting_class(type(record))
    except Exception:  # noqa: BLE001 - fail closed rather than leak
        record.msg = WITHHELD_MESSAGE
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
    return record


_INSTALLED_MARKER = "_workout_agent_log_redaction"


def install_log_redaction(environ: Mapping[str, str] | None = None) -> None:
    """Redact every log record this process creates. Safe to call repeatedly.

    Call it once, as early as possible, from every process entry point (the web
    application package and each agent or script that configures logging).
    Registers the secret-bearing environment variables for exact-value masking.
    """

    register_environment_secrets(environ)
    previous = logging.getLogRecordFactory()
    if getattr(previous, _INSTALLED_MARKER, False):
        return

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return harden_record(previous(*args, **kwargs))

    setattr(redacting_factory, _INSTALLED_MARKER, True)
    logging.setLogRecordFactory(redacting_factory)


def log_redaction_installed() -> bool:
    """Whether the process-wide record factory is currently the redacting one."""

    return bool(getattr(logging.getLogRecordFactory(), _INSTALLED_MARKER, False))
