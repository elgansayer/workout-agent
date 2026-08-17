"""Make the project root importable when running the test suite."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
