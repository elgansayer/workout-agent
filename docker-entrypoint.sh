#!/bin/sh
# Entry point for the containerised workout agent.
#
# MODE controls behaviour:
#   schedule (default) - run the unified scheduler forever.
#   once               - run a single live cycle and exit.
#   preview            - print today's plan to stdout and exit (sends nothing).
#
# RUN_AT is one or more 24-hour HH:MM times, comma or space separated,
# defaulting to 07:00. For example RUN_AT="00:00,05:00" runs at midnight and
# 5am. Set TZ (e.g. Europe/London) so the times fire in your local time.
set -e

MODE="${MODE:-schedule}"

case "$MODE" in
    once)
        python main.py
        ;;
    preview)
        python main.py --preview
        ;;
    schedule)
        echo "[agent] starting unified scheduler (RUN_AT=${RUN_AT:-07:00}, TZ=${TZ:-system default})"
        exec python scheduler.py
        ;;
    *)
        echo "[agent] unknown MODE '$MODE' (use schedule, once or preview)" >&2
        exit 1
        ;;
esac
