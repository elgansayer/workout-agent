"""The post-sign-in destination: same-origin page paths only.

A destination that survives validation is only ever somewhere inside this
application, so carrying it through the OAuth round trip cannot become an open
redirect, and it can never send the person back into the sign-in machinery.
All paths are synthetic.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from webapp.login_destination import (
    LOGIN_DESTINATION_SESSION_KEY,
    MAX_DESTINATION_LENGTH,
    login_url,
    login_url_for_request,
    remember_destination,
    safe_destination,
    take_destination,
)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/plan",
        "/plan?week=2",
        "/settings#hevy",
        "/programmes?goal=strength&weeks=8#preview",
        "/history/2026-11-05",
        "/plan%20b",  # an encoded space is data, not whitespace
        "/loginx",  # shares a prefix with /login but is a different page
        "/authors",
        "/api-docs",
    ],
)
def test_same_origin_page_paths_are_kept_unchanged(path: str) -> None:
    assert safe_destination(path) == path


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        0,
        ["/plan"],
        {"next": "/plan"},
        "plan",  # relative, not absolute
        "//evil.example",
        "//evil.example/plan",
        "///evil.example",
        "/\\evil.example",
        "\\\\evil.example",
        "/plan\\..\\evil",
        "https://evil.example/plan",
        "http://evil.example",
        "javascript:alert(1)",
        "data:text/html,x",
        "/%2F/evil.example",  # decodes to //evil.example
        "/%2f/evil.example",
        "/%5Cevil.example",  # decodes to \evil.example
        "/plan\r\nSet-Cookie: x=1",
        "/plan%0d%0aSet-Cookie:x=1",
        "/plan%00",
        "/plan\x00",
        "/plan\tb",
        "/plan b",
        "/plan b",
        "/pläne",
        "/../etc/passwd",
        "/plan/../../etc",
        "/plan/./x",
        "/%2e%2e/secret",
        "/" + "a" * MAX_DESTINATION_LENGTH,
    ],
)
def test_anything_that_could_leave_the_site_or_inject_a_header_is_dropped(raw: Any) -> None:
    assert safe_destination(raw) is None


@pytest.mark.parametrize(
    "path",
    [
        "/login",
        "/login/google",
        "/login?error=access_denied",
        "/Login",
        "/%6Cogin",
        "/logout",
        "/auth",
        "/auth?code=1",
        "/api",
        "/api/me",
        "/api%2Fme",
        "/API/settings",
        "/static/app.css",
        "/assets/main.js",
        "/google-health/connect",
        "/google-health/callback?code=1",
        "/livez",
        "/readyz",
        "/sw.js",
        "/favicon.ico",
    ],
)
def test_the_sign_in_machinery_and_apis_are_never_a_destination(path: str) -> None:
    assert safe_destination(path) is None


@settings(max_examples=400, deadline=None)
@given(st.text(max_size=120))
def test_whatever_survives_is_a_path_inside_this_application(raw: str) -> None:
    result = safe_destination(raw)
    if result is None:
        return
    assert result == raw
    parts = urlsplit(result)
    assert not parts.scheme and not parts.netloc
    assert result.startswith("/") and not result.startswith("//")
    decoded = unquote(parts.path)
    assert not decoded.startswith("//")
    assert "\\" not in result and "\\" not in decoded
    assert not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in result + decoded)
    assert result.isascii()
    assert len(result) <= MAX_DESTINATION_LENGTH


def test_remember_stores_a_safe_destination_and_take_removes_it() -> None:
    session: dict[str, Any] = {}
    remember_destination(session, "/plan?week=2")
    assert session == {LOGIN_DESTINATION_SESSION_KEY: "/plan?week=2"}

    assert take_destination(session) == "/plan?week=2"
    assert session == {}
    assert take_destination(session) is None  # single use


@pytest.mark.parametrize("raw", [None, "", "/", "//evil.example", "https://evil.example", "/login"])
def test_remember_clears_what_an_abandoned_attempt_left_behind(raw: Any) -> None:
    session: dict[str, Any] = {LOGIN_DESTINATION_SESSION_KEY: "/plan"}
    remember_destination(session, raw)
    assert LOGIN_DESTINATION_SESSION_KEY not in session


@pytest.mark.parametrize(
    "stored",
    ["//evil.example", "https://evil.example", "/login", 7, None, ["/plan"], "/plan\r\n"],
)
def test_take_validates_again_so_a_bad_stored_value_is_never_used(stored: Any) -> None:
    session: dict[str, Any] = {LOGIN_DESTINATION_SESSION_KEY: stored}
    assert take_destination(session) is None
    assert LOGIN_DESTINATION_SESSION_KEY not in session


@pytest.mark.parametrize("destination", [None, "", "/", "//evil.example", "/login", "/api/me", 5])
def test_sign_in_url_is_plain_without_a_usable_destination(destination: Any) -> None:
    assert login_url(destination) == "/login"


def test_sign_in_url_carries_an_encoded_destination() -> None:
    url = login_url("/plan?week=2&x=a b".replace(" ", "%20"))
    assert url.startswith("/login?next=")
    assert parse_qs(urlsplit(url).query)["next"] == ["/plan?week=2&x=a%20b"]
    assert "?week" not in url  # the destination's own query is encoded, not merged


def test_a_refused_page_navigation_resumes_at_that_page() -> None:
    assert login_url_for_request("GET", "/plan", b"") == "/login?next=%2Fplan"
    assert login_url_for_request("get", "/plan", b"week=2&x=1") == (
        "/login?next=%2Fplan%3Fweek%3D2%26x%3D1"
    )
    assert login_url_for_request("HEAD", "/history", b"") == "/login?next=%2Fhistory"


def test_the_home_page_is_the_default_so_it_adds_nothing() -> None:
    assert login_url_for_request("GET", "/", b"") == "/login"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_only_a_page_navigation_can_be_resumed(method: str) -> None:
    assert login_url_for_request(method, "/plan", b"") == "/login"


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("//evil.example", b""),
        ("/api/me", b""),
        ("/login", b""),
        ("/plan\r\nx", b""),
        ("/plan", b"a=\r\nSet-Cookie: x=1"),
        ("/plan", b"a=\xff\xfe"),
    ],
)
def test_a_hostile_request_target_falls_back_to_a_plain_sign_in_page(
    path: str, query: bytes
) -> None:
    location = login_url_for_request("GET", path, query)
    # Either nothing is carried, or what is carried is inert, encoded data.
    assert location == "/login" or (
        "\r" not in location and "\n" not in location and location.startswith("/login?next=%2F")
    )
    assert not location.startswith(("//", "http"))


def test_a_decoded_path_is_re_encoded_so_it_stays_one_path() -> None:
    location = login_url_for_request("GET", "/plan b/100%", b"")
    assert location == "/login?next=%2Fplan%2520b%2F100%2525"
    # What the sign-in page receives as ``next`` (one level of decoding) is
    # itself a valid, still-encoded destination that names the same page.
    received = parse_qs(urlsplit(location).query)["next"][0]
    assert received == "/plan%20b/100%25"
    assert safe_destination(received) == received
