#!/bin/sh
# Entry point for the containerised workout agent.
#
# MODE controls behaviour:
#   schedule (default) - run every day at RUN_AT (local time), forever.
#   once               - run a single live cycle and exit.
#   preview            - print today's plan to stdout and exit (sends nothing).
#
# RUN_AT is a 24-hour HH:MM time, defaulting to 07:00. Set TZ (e.g.
# Europe/London) so it fires in your local time.
set -e

RUN_AT="${RUN_AT:-07:00}"
MODE="${MODE:-schedule}"

run_once() {
    echo "[agent] $(date '+%Y-%m-%d %H:%M:%S') running daily cycle"
    python main.py || echo "[agent] run failed, will try again tomorrow"
}

case "$MODE" in
    once)
        python main.py
        ;;
    preview)
        python main.py --preview
        ;;
    schedule)
        echo "[agent] scheduled for ${RUN_AT} daily (TZ=${TZ:-system default})"
        while true; do
            now=$(date +%s)
            target=$(date -d "today ${RUN_AT}" +%s)
            if [ "$target" -le "$now" ]; then
                target=$(date -d "tomorrow ${RUN_AT}" +%s)
            fi
            wait_secs=$((target - now))
            echo "[agent] sleeping ${wait_secs}s until next run at ${RUN_AT}"
            sleep "$wait_secs"
            run_once
        done
        ;;
    *)
        echo "[agent] unknown MODE '$MODE' (use schedule, once or preview)" >&2
        exit 1
        ;;
esac
