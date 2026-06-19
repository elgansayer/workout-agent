#!/bin/sh
# Entry point for the containerised workout agent.
#
# MODE controls behaviour:
#   schedule (default) - run every day at RUN_AT (local time), forever.
#   once               - run a single live cycle and exit.
#   preview            - print today's plan to stdout and exit (sends nothing).
#
# RUN_AT is one or more 24-hour HH:MM times, comma or space separated,
# defaulting to 07:00. For example RUN_AT="00:00,05:00" runs at midnight and
# 5am. Set TZ (e.g. Europe/London) so the times fire in your local time.
set -e

RUN_AT="${RUN_AT:-07:00}"
MODE="${MODE:-schedule}"

run_once() {
    echo "[agent] $(date '+%Y-%m-%d %H:%M:%S') running scheduled cycle"
    python main.py || echo "[agent] run failed, will retry at the next scheduled time"
}

case "$MODE" in
    once)
        python main.py
        ;;
    preview)
        python main.py --preview
        ;;
    schedule)
        python insight_scheduler.py &

        # RUN_AT may list several HH:MM times (comma or space separated),
        # e.g. "00:00,05:00" to run at midnight and 5am.
        times=$(echo "$RUN_AT" | tr ',' ' ')
        echo "[agent] scheduled for ${RUN_AT} daily (TZ=${TZ:-system default})"
        while true; do
            now=$(date +%s)
            next=""
            for t in $times; do
                target=$(date -d "today ${t}" +%s)
                if [ "$target" -le "$now" ]; then
                    target=$(date -d "tomorrow ${t}" +%s)
                fi
                if [ -z "$next" ] || [ "$target" -lt "$next" ]; then
                    next=$target
                fi
            done
            wait_secs=$((next - now))
            echo "[agent] sleeping ${wait_secs}s until next run ($(date -d "@${next}" '+%Y-%m-%d %H:%M'))"
            sleep "$wait_secs"
            run_once
        done
        ;;
    *)
        echo "[agent] unknown MODE '$MODE' (use schedule, once or preview)" >&2
        exit 1
        ;;
esac
