"""Deprecated -- scheduling has moved to scheduler.py.

This module exists as a backward-compatibility shim.  When invoked directly
it delegates to the unified scheduler.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    from scheduler import main as scheduler_main

    sys.exit(scheduler_main())
