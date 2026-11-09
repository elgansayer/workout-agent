# Programme activation, week states and adaptation

This document describes the #975 lifecycle slice. It builds on the dynamic
programme engine (#974) and the Hevy source layer (#819).

## Immutable revisions and single ownership

Activation goes through `programme_lifecycle.activate_from_preview`. It:

1. checks the tenant and the source-bound preview token, engine version,
   selected routine order and source hashes;
2. reruns deterministic definition validation (contiguous blocks, full week
   coverage, per-exercise prescriptions for every block);
3. archives the previous active programme in the same transaction;
4. inserts an immutable `programme_revisions` row (`revision_number + 1`,
   `supersedes_revision_id` points at the previous revision);
5. materialises the first dated week and its routine exposures;
6. writes a `programme_audit_events` row and a user notification;
7. fails atomically on validation, stale-source or persistence errors.

A partial unique index enforces at most one active programme per `user_id`.
Revision rows reject `UPDATE` at the database level. New behaviour always
creates another revision; completed week states remain attached to the old
revision.

## Week materialisation and cadence

`materialise_week` is deterministic and idempotent. It derives the week start
from the stored `programme_spec.start_date`, records the user's timezone, and
stores planned exposures by rotation slot.

- `cadence="rolling"` advances the routine rotation but treats planned dates as
  nominal. `next_exposure` returns the earliest planned or partial exposure even
  if its date has passed, so a missed rolling session is never skipped.
- `cadence="calendar"` uses the configured `weekdays` (0 = Monday) and explicit
  local dates.
- `sessions_per_week` controls sessions per week; the rotation offset is derived
  from the revision and week number, never from `today`.

Pause, resume and extension create a new revision. Pausing records
`programme_spec.paused` and the local pause date; the web runtime reports a
paused state without a due workout. Resuming shifts the start date by the pause
length so skipped days do not consume programme weeks. Extension appends a new
validated block and prescription for every exercise.

## Workout ingestion and idempotency

`ingest_workout_event` matches a completed Hevy workout by provider routine ID
(`high`) or by a unique exercise-template signature (`probable`). Ambiguous and
unmatched workouts create a confirmation decision and cannot drive structural
adaptation.

Identical workout payloads are deduplicated by
`(user_id, revision_id, workout_id, payload_hash)`. An updated provider payload
creates a new observation and new decision keys while preserving the previous
decision rows. `delete_workout_event` marks observations as deleted, reopens the
exposure and keeps the decision audit intact.

## Deterministic adaptation

`programme_engine.decide` applies the documented order:

1. hard safety/profile constraint;
2. explicit user override;
3. match confidence;
4. pain/injury/technique feedback;
5. target completion and set type;
6. performance trend;
7. logged RPE;
8. adherence and fresh recovery/fatigue evidence;
9. block envelope;
10. exercise-specific progression rule.

Recovery evidence must be fresh and repeated; one noisy or missing reading
cannot trigger a deload. Reductions happen only after repeated failures and are
followed by a cooldown before re-escalation. A successful escalation requires
consecutive successes (more for probable matches or sparse baselines).
Per-exposure load/set steps and cumulative weekly changes are bounded. Every
decision stores previous/proposed/accepted prescriptions, rationale codes,
observation references, match/baseline confidence, recovery freshness, rule and
engine versions.

A whole-programme fatigue revision is only created as a confirmation-gated
decision; confirming it creates a new immutable revision with reduced sets.
`hold` is always a valid outcome and missing recovery data never forces change.

## User control and drift

Durable `programme_overrides` pin a decision type and prescription for a week
range and are visible through the API; reverting an override keeps its audit
event. Source drift is compared against the immutable active snapshots and never
mutates them. `keep`, `merge`, `rebase` and `replace` all route through
validation; non-keep actions create a new revision and retain the old one. The
integration never writes back to the original Hevy routine.

## API

All routes require the authenticated tenant and never accept a client-supplied
owner ID.

- `POST /api/programmes/preview`, `POST /api/programmes/activate`
- `GET /api/programmes/state`, `/revisions`, `/decisions`, `/overrides`
- `GET /api/programmes/weeks/{week_number}`
- `POST /api/programmes/pause`, `POST /api/programmes/resume`
- `POST /api/programmes/extend`
- `POST /api/programmes/overrides`, `POST /api/programmes/overrides/{id}/revert`
- `POST /api/programmes/decisions/{id}/resolve`
- `GET /api/programmes/drift`, `POST /api/programmes/drift/resolve`
- `POST /api/programmes/workouts/ingest`, `DELETE /api/programmes/workouts/{id}`
