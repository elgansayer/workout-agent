---
name: scheduler-job
description: 'Add a new periodic background job (daily coaching run, insight generation, connector polling) to the unified scheduler.py. Use when adding any new recurring job dispatch from the scheduler loop.'
---

# Scheduler Job

## Why This Exists

The agent container runs a single unified scheduler (`scheduler.py`) that
wakes every 60 seconds, checks each user's local time against the configured
`RUN_AT` times, and dispatches due coaching runs (`main.py`) and insight
jobs (`insight_cron.py --daily`/`--weekly`). Each per-user dispatch is
isolated so one user's failures do not block another's. All scheduling is
consolidated into this one process — do not add additional sleep loops.

## When to Use

- Adding a new recurring job (e.g. periodic connector re-sync, a weekly
  digest email).
- Touching the dispatch logic in `scheduler.py`.

## How to Add a New Job

The unified `scheduler.py` already handles the main loop. To add a new
recurring job:

1. Add a new dispatch function in `scheduler.py` (e.g. `_run_connector_sync()`)
   following the existing pattern of `_run_coaching()` / `_run_insight_job()`.
2. If the job is per-user, accept a `user_id` parameter and wrap each user
   dispatch in a try/except for failure isolation.
3. Wire it into `run_scheduler()`'s main loop at the appropriate cadence
   (daily, weekly, per-run-time, etc.).
4. **Failure isolation between users**: one user's Hevy API being down or
   their AI key being invalid must not stop other users' scheduled runs from
   executing. Wrap each per-user dispatch in a try/except that logs and
   continues, matching the connector-level isolation already required by the
   `connector-integration` skill.
5. Keep `MODE=once`/`MODE=preview` (manual single-run, dry-run) working —
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
