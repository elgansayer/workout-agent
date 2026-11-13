"""Resume the page that prompted sign-in without allowing an open redirect.

The OAuth start endpoint accepts a ``next`` destination, stores it in the signed
pre-login session and redirects there after a successful callback. Only an
absolute path on this origin is accepted: a scheme, authority, protocol-relative
path, backslash or control character is refused, so the parameter can never
become an off-site redirect. Authentication and loop endpoints are refused too.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

LOGIN_NEXT_SESSION_KEY = "login_next"

_MAX_LENGTH = 2048
_LOOP_PATHS = frozenset({"/login", "/logout", "/auth", "/api/auth"})
_LOOP_PREFIXES = ("/login/", "/auth/", "/api/auth/")


def safe_destination(value: object) -> str | None:
    """Return a safe same-origin path (with query), or ``None``.

    Fragments are dropped because they are never sent to the server and cannot
    affect which page is served. Percent-encoding is preserved rather than
    decoded, so a later decode cannot turn what we validated into another URL.
    """

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_LENGTH:
        return None
    if not candidate.startswith("/") or candidate.startswith("//"):
        return None
    if "\\" in candidate or any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in candidate
    ):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return None
    path = parts.path
    if path.startswith("//") or path in _LOOP_PATHS or path.startswith(_LOOP_PREFIXES):
        return None
    return f"{path}?{parts.query}" if parts.query else path


def login_redirect(path: str, query_string: str = "") -> str:
    """Return the sign-in URL that will resume *path* after authentication."""

    raw = f"{path}?{query_string}" if query_string else path
    destination = safe_destination(raw)
    if destination is None or destination == "/":
        return "/login"
    return "/login?next=" + quote(destination, safe="")


__all__ = [
    "LOGIN_NEXT_SESSION_KEY",
    "login_redirect",
    "safe_destination",
]
