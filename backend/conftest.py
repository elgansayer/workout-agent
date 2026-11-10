"""Make the project root importable when running the test suite."""

from __future__ import annotations

import atexit
import os
import sys
import tempfile

# Tests intentionally exercise the dashboard without live Google OAuth. Make
# that exception explicit before any ``webapp`` package is imported; production
# ignores this flag and still requires complete authentication credentials.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ALLOW_ANONYMOUS_WEB", "1")

# Unhandled-error tests write protected diagnostics; keep them out of the
# working tree unless a test points DIAGNOSTICS_DIR somewhere specific.
_DIAGNOSTICS_TMP = tempfile.TemporaryDirectory(prefix="workout-agent-diagnostics-")
atexit.register(_DIAGNOSTICS_TMP.cleanup)
os.environ.setdefault("DIAGNOSTICS_DIR", _DIAGNOSTICS_TMP.name)

sys.path.insert(0, os.path.dirname(__file__))

# Run the suite with the same log redaction the production entry points install,
# so no test depends on logging behaviour that production does not have.
from log_redaction import install_log_redaction

install_log_redaction()
