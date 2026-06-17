"""Make the project root importable when running the test suite."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
