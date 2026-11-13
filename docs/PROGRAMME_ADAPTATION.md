# Programme activation and adaptation

This document is the contract for issue #975: transactional activation, dated
week states, the deterministic adaptation engine, hysteresis, fatigue
management, source drift and rebase, and user control. It builds on the domain
foundation in `docs/PROGRAMME_DOMAIN.md` and the architecture in
`docs/HEVY_NATIVE_DYNAMIC_PROGRAMMES.md`.

Every decision is a pure function of stored inputs. No language model takes
part in any decision, and nothing in this document ever writes to Hevy.

## Module map

| Module | Responsibility |
| --- | --- |
| `programme_service.py` | Verifies a preview and activates it through `ProgrammeService`. |
| `programme_domain/activation.py` | Projects a generated definition onto normalised revision rows and builds week states. |
| `programme_domain/weeks.py` | Pure cadence, week materialisation, exposure classification and timezone helpers. |
| `programme_domain/adaptation.py` | Pure engine: decision order, bounds, hysteresis, fatigue, programme-level evaluation. |
| `programme_domain/review.py` | `AdaptationService`: ingestion, audit, apply, confirm, reject, override, revert, user overrides. |
| `programme_domain/lifecycle.py` | `LifecycleService`: pause, resume, extension, read-only week materialisation. |
| `programme_domain/drift.py` | Exact provider-versus-programme diff and keep / merge / rebase / replace. |
| `programme_review_runner.py` | Matches cached Hevy workouts to the active revision and runs one review. |

## Transactional activation

`POST /api/programmes/activate` rebuilds the preview from the tenant's current
Hevy source and calls `programme_service.activate_programme`, which:

1. rejects an engine version that differs from `dynamic_programme.ENGINE_VERSION`;
2. rejects a presented preview token or per-routine source hash that no longer
   matches (HTTP 409 with the exact changed/added/removed routines);
3. reruns the shared structured validator (`validate_programme_preview` plus
   `programme_validator.validate_programme`);
4. opens one `BEGIN IMMEDIATE` transaction in which it re-reads the tenant's
   *current* profile constraints and training rules and validates again, so a
   rule saved after the preview still gates activation;
5. in that same transaction: persists an immutable revision (the full definition
   in `settings`, normalised routines/blocks/prescriptions, and the activation
   audit in `source_summary.audit`), archives the previous active programme (its
   revisions and decisions are kept), activates the new one, writes the first
   week state, records the `activation` decision, the `authoritative` programme
   decision and the in-app notification, and keeps the legacy
   `programme_start_date` in step.

Any failure rolls the whole transaction back and leaves the previous programme
active. The partial unique index `idx_programmes_one_active_per_user` plus the
write lock mean concurrent activations serialise and leave exactly one active
programme. Ownership is the authenticated `user_id`: routine snapshots are
stamped with it and the repository refuses a snapshot owned by another tenant.

## Week states, cadence and time zones

A week state (`programme_week_states`) holds:

* `planned` — the dated plan from `weeks.plan_week`, a pure function of the
  stored revision definition, the programme start date, the week number and the
  rotation pointer, so any week can be re-derived and compared;
* `actual` — the recorded exposures and their summary: planned, completed,
  partial and substituted exposures, skipped exercises, substitutions,
  unmatched and ambiguous workouts, adherence and the next exposure;
* `adherence`, `fatigue`, `review_state`.

Cadence is read from the stored definition:

| Mode | Behaviour |
| --- | --- |
| `rolling` (first class) | Routines follow the user's rotation order. Only a completed exposure advances the rotation. A passed weekday never skips a routine; its nominal date is reported as `carried_over`. Unmatched and ambiguous workouts never move the rotation. |
| `calendar` (`weekly` in the preview) | Routines sit on explicit dates. A scheduled session that passes without a workout is `missed`; it is never silently rescheduled. |

A session is `completed`, `partial` (a planned exercise skipped, or under 80% of
planned working sets), or `substituted` (a *user-recorded* substitution; a
substitute is never inferred). Dates and weeks always use the tenant's own
`users.timezone` (an unknown value falls back to UTC): a workout at 23:30 UTC is
placed on the user's local calendar day, which can change its programme week.

`GET .../weeks/{n}` is read-only. A stored state is returned as recorded; a
missing one is derived, not written. Reviews and activation persist states.
Later revisions never rewrite a week: a past week shows the state recorded
against the revision that was current when it was lived.

## Pause, resume and extension

* **Pause** records a `pause` event and sets the programme `paused` (never
  deleted). The runtime shows an explicit paused state.
* **Resume** returns a paused programme to `active` (archiving any other active
  programme) and records `resume`. Whole weeks lost to the pause (days // 7)
  extend the programme through a new revision unless `extend` is `false`. The
  rotation continues after the last completed routine.
* **Extend** (1–8 weeks, 52 maximum) appends a revision that lengthens the block
  containing the current week and shifts later blocks; every week stays covered
  exactly once.

## Deterministic adaptation

### Decision order

The engine applies these steps in order; the first that decides wins.

1. hard safety/profile constraints (blocked exercise → substitution *proposal* or
   `block_progression`);
2. explicit user overrides (a pin locks a dimension; nothing later may change it);
3. routine/workout match confidence;
4. pain/injury/technique feedback;
5. target completion and set type (warm-ups never count);
6. performance trend (streaks since the last applied change);
7. logged RPE/effort against the target RIR;
8. adherence and recovery evidence;
9. the block target envelope;
10. the exercise-specific progression rule and modality.

### Outcomes

| Outcome | Applied as |
| --- | --- |
| `progress_load`, `progress_reps` (within the rep range), `progress_duration_distance`, `progress_bodyweight_variable` (added load, less assistance; unquantifiable variables need confirmation), `reduce_load`, `exercise_fatigue_management` | week-level adjustment (reversible, folded over the immutable revision) |
| `reduce_sets`, `alter_rep_range`, `programme_fatigue_management` | structural: appends a new revision after the shared validator accepts it |
| `propose_substitution`, `request_confirmation` | pending; nothing changes until the user acts |
| `block_progression`, `hold` | no change; `hold` is a valid outcome with recorded reasons |

A user time limit that caps sets is an explicit instruction and is applied as a
week-level `reduce_sets`.

Bodyweight, assisted, duration and distance exercises use modality-specific
fields and never run load/e1RM logic.

### Confidence and bounds

| Evidence | Behaviour |
| --- | --- |
| High match (or user-confirmed) | Normal configured bounds. |
| Probable match | Load steps halved, reversible changes only; a structural change needs confirmation. |
| Ambiguous or unmatched | `request_confirmation`; no change. Unmatched workouts create no per-exercise decisions and are tracked in the week state. |
| Sparse baseline (< 3 known exposures) | One extra successful exposure required and steps halved. |
| Recovery data | Optional, with quality and age. Only fresh (≤ 36 h), medium/high-quality evidence with at least two concordant signals can *defer* a progression; it never reduces a prescription, and a single signal is recorded as insufficient. Missing or stale data is ignored. |

Per-exposure bounds: load changes ≤ the prescription's `max_load_percent`
(default 5%), rounded **down** to the equipment increment; if one increment
exceeds the bound the engine holds or widens the rep range instead. Per-week
bounds: cumulative load change ≤ 5% and set change ≤ 1 per exercise.

### Hysteresis and anti-oscillation

* escalation needs consecutive successes since the last applied change (2, or 3
  for conservative primary lifts, +1 for a sparse baseline);
* a reduction needs 2 consecutive failures; one miss after a progression holds;
* after a reduction a cooldown of 2 exposures blocks both directions, and
  re-escalation needs a stable run of successes after it;
* fatigue management has separate thresholds: enter after a reduction that did
  not help (or 3 failures in the last 4), exit after 2 consecutive met sessions;
* a user rejection or revert restarts the streak so the same proposal cannot be
  re-issued on the next exposure;
* replaying the same event is idempotent (see below).

`tests/test_programme_adaptation_engine.py` property-tests determinism, the
bounds and the no-oscillation rules over generated exposure sequences.

### Fatigue management, deload and taper

A deload is an available response, never a mandatory fourth week. Exercise-level
management temporarily cuts work (~30%) and is removed on exit.
Programme-level management (`programme_fatigue_management`) needs breadth
(≥ 3 exercises and ≥ 50% of those evaluated in trouble, across ≥ 2 sessions)
**and** independent corroboration (a user fatigue report, adherence below 60%, or
fresh corroborating recovery evidence). Without corroboration the outcome is a
`request_confirmation`. One poor session or one noisy wearable reading can never
trigger it, and it respects a 3-week cooldown. Modes are `volume_only`,
`load_plus_volume` and `frequency`; the weeks, exit criteria and trigger evidence
are stored in `fatigue_periods` on the new revision. A programme with
`fatigue_management = "planned"` always asks first. A true taper exists only as a
generated block for a dated test goal (#974).

## Audit and revisioning

Every evaluation writes an `adaptation_decisions` row (including holds):

| Required record | Stored in |
| --- | --- |
| user/programme/revision/week/routine/exercise | `scope_json`, `week_number`, `exercise_template_id` |
| raw observation references | `inputs_json.observation` (workout id, evidence hash, sets), `source_workout_id` |
| match and baseline confidence | `inputs_json.match`, `inputs_json.baseline` |
| constraints and recovery freshness | `inputs_json.constraints`, `inputs_json.recovery` |
| rule/engine version | `rule_version` (`programme-adaptation/v1`) |
| previous / proposed / accepted prescription | `output_json.previous_prescription`, `.proposed_prescription`, `.accepted_prescription` |
| decision type and rationale codes | `decision_type`, `output_json.rationale_codes` |
| applied / confirmed / overridden / reverted | `application_state` (`auto_applied`, `pending_confirmation`, `user_confirmed`, `overridden`, `rejected`, `reverted`, `superseded`, `informational`, `acknowledged`), `resolved_at` |
| from/to revision | `from_revision_id`, `to_revision_id` (equal when no revision was created) |

The explanation shown to the user is rebuilt from the stored rationale codes
(`adaptation.explain`); no model conversation is replayed. Lifecycle events
(`activation`, `pause`, `resume`, `extension`, `revert`, `drift_*`) use the same
table, so one stream explains the whole programme.

Activated revisions are immutable (database triggers). A structural change
rewrites the **current block and later blocks** only; earlier blocks and every
stored week state stay as recorded.

### Replay, edits and deletions

The idempotency key is `sha256(user | programme | workout | evidence | exercise |
routine | rule version)`, where *evidence* covers the workout content and the
match. An identical replay writes nothing. An edited workout supersedes its
earlier decisions and is re-evaluated. A deleted workout (`retract_workouts`, or a
workout missing from a complete provider listing) is retracted: its week-level
effects disappear and its exposure leaves the week state. An immutable revision
it caused is never withdrawn silently; a pending review asks whether to revert it.

## User control

Overrides are durable rows in `programme_overrides`, scoped to one programme
(and usually one exercise), visible, time bounded and retained after revocation.

| Kind | Effect |
| --- | --- |
| `pin` (`load`, `sets`, `reps`; default 4 weeks) | the engine never changes the pinned dimension |
| `pain_report`, `technique_report` | progression for that exercise is blocked until revoked |
| `equipment_issue` | blocks the exercise and proposes (never applies) a substitution |
| `time_limit` (`max_sets`) | caps sets for the period |
| `substitution` | records an intentional substitution so the session counts as substituted |
| `fatigue_report` | corroborates programme-level fatigue |
| `review_request` | records an early-review request |

Decisions can be confirmed (optionally with a bounded correction), rejected,
overridden (a bounded correction of an applied load/rep value → `overridden`) or
reverted. Reverting a structural decision restores the earlier revision's content
as a *new* revision. Ambiguous matches are confirmed or corrected with
`POST .../workout-matches/confirm`, which records a user-confirmed match and
re-evaluates the workout.

## Source drift and rebase

`GET .../drift` compares the active snapshots with the live Hevy routines and
returns, per routine, a state (`unchanged`, `renamed`, `compatible_edit`,
`structural_edit`, `deleted`) and an exact change list: `routine_renamed`,
`renamed`, `reordered`, `added`, `removed`, `sets_changed`, `targets_changed`,
with an `unsupported` flag for exercises the engine cannot interpret. Exercises
are identified by template id and occurrence, never by title. Detection never
changes the programme.

`POST .../drift/resolve` takes one decision per routine:

* `keep` — acknowledge; no revision, the snapshot stays;
* `merge` — adopt the listed change ids only (residual drift stays visible);
* `rebase` — adopt every change; changed or added exercises get fresh, validated
  prescriptions at the provider's sets, others keep their overlay;
* `replace` — swap the routine (also for a deleted routine) keeping its rotation
  position.

The reconciled definition must pass the structured validator before a new
revision is appended; the old revision is preserved and the decision records the
from/to revisions. A stale `expected_hash` returns 409; unsupported exercises need
explicit acknowledgement. Preview-time planning aggregates (`workload`,
`muscle_sets`) are not recomputed by a reconciliation.

## API

All routes derive the owner from the session, forbid unknown request fields and
return 404 for another tenant's records.

| Route | Purpose |
| --- | --- |
| `GET /api/programmes/active/state` | programme, current week, pending decisions, overrides, revisions |
| `GET /api/programmes/{id}/weeks/{week}` | dated week with effective prescriptions (read-only) |
| `GET /api/programmes/{id}/next-session` | next exposure for the current week |
| `POST /api/programmes/{id}/pause` · `/resume` · `/extend` | lifecycle |
| `POST /api/programmes/{id}/review` | run the review now (early review) |
| `GET /api/programmes/{id}/decisions[?state&decision_type&exercise]` · `/{decision_id}` | audit and "why" |
| `POST .../decisions/{id}/confirm` · `/reject` · `/revert` · `/override` | decision control |
| `GET/POST /api/programmes/{id}/overrides` · `POST .../overrides/{id}/revoke` | durable overrides |
| `POST /api/programmes/{id}/workout-matches/confirm` | confirm or correct a match |
| `GET /api/programmes/{id}/drift` · `POST .../drift/resolve` | drift diff and reconciliation |

## Operation

The daily agent runs `programme_review_runner.run_review` before it loads the
runtime. It reads the tenant's locally cached workouts, matches them to the
active revision's snapshots and replays safely. A failure is logged and never
blocks the plan. `programme_overrides` is a user-owned table registered for
tenant isolation, ownership migration and account deletion.

## Known limits

* There is no Angular UI for these controls yet; the API is the delivered surface.
* The Hevy cache has no delete-event feed. Deletions are applied through
  `retract_workouts` / `deleted_workout_ids` or a complete `known_workout_ids` list.
* Recovery evidence is an optional input; it is not yet fed from a health
  connector.
* The engine never adds sets automatically; planned volume comes from the block
  allocation.
