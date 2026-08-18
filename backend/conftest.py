"""Make the project root importable when running the test suite."""

from __future__ import annotations

import os
import sys

# Tests intentionally exercise the dashboard without live Google OAuth. Make
# that exception explicit before any ``webapp`` package is imported; production
# ignores this flag and still requires complete authentication credentials.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ALLOW_ANONYMOUS_WEB", "1")

sys.path.insert(0, os.path.dirname(__file__))
