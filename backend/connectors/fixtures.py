"""Utilities that enforce synthetic, secret-free connector fixtures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


FORBIDDEN_KEYS = frozenset({"access_token", "refresh_token", "api_key", "client_secret", "authorization", "cookie"})


def assert_safe_fixture(value: Any, *, path: str = "fixture") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ValueError(f"secret-bearing key is forbidden in {path}: {key}")
            assert_safe_fixture(item, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            assert_safe_fixture(item, path=f"{path}[{index}]")
