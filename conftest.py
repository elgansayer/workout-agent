"""Make the project root importable and isolate the test environment."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# Anonymous web access is never implicit. The test suite opts into the same
# explicit development-only flag required by local `uvicorn` sessions.
os.environ["APP_ENV"] = "test"
os.environ["WEB_ALLOW_ANONYMOUS"] = "1"
