"""Opaque error references and the protected diagnostics sink.

Clients only ever receive an opaque ``err_<hex>`` reference. The redacted
traceback is appended to a private file (directory ``0700``, file ``0600``)
keyed by that reference, so an operator can look up a reported failure while
the HTTP response, the browser, and the general application log carry neither
the exception text nor the stack.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from log_redaction import redact_text

logger = logging.getLogger(__name__)

ERROR_ID_PREFIX = "err_"
DIAGNOSTICS_FILENAME = "errors.jsonl"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TRACE_CHARS = 32_000

_write_lock = threading.Lock()


def new_error_id() -> str:
    """Return an unguessable reference that encodes nothing about the failure."""
    return f"{ERROR_ID_PREFIX}{secrets.token_hex(8)}"


def diagnostics_path() -> Path:
    """Return the diagnostics file: ``DIAGNOSTICS_DIR`` or next to the database."""
    configured = os.environ.get("DIAGNOSTICS_DIR", "").strip()
    if configured:
        directory = Path(configured).expanduser()
    else:
        database = os.environ.get("DATABASE_PATH", "").strip() or "workout_agent.db"
        directory = Path(database).expanduser().resolve().parent / "diagnostics"
    return directory / DIAGNOSTICS_FILENAME


def _rotate_if_full(path: Path) -> None:
    try:
        if path.stat().st_size >= MAX_FILE_BYTES:
            os.replace(path, path.with_name(f"{path.name}.1"))
    except FileNotFoundError:
        return


def _append_private(path: Path, line: str) -> None:
    """Append one line, creating the directory and file owner-only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    with _write_lock:
        _rotate_if_full(path)
        descriptor = os.open(path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            os.write(descriptor, line.encode("utf-8"))
        finally:
            os.close(descriptor)


def record_exception(
    error_id: str,
    exc: BaseException,
    *,
    method: str = "",
    route: str = "",
) -> bool:
    """Persist the redacted traceback for *error_id*.

    Only the route template is recorded, never the concrete URL, query string,
    headers, body, or any user identifier. Returns ``False`` when the private
    sink cannot be written; the redacted trace then goes to the server log so
    the failure is not lost to operators.
    """
    trace = redact_text(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    )[-MAX_TRACE_CHARS:]
    entry = {
        "error_id": error_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "method": redact_text(method),
        "route": redact_text(route),
        "exception": type(exc).__qualname__,
        "trace": trace,
    }
    line = json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n"
    try:
        _append_private(diagnostics_path(), line)
    except OSError:
        logger.error(
            "Diagnostics sink unavailable for %s; redacted traceback follows.\n%s",
            error_id,
            trace,
        )
        return False
    return True


__all__ = [
    "DIAGNOSTICS_FILENAME",
    "ERROR_ID_PREFIX",
    "diagnostics_path",
    "new_error_id",
    "record_exception",
]
