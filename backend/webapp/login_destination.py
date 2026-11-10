"""Where a person returns to after signing in.

A visitor who opens a deep link (or whose session ended mid-task) must come back
to that page after Google sign-in instead of landing on the dashboard. The
destination is therefore carried through the OAuth round trip, but only in forms
that cannot be abused:

* It is a same-origin **path** (with optional query and fragment), never a URL:
  anything with a scheme, a host, a protocol-relative ``//`` prefix, a
  backslash, a control character or a dot segment is dropped, so the sign-in
  redirect cannot be turned into an open redirect.
* It never points at the sign-in machinery or an API (``/login``, ``/auth``,
  ``/api`` ...), so signing in cannot loop or land on a JSON endpoint.
* It travels in the signed session cookie (tamper-evident, and replaced at
  sign-in), not in the OAuth ``state`` or the redirect URI that Google sees,
  and it is validated again when it is read back. It is cleared whenever
  sign-in is refused.

An invalid destination is not an error: the person simply lands on the default
page.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any
from urllib.parse import quote, unquote, urlsplit

LOGIN_DESTINATION_SESSION_KEY = "login_next"
DEFAULT_DESTINATION = "/"
MAX_DESTINATION_LENGTH = 512

# Sign-in machinery, APIs, static assets and operational endpoints are never a
# place to "return to". Matched on the decoded path, case-insensitively, as the
# path itself or a parent of it.
_EXCLUDED_PREFIXES = (
    "/login",
    "/logout",
    "/auth",
    "/api",
    "/static",
    "/assets",
    "/google-health",
    "/livez",
    "/readyz",
)
_EXCLUDED_PATHS = frozenset({"/sw.js", "/favicon.ico"})


def _has_unsafe_character(value: str, *, allow_space: bool = False) -> bool:
    return any(
        ord(char) < (0x20 if allow_space else 0x21) or ord(char) == 0x7F or char == "\\"
        for char in value
    )


def safe_destination(raw: object) -> str | None:
    """Return ``raw`` if it is a safe same-origin destination, else ``None``.

    The value is returned unchanged (not re-encoded), so what the caller stored
    is exactly what was validated.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_DESTINATION_LENGTH:
        return None
    if not raw.isascii() or not raw.startswith("/") or raw.startswith("//"):
        return None
    if _has_unsafe_character(raw):
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        return None
    # Percent-encoding must not smuggle in what the raw check refused: some
    # routers and user agents decode before they interpret the path.
    path = unquote(parts.path)
    if not path.startswith("/") or path.startswith("//"):
        return None
    if _has_unsafe_character(path, allow_space=True):
        return None
    if any(segment in {".", ".."} for segment in path.split("/")):
        return None
    lowered = path.lower()
    if lowered in _EXCLUDED_PATHS or any(
        lowered == prefix or lowered.startswith(prefix + "/")
        for prefix in _EXCLUDED_PREFIXES
    ):
        return None
    return raw


def remember_destination(session: MutableMapping[str, Any], raw: object) -> None:
    """Record the requested destination for the sign-in about to start.

    Always replaces what an earlier, abandoned attempt left behind: a destination
    that is missing, unsafe or just the default clears the stored one.
    """
    destination = safe_destination(raw)
    if destination is None or destination == DEFAULT_DESTINATION:
        session.pop(LOGIN_DESTINATION_SESSION_KEY, None)
    else:
        session[LOGIN_DESTINATION_SESSION_KEY] = destination


def take_destination(session: MutableMapping[str, Any]) -> str | None:
    """Remove and return the stored destination, validating it once more."""
    return safe_destination(session.pop(LOGIN_DESTINATION_SESSION_KEY, None))


def login_url(destination: object = None) -> str:
    """The sign-in page URL that resumes at ``destination`` after sign-in."""
    safe = safe_destination(destination)
    if safe is None or safe == DEFAULT_DESTINATION:
        return "/login"
    return f"/login?next={quote(safe, safe='')}"


def login_url_for_request(method: str, path: str, query_string: bytes = b"") -> str:
    """The sign-in page URL for a request that needs a session it does not have.

    Only a page navigation (``GET``/``HEAD``) can be resumed; a refused
    ``POST`` or other method sends the person to a plain sign-in page. ``path``
    is the decoded ASGI path and ``query_string`` the raw one.
    """
    if method.upper() not in {"GET", "HEAD"}:
        return "/login"
    target = quote(path, safe="/")
    if query_string:
        target += "?" + query_string.decode("ascii", "ignore")
    return login_url(target)


__all__ = [
    "DEFAULT_DESTINATION",
    "LOGIN_DESTINATION_SESSION_KEY",
    "MAX_DESTINATION_LENGTH",
    "login_url",
    "login_url_for_request",
    "remember_destination",
    "safe_destination",
    "take_destination",
]
