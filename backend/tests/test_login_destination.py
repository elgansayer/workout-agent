"""Post-login destination validation and sign-in redirects."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from webapp.login_destination import login_redirect, safe_destination


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/plan", "/plan"),
        ("/plan?week=2", "/plan?week=2"),
        ("/settings?section=connections", "/settings?section=connections"),
        ("/onboarding#step-2", "/onboarding"),
        ("  /dashboard  ", "/dashboard"),
    ],
)
def test_safe_destination_accepts_same_origin_paths(raw: str, expected: str) -> None:
    assert safe_destination(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        7,
        "",
        "   ",
        "plan",
        "https://evil.example/plan",
        "//evil.example/plan",
        "/\\evil.example/plan",
        "/\r\nLocation: https://evil.example",
        "/\x00plan",
        "/login",
        "/login/google?next=/plan",
        "/auth",
        "/logout",
        "/api/auth/logout",
        "/" + "a" * 3000,
    ],
)
def test_safe_destination_rejects_off_site_or_looping_targets(raw: object) -> None:
    assert safe_destination(raw) is None


def test_login_redirect_carries_the_encoded_destination() -> None:
    location = login_redirect("/plan", "week=2")

    assert location.startswith("/login?next=")
    next_value = parse_qs(urlsplit(location).query)["next"][0]
    assert next_value == "/plan?week=2"


def test_login_redirect_omits_the_default_destination() -> None:
    assert login_redirect("/") == "/login"
    assert login_redirect("//evil.example") == "/login"
    assert login_redirect("https://evil.example") == "/login"
