# Programme runtime cutover

This is the runtime slice of #976. Web routes, the scheduled worker and AI
explanation code share `programme_runtime.load_programme_runtime`, which reads
only the authenticated tenant's active programme.

## Resolution order

1. The normalised programme domain (#970): immutable revision, routines, blocks
   and materialised prescriptions.
2. The activated Hevy definition while activation clients migrate to the domain.
3. An explicit `setup_required` state.

Paused and completed programmes surface their own state. The runtime never
falls back to the retired static `program.py` split, a template list, or a
fabricated workout.

## Dates, cadence and prescriptions

- Week and block use the user's stored timezone and the programme's real start
  date and requested duration. Legacy definitions without a start date use the
  saved activation timestamp, never a moving "today" fallback.
- Programme end dates come from the active programme, not a global twelve-week
  constant.
- Rolling rotation (`cadence: rolling`/`rotation`) advances only after a
  completed session, so missed days do not skip the next routine. Explicit
  calendar cadence (`cadence: calendar`) advances with elapsed local calendar
  days. Legacy definitions without cadence metadata keep the calendar behaviour.
- Statistics distribution counts completed sessions by selected Hevy routine ID
  where available, falling back to legacy free-text focus only for history that
  predates provider routine IDs.
- Exercise sets and rep ranges are materialised from the current block without
  mutating the saved snapshot.
- Review points come from block boundaries. Programmes without blocks, very
  short programmes and sparse history degrade to a cadence-based review or no
  check-in.

## Surfaces

- `/api/plan` returns programme dates, duration, current block/week, selected
  routine snapshots and materialised prescriptions, or an explicit setup state.
- `/api/dashboard` derives the week, block, progress rings and next routine from
  the same runtime. Health and recovery displays remain optional and separate
  from programme existence.
- The scheduled worker ingests completed Hevy workouts idempotently, reads the
  next due routine from the runtime, and sends setup guidance instead of a
  workout when no programme is active.
- Hevy source synchronisation is read-first. When the explicit
  `HEVY_SYNC_ROUTINES` opt-in is enabled, an active programme is mirrored to
  managed copies in the tenant's dedicated Hevy folder; source routines are
  never overwritten. With the opt-in disabled the scheduler only reads Hevy.
- The coach receives the deterministic prescription and may only explain,
  summarise or format it. Generated prose that drops or rewrites a prescribed
  exercise, set or rep target is rejected in favour of the validated plan.
- Statistics project only programme priority outcomes with sufficient data, use
  the programme end date and label the model, evidence window and uncertainty.

Verification includes synthetic tests for 4/8/12/20/52-week programmes, active,
none, paused and completed states, rolling/timezone boundaries, no-programme
scheduler behaviour, coach validation boundaries, generic projections and
tenant isolation. No live account credentials are needed.
