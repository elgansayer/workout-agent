---
name: scheduler-job
description: 'Add or consolidate a periodic background job (daily coaching run, insight generation, connector polling) instead of adding a third hand-rolled sleep loop. Use when touching docker-entrypoint.sh, insight_scheduler.py, or adding any new recurring job.'
---

# Scheduler Job

## Why This Exists

There are currently **two independent, hand-rolled sleep loops** in the
`agent` container: `docker-entrypoint.sh`'s bash loop (runs `main.py` at
`RUN_AT` times, default `00:00,05:00`) and `insight_scheduler.py`'s Python
`while True` loop (runs `insight_cron.py --daily`/`--weekly` at hardcoded
times), started as a background process by the same entrypoint script. Both
are single-timezone, single-run-time by construction — that breaks the
moment different users need different schedules (`AGENTS.md` §7).

## When to Use

- Adding a new recurring job (e.g. periodic connector re-sync, a weekly
  digest email).
- Touching `docker-entrypoint.sh` or `insight_scheduler.py`.
- Doing the actual consolidation work described below (a good first task
  once multi-tenancy has landed for at least one domain table).

## Target Design (consolidate toward this, don't add a third loop)

One process, one scheduler, iterating per-user run times:

1. Prefer a small, dependency-light approach over pulling in Celery/RQ +
   Redis for a project this size: either (a) a single Python loop that wakes
   every minute, loads all users' `user_preferences.timezone` + a per-user
   run-time preference, and dispatches due jobs, or (b) `APScheduler`
   (`BackgroundScheduler`) if per-job cron-expression scheduling gets
   unwieldy to hand-roll. If you add APScheduler, add it to
   `requirements.txt` and remove the two existing sleep loops in the same
   change — don't run three schedulers at once.
2. Each job (daily coaching run, daily insight header, weekly correlations,
   weekly self-review, weekly check-in) becomes a function taking a
   `user_id`, called once per due user, not a global script assuming a
   single implicit user. `main.py`'s `run(preview)` and `insight_cron.py`'s
   `--daily`/`--weekly` entry points are the functions to adapt — see the
   `multi-tenant-migration` skill for how their DB calls need to change
   first.
3. **Failure isolation between users**: one user's Hevy API being down or
   their AI key being invalid must not stop other users' scheduled runs from
   executing. Wrap each per-user dispatch in a try/except that logs and
   continues, matching the connector-level isolation already required by the
   `connector-integration` skill.
4. Keep `MODE=once`/`MODE=preview` (manual single-run, dry-run) working —
   they're useful for local dev and CI and shouldn't require the full
   scheduler to be running.

## Verification

Run the `verification-gate` skill's steps. Manually verify: starting the
container doesn't spawn more than one long-running scheduler process
(`ps aux` inside the container), and a simulated two-user scenario with
different `timezone` preferences fires each user's job at their own local
time, not the container's `TZ`.

## Gotchas

- Don't schedule anything from inside `webapp/app.py` — that process can run
  as multiple replicas behind a load balancer (per `AGENTS.md` §7); a
  scheduler embedded there would fire the same job N times. Scheduling
  belongs in the `agent` container/process only.
- SQLite's default locking can serialize concurrent writes from many
  per-user jobs running back-to-back; keep individual job transactions short
  rather than holding a connection open across a whole multi-step run.
