#!/bin/sh
# Entry point for the containerised workout agent.
#
# All modes, scheduling, and run times are now handled by the unified
# scheduler.py (see that file for documentation).  The old bash sleep-loop
# and background insight_scheduler.py have been removed — one process,
# one scheduler.
set -e

exec python scheduler.py
