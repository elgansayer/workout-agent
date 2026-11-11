"""The web throttle is a shared in-process cache: it must not cross tenants.

Every worker and request handler shares one process-wide ``_RATE_LIMITS`` map.
A bucket keyed only by a client-controlled address lets a neighbour behind the
same proxy, or a spoofed ``X-Forwarded-For``, spend another account's quota.
Signed-in requests are therefore bucketed by owner; only anonymous requests
fall back to the client address.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")

from fastapi import HTTPException
from starlette.requests import Request
from tenant_isolation import OwnerIdentity, owner_scoped_key
from webapp import app as web_app

ADDRESS = "203.0.113.10"


@pytest.fixture(autouse=True)
def _fresh_buckets(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[float]]:
    buckets: dict[str, list[float]] = {}
    monkeypatch.setattr(web_app, "_RATE_LIMITS", buckets)
    return buckets


def _request(
    *,
    session: dict[str, Any] | None = None,
    host: str = ADDRESS,
    forwarded: str | None = None,
) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/example",
            "headers": headers,
            "client": (host, 50000),
            "session": session if session is not None else {},
        }
    )


def _signed_in(user_id: str, **kwargs: Any) -> Request:
    return _request(session={"user_id": user_id, "user": {"id": user_id}}, **kwargs)


def _spend(request: Request, times: int, *, limit: int) -> None:
    for _ in range(times):
        web_app._check_rate_limit(request, limit=limit)


def _assert_throttled(request: Request, *, limit: int) -> None:
    with pytest.raises(HTTPException) as denied:
        web_app._check_rate_limit(request, limit=limit)
    assert denied.value.status_code == 429


def test_signed_in_requests_are_throttled_per_owner_not_per_address() -> None:
    _spend(_signed_in("owner-a"), 2, limit=2)

    _assert_throttled(_signed_in("owner-a"), limit=2)
    # Same address, different owner: untouched by owner-a's traffic.
    _spend(_signed_in("owner-b"), 2, limit=2)


def test_one_owner_is_throttled_across_every_address_they_use() -> None:
    _spend(_signed_in("owner-a", host="203.0.113.1"), 2, limit=2)

    _assert_throttled(_signed_in("owner-a", host="203.0.113.2"), limit=2)
    _assert_throttled(_signed_in("owner-a", forwarded="198.51.100.9"), limit=2)


def test_a_spoofed_forwarded_for_cannot_spend_another_owners_quota() -> None:
    victim_address = "198.51.100.7"
    # An anonymous attacker claims the victim's address until it is throttled.
    _spend(_request(forwarded=victim_address), 2, limit=2)
    _assert_throttled(_request(forwarded=victim_address), limit=2)
    # A signed-in attacker can only burn their own account's budget.
    _spend(_signed_in("attacker", forwarded=victim_address), 2, limit=2)
    _assert_throttled(_signed_in("attacker", forwarded=victim_address), limit=2)

    # The victim, signed in from that very address, still has a full budget.
    _spend(_signed_in("victim", forwarded=victim_address), 2, limit=2)


def test_anonymous_requests_are_still_throttled_per_client_address() -> None:
    _spend(_request(host="203.0.113.1"), 2, limit=2)

    _assert_throttled(_request(host="203.0.113.1"), limit=2)
    _spend(_request(host="203.0.113.2"), 2, limit=2)


def test_owner_and_address_buckets_cannot_collide() -> None:
    assert web_app._rate_limit_key(_signed_in(ADDRESS)) != web_app._rate_limit_key(
        _request(host=ADDRESS)
    )


def test_the_owner_bucket_is_the_shared_owner_scoped_key() -> None:
    expected = owner_scoped_key(OwnerIdentity("owner-a"), "rate-limit")

    assert web_app._rate_limit_key(_signed_in("owner-a")) == expected
    # The identity may live only on the session's user record.
    assert web_app._rate_limit_key(_request(session={"user": {"id": "owner-a"}})) == (
        expected
    )
    # An explicit user_id wins over a stale user record.
    assert (
        web_app._rate_limit_key(
            _request(session={"user_id": "owner-a", "user": {"id": "owner-z"}})
        )
        == expected
    )


@pytest.mark.parametrize("identity", ["   ", " padded ", "x" * 129, "bad\nid"])
def test_a_malformed_session_identity_falls_back_to_the_client_address(
    identity: str,
) -> None:
    key = web_app._rate_limit_key(_request(session={"user_id": identity}))

    assert key == f"rate-limit-ip:{ADDRESS}"


def test_the_bucket_for_each_owner_holds_only_that_owners_requests(
    _fresh_buckets: dict[str, list[float]],
) -> None:
    _spend(_signed_in("owner-a"), 2, limit=5)
    _spend(_signed_in("owner-b"), 1, limit=5)

    assert {key: len(stamps) for key, stamps in _fresh_buckets.items()} == {
        owner_scoped_key(OwnerIdentity("owner-a"), "rate-limit"): 2,
        owner_scoped_key(OwnerIdentity("owner-b"), "rate-limit"): 1,
    }
