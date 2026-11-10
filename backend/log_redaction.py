"""Central, fail-closed redaction for logs, diagnostics, and error responses.

Every process that can handle credentials or user data installs this layer
(``install_log_redaction``) so the same rules apply to application loggers,
third-party loggers (uvicorn, httpx, SDKs), exception tracebacks, and uncaught
exceptions. The field vocabulary comes from :mod:`data_classification`; this
module adds the value-shape rules (bearer tokens, API-key formats, OAuth codes,
cookies, e-mail addresses) that apply even when a secret appears in free text
with no field name next to it.

Redaction is deliberately conservative: a false positive costs a little
debugging context, a false negative leaks a credential or personal data.
"""

from __future__ import annotations

import hashlib
import logging
import re
import string
import sys
import traceback
from functools import lru_cache
from types import TracebackType
from typing import Any

from data_classification import redact_for_log, safe_log_value

REDACTED_CREDENTIAL = safe_log_value("api_key", "")
REDACTED_IDENTIFIER = safe_log_value("email", "")

MAX_REDACTION_CHARS = 131_072
TRUNCATION_NOTICE = "[TRUNCATED]"

_INSTALLED_MARKER = "_workout_log_redaction_installed"
_ACCESS_LOGGERS = frozenset({"uvicorn.access"})
_ACCESS_LOG_ARGUMENTS = 5

# Header lines and header-shaped mappings whose whole value is a credential.
# The value runs to the end of the line or the closing quote of a dict/JSON
# repr, so multi-word values such as ``Bearer <token>`` and cookie jars go too.
_CREDENTIAL_HEADER = re.compile(
    r"(?P<head>\b(?:proxy[-_])?authorization|\b(?:set[-_])?cookie)"
    r"(?P<sep>[\"']?[ \t]*[:=][ \t]*[\"']?)"
    r"(?P<value>[^\r\n\"']+)",
    re.IGNORECASE,
)

# Query-string / form parameters that carry one-time OAuth material, or a
# precise location. Matched only after ``?`` or ``&`` so that prose such as
# "exit code=1" is left alone.
_SECRET_PARAM = re.compile(
    r"(?P<head>[?&](?:code|state|code_verifier|session_state|key|signature)=)"
    r"(?P<value>[^&\s\"'#]*)",
    re.IGNORECASE,
)
_LOCATION_PARAM = re.compile(
    r"(?P<head>[?&](?:latitude|longitude|lat|lon|lng)=)(?P<value>[^&\s\"'#]*)",
    re.IGNORECASE,
)
# ``{"code": "4/0AX..."}`` in a JSON or dict dump (OAuth token-exchange body).
_OAUTH_CODE_FIELD = re.compile(
    r"(?P<head>[\"']code[\"']\s*:\s*[\"'])(?P<value>[^\"'\s]{10,})",
    re.IGNORECASE,
)

_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_URL_USERINFO = re.compile(
    r"(?P<scheme>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{0,31}://)"
    r"[^/\s:@]{1,256}:[^/\s@]{1,256}@"
)
_BEARER = re.compile(
    r"\b(?P<scheme>Bearer)\s{1,8}[A-Za-z0-9._~+/=\-]{8,4096}", re.IGNORECASE
)

# Credential formats that are recognisable without a field name beside them.
# Every repeat is bounded so a failed attempt costs a constant, never a rescan
# of an arbitrarily long run (see ``MAX_REDACTION_CHARS``).
_CREDENTIAL_SHAPES = tuple(
    re.compile(pattern)
    for pattern in (
        # JWTs and signed session cookies (payload.timestamp.signature).
        r"\beyJ[A-Za-z0-9_\-]{8,512}\.[A-Za-z0-9_\-]{3,4096}\.[A-Za-z0-9_\-]{0,1024}",
        # OpenAI, Anthropic, DeepSeek keys, including provider-masked forms.
        r"\bsk-[A-Za-z0-9_*\-]{6,256}",
        # Google API keys, OAuth access/refresh tokens, authorisation codes.
        r"\bAIza[0-9A-Za-z_\-]{20,128}",
        r"\bya29\.[0-9A-Za-z_\-]{10,2048}",
        r"(?<![\w/])1//[0-9A-Za-z_\-]{10,512}",
        r"(?<![\w/])4/0[0-9A-Za-z_\-]{18,512}",
        # GitHub tokens, AWS access keys.
        r"\bgh[pousr]_[A-Za-z0-9]{20,255}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,255}",
        r"\bAKIA[0-9A-Z]{16}\b",
        # Telegram bot tokens (also appear inside request URLs: /bot<token>/).
        r"(?<!\d)\d{6,12}:[A-Za-z0-9_\-]{30,64}",
        # Fernet ciphertext (encrypted stored credentials).
        r"\bgAAAA[A-Za-z0-9_\-=]{20,4096}",
    )
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@"
    r"[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,8}\.[A-Za-z]{2,24}"
)

# Leading padding is bounded: an unbounded run would make every position in a
# long whitespace run rescan the rest of it.
_SEPARATOR = re.compile(r"[\"']?[ \t]{0,32}[:=][ \t]*")
_KEY_CHARS = frozenset(string.ascii_letters + string.digits + "_.-")
_MAX_KEY_CHARS = 128
_BARE_VALUE = re.compile(r"[^\s,;&)\]}\"']+")
_OPENERS = "{[("
_CLOSERS = "}])"
_QUOTES = "\"'"

# Used to ask ``safe_log_value`` whether a field may be logged without
# duplicating its decision logic: the probe comes back unchanged if it may.
_PROBE = object()


@lru_cache(maxsize=2048)
def _marker_for_field(field_name: str) -> str | None:
    """Return the redaction marker for *field_name*, or None if it is loggable."""
    result = safe_log_value(field_name.lower().replace("-", "_"), _PROBE)
    return None if result is _PROBE else str(result)


def _value_end(text: str, start: int) -> int:
    """Return the index just past the value that begins at *start*.

    Understands quoted strings (with escapes) and balanced ``{}``/``[]``/``()``
    containers so a classified field redacts its whole nested payload. An
    unterminated value consumes the rest of the text (fail closed).
    """
    length = len(text)
    if start >= length:
        return start
    first = text[start]
    if first in _QUOTES:
        index = start + 1
        while index < length:
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == first:
                return index + 1
            index += 1
        return length
    if first in _OPENERS:
        depth = 0
        quote = ""
        index = start
        while index < length:
            char = text[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = ""
            elif char in _QUOTES:
                quote = char
            elif char in _OPENERS:
                depth += 1
            elif char in _CLOSERS:
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        return length
    match = _BARE_VALUE.match(text, start)
    return match.end() if match else start


def _marker_in_quotes(text: str, start: int, end: int, marker: str) -> str:
    """Return *marker*, keeping the quotes of the value it replaces.

    Preserving the delimiters keeps redacted JSON and dict dumps well formed.
    """
    first = text[start]
    if first not in _QUOTES:
        return marker
    closed = end - start >= 2 and text[end - 1] == first
    return f"{first}{marker}{first}" if closed else f"{first}{marker}"


def _key_before(text: str, end: int) -> str:
    """Return the field name that ends at *end* (at most ``_MAX_KEY_CHARS``)."""
    start = end
    while start > 0 and end - start < _MAX_KEY_CHARS and text[start - 1] in _KEY_CHARS:
        start -= 1
    while start < end and not (text[start].isalpha() or text[start] == "_"):
        start += 1
    return text[start:end]


def _redact_field_values(text: str) -> str:
    """Redact the value of every ``name=value`` / ``"name": value`` pair whose
    name is classified as sensitive by :mod:`data_classification`.

    Separators are located first and the name is read backwards from each, so
    the scan is linear however long a run of identifier characters is.
    """
    pieces: list[str] = []
    position = 0
    for match in _SEPARATOR.finditer(text):
        if match.start() < position:
            continue
        key = _key_before(text, match.start())
        marker = _marker_for_field(key) if key else None
        if marker is None:
            continue
        value_start = match.end()
        value_end = _value_end(text, value_start)
        if value_end == value_start:
            continue
        pieces.append(text[position:value_start])
        pieces.append(_marker_in_quotes(text, value_start, value_end, marker))
        position = value_end
    if not pieces:
        return text
    pieces.append(text[position:])
    return "".join(pieces)


def _redact_bounded(text: str) -> str:
    redacted = _PEM_BLOCK.sub(REDACTED_CREDENTIAL, text)
    redacted = _CREDENTIAL_HEADER.sub(
        lambda m: f"{m.group('head')}{m.group('sep')}{REDACTED_CREDENTIAL}", redacted
    )
    redacted = _URL_USERINFO.sub(
        lambda m: f"{m.group('scheme')}{REDACTED_CREDENTIAL}@", redacted
    )
    redacted = _SECRET_PARAM.sub(
        lambda m: f"{m.group('head')}{REDACTED_CREDENTIAL}", redacted
    )
    redacted = _LOCATION_PARAM.sub(
        lambda m: f"{m.group('head')}{REDACTED_IDENTIFIER}", redacted
    )
    redacted = _OAUTH_CODE_FIELD.sub(
        lambda m: f"{m.group('head')}{REDACTED_CREDENTIAL}", redacted
    )
    redacted = _redact_field_values(redacted)
    redacted = _BEARER.sub(
        lambda m: f"{m.group('scheme')} {REDACTED_CREDENTIAL}", redacted
    )
    for shape in _CREDENTIAL_SHAPES:
        redacted = shape.sub(REDACTED_CREDENTIAL, redacted)
    return _EMAIL.sub(REDACTED_IDENTIFIER, redacted)


def redact_text(text: str) -> str:
    """Return *text* with credentials and personal data replaced by markers.

    Covers authorization headers, API keys, OAuth codes and tokens, cookies,
    URL-embedded passwords, e-mail addresses, and the value of any
    ``name=value`` pair whose name classifies as credential, identifier, health,
    workout, prompt, or analytics data. Redacting redacted text changes nothing.

    Input beyond ``MAX_REDACTION_CHARS`` is cut off (and flagged) rather than
    scanned, which bounds the cost of a log call whatever it is asked to log.
    """
    if not text:
        return text
    if len(text) > MAX_REDACTION_CHARS:
        return _redact_bounded(text[:MAX_REDACTION_CHARS]) + TRUNCATION_NOTICE
    return _redact_bounded(text)


def redact_value(value: Any, *, field_name: str | None = None) -> Any:
    """Recursively redact a structure: by field name first, then by value shape."""
    return redact_for_log(value, field_name=field_name, text_redactor=redact_text)


def pseudonymise_identifier(identifier: str) -> str:
    """Return a stable, non-reversible tag for an opaque identifier.

    Intended for random high-entropy identifiers such as the UUID user IDs, so
    operators can correlate log lines without exposing the identifier itself.
    Low-entropy values such as e-mail addresses must be redacted instead,
    because an unkeyed hash of them can be recovered by guessing.
    """
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return f"id:{digest[:12]}"


def _scrub_request_path(full_path: object) -> object:
    """Drop the query string from an access-log request path.

    Query strings carry OAuth codes and, for the coach endpoint, the user's
    question, so no part of them is ever logged.
    """
    if not isinstance(full_path, str):
        return full_path
    path, separator, _query = full_path.partition("?")
    return redact_text(path) + (f"?{REDACTED_CREDENTIAL}" if separator else "")


def _scrub_text_arg(value: object) -> object:
    return redact_text(value) if isinstance(value, str) else value


def _scrub_access_args(args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Scrub uvicorn's (client, method, path, http_version, status) arguments."""
    client, method, full_path, http_version, status = args
    return (
        _scrub_text_arg(client),
        _scrub_text_arg(method),
        _scrub_request_path(full_path),
        _scrub_text_arg(http_version),
        status,
    )


_STANDARD_ATTRIBUTES = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _format_exc_info(exc_info: Any) -> str:
    return "".join(traceback.format_exception(*exc_info)).rstrip("\n")


def _scrub_record(record: logging.LogRecord) -> None:
    """Redact one log record in place; on any failure drop its content."""
    try:
        if (
            record.name in _ACCESS_LOGGERS
            and isinstance(record.args, tuple)
            and len(record.args) == _ACCESS_LOG_ARGUMENTS
        ):
            # uvicorn's AccessFormatter unpacks these five positional arguments
            # itself, so keep their shape. Any other call on this logger takes
            # the general path below and is redacted like every other record.
            record.args = _scrub_access_args(record.args)
        else:
            record.msg = redact_text(record.getMessage())
            record.args = ()

        if record.exc_info and not record.exc_text:
            record.exc_text = redact_text(_format_exc_info(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
    except Exception:  # noqa: BLE001 - a log call must never raise, nor leak
        record.msg = "[log message withheld: redaction failed]"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None


def _scrub_extra_attributes(record: logging.LogRecord) -> None:
    """Redact ``extra=`` attributes, which are attached after the factory runs."""
    # uvicorn's coloured formatter re-formats this presentational template with
    # the record's original arguments. They are already folded into the final
    # message, so it would print raw ``%d`` placeholders; the plain message is
    # used instead.
    record.__dict__.pop("color_message", None)
    try:
        for name, value in list(record.__dict__.items()):
            if name not in _STANDARD_ATTRIBUTES:
                record.__dict__[name] = redact_value(value, field_name=name)
    except Exception:  # noqa: BLE001 - fail closed: drop what cannot be scrubbed
        for name in [n for n in record.__dict__ if n not in _STANDARD_ATTRIBUTES]:
            del record.__dict__[name]


def _log_uncaught(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    logging.getLogger("workout_agent.uncaught").critical(
        "Uncaught exception", exc_info=(exc_type, exc, tb)
    )


def _install_excepthook() -> None:
    previous = sys.excepthook
    if getattr(previous, _INSTALLED_MARKER, False):
        return

    def hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        _log_uncaught(exc_type, exc, tb)

    setattr(hook, _INSTALLED_MARKER, True)
    sys.excepthook = hook


def install_log_redaction() -> None:
    """Redact every log record process-wide. Safe to call more than once.

    A log-record factory (rather than a handler filter) is used so records from
    every logger and handler are covered, including handlers that third-party
    code or the web server adds later. ``Logger.makeRecord`` is wrapped as well
    because ``extra=`` attributes are attached after the factory returns.
    Uncaught exceptions are routed through logging too, instead of the
    interpreter's unredacted default printout.
    """
    previous = logging.getLogRecordFactory()
    if not getattr(previous, _INSTALLED_MARKER, False):

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            _scrub_record(record)
            return record

        setattr(factory, _INSTALLED_MARKER, True)
        logging.setLogRecordFactory(factory)

    original_make_record = logging.Logger.makeRecord
    if not getattr(original_make_record, _INSTALLED_MARKER, False):

        def make_record(
            self: logging.Logger, *args: Any, **kwargs: Any
        ) -> logging.LogRecord:
            record = original_make_record(self, *args, **kwargs)
            _scrub_extra_attributes(record)
            return record

        setattr(make_record, _INSTALLED_MARKER, True)
        logging.Logger.makeRecord = make_record  # type: ignore[method-assign]
    _install_excepthook()


__all__ = [
    "MAX_REDACTION_CHARS",
    "REDACTED_CREDENTIAL",
    "REDACTED_IDENTIFIER",
    "TRUNCATION_NOTICE",
    "install_log_redaction",
    "pseudonymise_identifier",
    "redact_text",
    "redact_value",
]
