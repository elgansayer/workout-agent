# Hevy-First Dynamic Programme Architecture

**Status:** proposed architecture and delivery plan  
**Decision date:** 2026-08-19  
**Supersedes:** the static `Hybrid Powerbuilding` product model and every static-programme fallback  
**Related issues:** #819, #804, #899

## 1. Decision

Workout Agent will no longer offer, seed, select, preview, schedule, coach, or fall back to a built-in `Hybrid Powerbuilding` programme.

The only supported programme creation flow is:

1. Connect a Hevy account.
2. Synchronise the user's Hevy folders, routines, exercises, set targets, and completed workouts.
3. Let the user select and order the Hevy routines that belong to the programme.
4. Ask for programme intent and constraints, including duration.
5. Generate a versioned programme around those routine snapshots.
6. Preview the selected Hevy routines, blocks, weekly prescriptions, assumptions, and warnings.
7. Activate the generated programme.
8. Adapt future prescriptions from completed Hevy workouts and recovery data without silently changing the original imported routines.

Hevy routines define **what the user trains**. Workout Agent defines **how that training is progressed over time**.

There will be no alternative static template path, no hidden six-day fallback, and no hard-coded assumption that all users are powerbuilders, are training for a physique stage, prioritise deadlifts and weighted pull-ups, train Monday through Saturday, or need a twelve-week accumulation/intensification/peaking cycle.

## 2. Product principles

### 2.1 Hevy is the source topology, not the complete programme

A Hevy routine is a reusable workout template. It contains ordered exercises, set types and targets, rest periods, notes, supersets, and provider metadata. A collection of routines, often grouped in a Hevy folder, describes the user's preferred training split.

That is not enough to describe a multi-week programme. A programme also needs:

- a goal and priority hierarchy;
- a duration and start date;
- selected routines and their order or cadence;
- exercise roles;
- weekly volume and intensity targets;
- progression rules;
- block or phase boundaries where they are useful;
- fatigue-management policy;
- success and exit criteria;
- adaptations made after each exposure;
- a durable audit trail.

Workout Agent must preserve the imported routine while adding this programme layer.

### 2.2 Selection is explicit

Programme generation must not automatically use every routine found in a Hevy account. Users often retain old routines, experiments, travel routines, warm-up templates, duplicate routines, and routines belonging to other goals.

The builder must present the Hevy routine library grouped by folder and let the user:

- select or deselect routines;
- inspect every routine before selection;
- reorder selected routines;
- define a simple repeating order or a calendar cadence;
- choose whether a selected routine is primary, optional, or rotating;
- resolve duplicates and unsupported exercises;
- refresh from Hevy without losing the current selection.

### 2.3 Imported routines are immutable snapshots inside an active revision

Hevy users can edit routines at any time. An active programme cannot silently change because a routine was renamed, reordered, or updated in Hevy.

At generation time, Workout Agent stores a canonical snapshot and content hash for every selected routine. A later Hevy sync can report drift, but the active programme continues to use its versioned snapshot until the user chooses one of:

- **Keep programme version:** ignore the provider change for this programme.
- **Rebase:** apply compatible provider changes and generate a new programme revision.
- **Compare and merge:** review exercise-level differences.
- **Replace routine:** substitute another Hevy routine and revalidate the programme.

### 2.4 Deterministic planning, explainable AI

Core programme calculations must be deterministic, testable, and versioned. An LLM may explain a recommendation, summarise evidence, or help the user express goals. It must not be the only implementation of progression, fatigue management, set allocation, activation validation, or exercise matching.

Every prescription and adaptation must be reproducible from:

- source routine snapshot;
- programme settings;
- historical observations;
- rule/engine version;
- user overrides;
- health/recovery inputs that were actually available.

### 2.5 Blocks are useful control envelopes, not fitness mythology

The product should support blocks, but it must not impose the same labels or structure on every goal.

A block is a period in which a defined set of programme variables and decision rules remain within a target envelope. It may control:

- weekly hard-set range;
- relative volume multiplier;
- rep-range distribution;
- load or estimated-intensity distribution;
- target RIR/RPE;
- exercise specificity;
- progression aggressiveness;
- fatigue-management triggers;
- exercise variation policy;
- test or taper intent;
- block exit criteria.

A block is not automatically four weeks, and every programme does not require accumulation, intensification, and peaking.

## 3. Why the current implementation must be replaced

The current repository has already begun a read-first Hevy integration, but it still treats Hevy inference as an optional programme template layered on top of the static product.

### 3.1 Static programme coupling

`backend/program.py` currently defines:

- the product name;
- a fixed twelve-week duration;
- fixed four-week blocks;
- a six-day split and Sunday rest day;
- hard-coded exercise selection and Hevy exercise IDs;
- deadlift and weighted-pull-up special cases;
- progression rules;
- lifestyle rules;
- personal goals and personal constraints.

The same assumptions are imported by:

- `backend/database.py`;
- `backend/main.py`;
- `backend/gemini_engine.py`;
- `backend/checkin.py`;
- `backend/lifestyle.py`;
- `backend/hevy_sync.py`;
- `backend/webapp/app.py`;
- tests, documentation, and migration helpers.

The Angular programme page then presents the static template beside “Infer from Hevy” and renders an “Active Split Preview”. Removing one card is therefore insufficient. The canonical runtime, scheduler, coaching prompts, fallback plan, statistics, check-ins, projections, and database seed all need to consume the same dynamic programme model before `program.py` can be deleted.

### 3.2 The existing inferred definition is only a display adapter

The current inference path:

- fetches all Hevy routines;
- fetches only a small recent-workout sample;
- classifies a rough split;
- copies routine exercises into a static-compatible JSON shape;
- creates one synthetic four-week block named `Inferred`;
- does not accept selected routine IDs;
- does not accept programme duration or goal;
- does not create real progression prescriptions;
- does not preserve per-set targets, distance, duration, RPE, or custom metrics;
- does not provide routine-match confidence;
- does not version source snapshots or adaptation decisions.

It is a useful discovery layer, not a programme engine.

### 3.3 The existing database model is too coarse

The `programmes` table stores one mutable JSON definition with an `active` flag. It has no first-class records for:

- routine snapshots;
- selected-routine order;
- programme revisions;
- duration and dated weeks;
- blocks;
- exercise roles;
- weekly prescriptions;
- adaptation decisions;
- source drift;
- rule-engine version;
- match confidence;
- user overrides.

These must become durable, tenant-scoped domain entities rather than nested anonymous JSON that is overwritten in place.

## 4. Hevy integration constraints

### 4.1 Data available from Hevy

The public Hevy API exposes the data needed for a strong source layer:

- routine folders and ordering;
- routines and their `updated_at` values;
- ordered routine exercises;
- exercise template IDs and muscle metadata;
- rest periods, notes, supersets, and set types;
- weight, reps, distance, duration, RPE, and custom metrics;
- completed workout creation and update timestamps;
- workout update/delete events for incremental synchronisation;
- per-exercise history.

The current connector should be extended rather than replaced.

### 4.2 Completed workouts do not identify their source routine

The public workout schema does not expose a `routine_id`. A completed workout therefore cannot always be joined directly to an imported routine.

Workout Agent needs an explicit matching subsystem. It must never pretend a guessed match is certain.

### 4.3 Provider instability and failure handling

The Hevy API is a Pro feature and its public documentation describes it as an early interface. The connector must therefore:

- keep raw provider payloads;
- ignore unknown fields safely;
- preserve fields it does not yet interpret;
- use bounded retries with backoff for transient failures;
- support pagination correctly;
- use workout events for incremental updates;
- be idempotent;
- record provider cursors and sync checkpoints per user;
- distinguish “empty account” from “provider unavailable”;
- expose last successful sync and stale-data warnings;
- maintain contract fixtures from observed payloads.

## 5. Target domain model

Every table below is tenant scoped. Every query, unique constraint, foreign key, migration, and test must include `user_id` where appropriate.

### 5.1 Source layer

#### `hevy_sync_state`

- `user_id`
- `resource`
- `cursor` or event checkpoint
- `last_attempt_at`
- `last_success_at`
- `last_error_code`
- `last_error_summary`

#### `hevy_routine_snapshots`

- `id`
- `user_id`
- `hevy_routine_id`
- `hevy_folder_id`
- `title`
- `provider_updated_at`
- `content_hash`
- `canonical_json`
- `raw_json`
- `synced_at`
- `is_current`
- unique current snapshot per `(user_id, hevy_routine_id)`

#### `hevy_workouts`

- `id`
- `user_id`
- `hevy_workout_id`
- `title`
- `started_at`
- `ended_at`
- `provider_updated_at`
- `canonical_json`
- `raw_json`
- `deleted_at`
- `synced_at`

#### `hevy_exercise_templates`

- `user_id`
- `hevy_template_id`
- `title`
- `exercise_type`
- `primary_muscle_group`
- `secondary_muscle_groups_json`
- `is_custom`
- `canonical_json`
- `provider_updated_at`

### 5.2 Programme definition layer

#### `programmes`

- `id`
- `user_id`
- `name`
- `status`: `draft | active | paused | completed | archived`
- `goal`
- `duration_weeks`
- `start_date`
- `end_date`
- `current_revision_id`
- `created_at`
- `updated_at`

There is deliberately no `template_key` and no built-in template source.

#### `programme_revisions`

- `id`
- `user_id`
- `programme_id`
- `revision_number`
- `reason`: `generated | user_edit | adaptation | routine_rebase | migration`
- `engine_version`
- `settings_json`
- `source_summary_json`
- `created_at`
- unique `(user_id, programme_id, revision_number)`

A revision is immutable after activation. Changes create a new revision.

#### `programme_routines`

- `id`
- `user_id`
- `programme_revision_id`
- `routine_snapshot_id`
- `position`
- `participation`: `required | optional | rotating`
- `cadence_json`
- `user_label`
- unique `(user_id, programme_revision_id, position)`

#### `programme_blocks`

- `id`
- `user_id`
- `programme_revision_id`
- `position`
- `kind`
- `name`
- `start_week`
- `end_week`
- `objective`
- `targets_json`
- `adaptation_policy_json`
- `exit_criteria_json`

#### `exercise_prescriptions`

- `id`
- `user_id`
- `programme_revision_id`
- `routine_snapshot_id`
- `hevy_template_id`
- `exercise_role`
- `priority_rank`
- `prescription_model`
- `rep_range_json`
- `set_target_json`
- `effort_target_json`
- `load_rule_json`
- `rest_rule_json`
- `progression_rule_json`
- `substitution_policy_json`

#### `programme_week_states`

- `user_id`
- `programme_id`
- `week_number`
- `revision_id`
- `status`
- `planned_json`
- `actual_json`
- `adherence_json`
- `fatigue_json`
- `reviewed_at`

#### `adaptation_decisions`

- `id`
- `user_id`
- `programme_id`
- `revision_from_id`
- `revision_to_id`
- `week_number`
- `routine_snapshot_id`
- `hevy_template_id`
- `decision_type`
- `inputs_json`
- `decision_json`
- `confidence`
- `rule_version`
- `created_at`

#### `routine_workout_matches`

- `user_id`
- `hevy_workout_id`
- `routine_snapshot_id`
- `match_method`
- `score`
- `confidence_band`
- `features_json`
- `confirmed_by_user`
- `created_at`

## 6. Builder experience

The programme page becomes one Hevy-first builder. It no longer has an “Available Templates” section.

### Step 1: Hevy state

Show:

- connected account;
- subscription/API availability;
- last successful sync;
- stale or partial data warning;
- folders, routines, workouts, and exercise-template counts;
- `Refresh from Hevy`;
- setup CTA when no key or no routines exist.

A user with no active programme sees this setup state. The app must never fall back to Hybrid Powerbuilding.

### Step 2: Select Hevy routines

Present routines grouped by Hevy folder.

Each routine card shows:

- selection checkbox;
- title and folder;
- provider update time;
- number of exercises and work sets;
- estimated duration;
- primary muscle distribution;
- unsupported or ambiguous fields;
- expand/collapse preview.

The full preview preserves exact provider structure:

- exercise order;
- exercise names and IDs;
- set order and type;
- weight/reps/distance/duration targets;
- RPE where present;
- rest period;
- notes;
- supersets.

Selected routines appear in a separate ordered list. The user can drag them into programme order and mark a routine as required, optional, or rotating.

### Step 3: Programme settings

Required inputs:

- duration, initially constrained to 4–52 weeks;
- primary goal;
- start date;
- selected routine order.

Recommended inputs:

- priority muscles or exercises;
- experience level;
- desired progression aggressiveness;
- typical sessions per week;
- available weekdays or “rolling order”;
- maximum session duration;
- whether a strength test or event date exists;
- injuries, movement exclusions, equipment constraints;
- concurrent sport/cardio commitments;
- preference for planned fatigue-management weeks;
- minimum acceptable adherence.

Goals should be structured, not a free-text label alone:

- general strength and fitness;
- hypertrophy;
- maximal strength;
- strength with test/competition date;
- recomposition or energy deficit;
- maintenance;
- return-to-training;
- power/performance.

Free text can supplement these fields but cannot replace validation.

### Step 4: Generated preview

Before activation show:

- programme name, dates, and duration;
- selected Hevy routine snapshots;
- inferred split and routine cadence;
- exercise-role assignments;
- current weekly muscle-set estimates;
- identified priorities, imbalances, duplicates, and recovery risks;
- block timeline;
- sample prescription for every routine in every block;
- expected progression mechanism by exercise;
- planned review and fatigue-management points;
- assumptions and confidence;
- blocking validation errors;
- non-blocking warnings;
- source snapshot hashes and sync time.

Replace “Active Split Preview” with **Selected Hevy Routine Preview** and **Generated Programme Preview**.

### Step 5: Activate

Activation is transactional:

1. Reconfirm the current source hashes.
2. Run the shared constraints validator.
3. Persist the draft revision.
4. Mark it active.
5. Archive or pause the previous active programme.
6. Create the first week state.
7. Emit an audit event and notification.

## 7. Programme-generation pipeline

### Stage A: synchronise and canonicalise

- Fetch all routines, folders, and exercise templates.
- Fetch historical workouts far enough back to estimate baselines. Prefer an incremental local cache over an arbitrary “last 15 workouts” request.
- Preserve per-set fields and provider order.
- Calculate stable content hashes from canonical JSON.
- Record sync freshness and failures.

### Stage B: validate the selected source

For selected routines:

- reject missing/deleted IDs;
- flag empty routines;
- identify duplicate exercise IDs;
- identify unsupported exercise types;
- estimate muscle coverage;
- estimate session duration;
- detect routines whose volume is already implausibly high;
- detect selected routines that are nearly identical;
- ensure the intended cadence is feasible within the chosen duration.

Warnings should not silently rewrite the source. The user sees and accepts or resolves them.

### Stage C: match history to routines

Because completed workouts do not expose a routine ID, compute a match score.

Suggested features:

| Feature | Weight |
| --- | ---: |
| weighted Jaccard overlap of exercise-template IDs | 0.35 |
| ordered-sequence similarity | 0.20 |
| title similarity | 0.15 |
| set-count/structure similarity | 0.10 |
| superset similarity | 0.05 |
| expected position in the routine cycle | 0.10 |
| recency to a known routine version | 0.05 |

Use explicit thresholds:

- `>= 0.85`: high-confidence automatic match;
- `0.65–0.84`: probable match, usable with reduced confidence;
- `0.45–0.64`: ambiguous, ask the user or omit from routine-specific adaptation;
- `< 0.45`: unmatched workout.

The thresholds must be calibrated against fixtures, not treated as permanent constants. A user correction creates a durable override and training example.

### Stage D: infer baselines

For each exercise:

- separate warm-up, normal, drop, and failure sets;
- use the exercise type;
- derive recent successful working-set distributions;
- calculate robust estimated strength where applicable;
- calculate recent rep, load, and set trends;
- calculate median and variability of logged RPE;
- calculate exposure frequency and days between exposures;
- identify the equipment increment from observed loads or user settings;
- measure adherence to the routine structure;
- down-weight stale or low-confidence matches.

Do not use a single historical maximum as the baseline. Prefer robust recent values such as a weighted median of successful exposures, with outlier rejection and an explicit confidence band.

### Stage E: classify exercise roles

Each selected exercise receives a role based on goal, routine position, exercise type, muscle contribution, and user priorities:

- primary strength;
- secondary compound;
- hypertrophy/accessory;
- power/skill;
- bodyweight progression;
- duration/distance;
- rehabilitation/prehabilitation;
- optional conditioning.

The same exercise can have a different role in different programmes. No exercise ID is globally “the main lift”.

### Stage F: establish the viable workload

Estimate current weekly sets per muscle from selected routines and historical completion.

The model should distinguish:

- direct sets;
- secondary contribution;
- warm-up sets;
- very low-effort sets;
- drop/failure sets;
- incomplete sets.

This estimate is a planning aid, not an assertion of exact biological stimulus. Display the uncertainty and allow a user override for custom exercises.

Set the opening workload from what the user has recently tolerated, bounded by:

- goal profile;
- experience;
- duration;
- session-time limits;
- selected routine structure;
- constraints;
- recovery and adherence;
- minimum and maximum change from recent training.

A generated programme should generally alter workload gradually rather than replacing the user's routine with a radically different first week.

### Stage G: allocate goal-specific blocks

The allocator uses duration, goal, training history, and optional test date. It does not simply divide every programme into three equal parts.

#### General strength and fitness

Typical intents:

- establish repeatable baseline;
- progress major movement patterns;
- maintain balanced weekly exposure;
- consolidate and review.

Peaking is absent unless the user requests a test.

#### Hypertrophy

Typical intents:

- calibrate opening volume where data is weak;
- accumulate productive volume;
- progress reps/load while managing proximity to failure;
- insert a planned or reactive fatigue-management period when needed;
- consolidate and review.

There is no required intensification or peak.

#### Maximal strength

Typical intents:

- establish technical and workload base;
- increase specificity and heavy exposure;
- retain enough volume to support progress;
- optionally realise/test strength.

A taper/test phase exists only when the user has a test or event objective.

#### Recomposition or energy deficit

Typical intents:

- preserve high-quality strength exposure;
- avoid unnecessary volume escalation;
- use adherence and recovery to cap workload;
- prioritise sustainable progression;
- reassess more frequently.

The engine must not label a block “shredding” or prescribe nutrition from a training-template constant.

#### Maintenance

Typical intents:

- retain performance with the minimum sustainable dose;
- preserve user-preferred routines;
- use sparse reviews and low-volatility prescriptions.

#### Return to training

Typical intents:

- conservative calibration;
- gradual exposure and volume ramp;
- strict constraint validation;
- greater use of holds than load increases;
- no forced catch-up to old personal records.

#### Power/performance

Typical intents:

- preserve movement velocity and quality;
- use appropriate load and low-to-moderate volume for power work;
- coordinate fatigue with sport practice;
- avoid hypertrophy assumptions for every exercise.

### Stage H: create per-exercise prescriptions

Prescriptions are generated by exercise role and exercise type.

#### Hypertrophy/accessory: double progression

Example rule:

1. Preserve the selected routine's set structure unless the block explicitly changes volume.
2. Choose a rep range from source targets and recent history.
3. Use a target effort band, commonly leaving repetitions in reserve rather than mandating failure.
4. When all qualifying work sets reach the top of the range within the effort band for the configured number of exposures, increase load by the available increment.
5. If the load increment is too large, progress reps, set quality, or range of motion before load.
6. Repeated misses trigger a hold, load reduction, set reduction, or substitution review.

#### Primary strength: exposure plus back-off model

Possible models include:

- top set at target RPE plus percentage/e1RM-derived back-offs;
- fixed rep range with autoregulated load;
- double progression where data is sparse.

Selection depends on the user's goal, experience, source routine, and available RPE data.

The system should not invent a top-set model when the user has deliberately selected a different routine structure without showing that transformation in preview.

#### Bodyweight

Progression order can be configured:

- reps;
- added load;
- harder variation;
- tempo/range;
- set count.

#### Duration/distance

Use domain-specific targets:

- time;
- distance;
- pace;
- resistance/level;
- work-to-rest ratio.

Do not compute `weight × reps` volume for every exercise type.

### Stage I: generate blocks, weeks, and review points

A block sets target envelopes. A week state materialises the actual prescriptions for that week.

The engine must be able to:

- shorten or extend a block within the programme duration;
- create a review-only transition without inventing a deload;
- insert reactive fatigue management;
- preserve calendar dates when a test date is fixed;
- support a rolling routine order rather than requiring Monday–Sunday mapping;
- handle missed sessions without automatically skipping the workout.

### Stage J: validate and explain

Run one shared validator during preview, creation, activation, rebase, edit, and adaptation.

The result contains:

- blocking violations;
- warnings;
- assumptions;
- confidence;
- exact affected routine/exercise/week;
- remediation options.

This integrates with #804 and #899.

## 8. Duration and block allocation

Programme duration is a user choice, but duration alone should not dictate a scientifically false block structure.

### 8.1 Duration rules

- Minimum: 4 weeks.
- Maximum initial product limit: 52 weeks.
- A programme can be extended through a new revision.
- The final week is not automatically a peak.
- Very short programmes should have fewer blocks.
- Long programmes should include review gates and repeated development waves rather than one twelve-month linear ramp.

### 8.2 Example defaults

These are product defaults, not universal physiological truths.

#### Twelve-week hypertrophy

- Week 1: calibration where history is incomplete.
- Weeks 2–5: development.
- Week 6: fatigue-management/review candidate, retained only when indicated or selected.
- Weeks 7–10: development with revised baselines.
- Weeks 11–12: consolidation and outcome review.

#### Twelve-week strength without a test date

- Week 1: calibration.
- Weeks 2–5: volume and technical base.
- Weeks 6–9: strength-specific development.
- Weeks 10–11: consolidation.
- Week 12: review and next-cycle decision.

#### Twelve-week strength with a fixed test date

- Week 1: calibration.
- Weeks 2–4: base.
- Weeks 5–8: specific strength.
- Weeks 9–10: realisation.
- Weeks 11–12: taper and test, with exact dates driven by the event.

#### Twelve-week recomposition

- Week 1: calibration.
- Weeks 2–5: sustainable development.
- Week 6: review and fatigue decision.
- Weeks 7–10: development or maintenance according to recovery.
- Weeks 11–12: consolidation and reassessment.

#### Four-to-six-week programme

Use one development block plus calibration/review as necessary. Do not fabricate three named phases merely to fill a timeline.

### 8.3 Allocation algorithm

A practical first allocator can be deterministic:

```text
reserve final review week when duration >= 6
reserve calibration week when confidence < threshold
reserve test/taper weeks only for fixed test goals
calculate remaining development weeks
split development into waves of 3–5 weeks
insert fatigue-management candidate between waves when:
  - user selected planned deloads; or
  - recent fatigue/adherence risk is elevated; or
  - workload increase exceeds configured bounds
merge blocks shorter than 2 weeks unless they are calibration, test, or review
validate total weeks exactly equals requested duration
```

Later versions can optimise the allocation, but the deterministic policy must remain inspectable.

## 9. Adaptation engine

### 9.1 Inputs

Use only observed, user-provided, or explicitly derived data:

- completed set performance;
- exercise type;
- RPE where logged;
- routine-match confidence;
- recent e1RM or rep trend;
- set completion;
- session duration and rest data where available;
- adherence and missed exposures;
- pain/injury feedback;
- programme constraints;
- health/recovery data with source quality and freshness;
- user overrides.

### 9.2 Decision order

1. Safety and hard constraints.
2. User override.
3. Exercise/routine match confidence.
4. Completion and technique/pain flags.
5. Performance trend.
6. Effort/RPE.
7. Recovery/fatigue indicators.
8. Programme-block target.
9. Progression rule.

A weak health signal must not override a clear user instruction or produce a dramatic automatic reduction. Confidence controls the size and reversibility of the change.

### 9.3 Example deterministic outcomes

- Target achieved with lower-than-target effort: progress load or reps.
- Target achieved at target effort: hold or make the minimum progression.
- Target achieved at excessive effort: hold.
- One missed target: hold and observe.
- Repeated misses with stable recovery: reduce load, adjust rep target, or flag technique/substitution review.
- Repeated misses with poor recovery/adherence: reduce work sets before discarding productive intensity.
- High fatigue but no performance loss: cap progression, do not automatically deload.
- Pain/injury flag: block normal progression and require a constraint-safe alternative.
- Sparse history: conservative prescription and low-confidence label.

### 9.4 Hysteresis and anti-oscillation

The engine must avoid changing direction every session.

Use:

- minimum exposures before escalating a rule;
- cooldown windows after reductions;
- bounded weekly change;
- separate thresholds for entering and leaving fatigue management;
- previous-decision context;
- explicit “hold” as a valid outcome.

### 9.5 Planned and reactive deloads

A deload is one fatigue-management option, not a mandatory fourth week.

Support:

- planned reduction selected by the user;
- reactive reduction from repeated evidence;
- exercise-specific reductions;
- volume-only reduction;
- frequency reduction;
- load and volume reduction;
- technique/practice week.

Every deload decision records why it happened and what ends it.

## 10. Evidence-informed policy

The engine should encode broad evidence-supported tendencies while preserving individualisation.

### 10.1 Strength

Higher-load training has the clearest advantage for maximal strength. The programme should prioritise heavier, specific exposure for strength goals while controlling fatigue and retaining useful volume.

### 10.2 Hypertrophy

A broad range of loads can support hypertrophy. Weekly volume is a more important programme lever than forcing every exercise into a heavy-loading phase. The system should estimate and progress tolerated hard-set volume rather than assuming every user needs the same number of sets.

### 10.3 Frequency

Frequency is primarily a way to distribute quality volume and manage fatigue. The product should respect the routines the user selected and should not rewrite a split merely because another frequency is fashionable.

### 10.4 Failure and effort

Training to failure is not required for every set or exercise. The engine should use effort targets and reserve failure selectively, while recognising that Hevy can provide per-set RPE when users log it.

### 10.5 Periodisation

Periodisation can be useful for organisation and may modestly benefit strength, but evidence does not justify one mandatory block sequence for every outcome. Blocks should emerge from goal and constraints.

### 10.6 Autoregulation

RPE/APRE/velocity approaches are viable ways to adapt loading. Workout Agent has direct access to RPE in the Hevy schema, but missing RPE must reduce confidence rather than trigger fabricated estimates.

### 10.7 Deload and taper

A temporary reduction in volume/frequency can preserve progress, but fixed deload timing is not universally established. A true taper is mainly relevant to a dated performance test or competition.

## 11. API design

Use typed request and response models. Do not extend the untyped `template_key` selector.

### Hevy source APIs

```http
POST /api/hevy/sync
GET  /api/hevy/sync-status
GET  /api/hevy/routines
GET  /api/hevy/routines/{routine_id}
GET  /api/hevy/routine-folders
GET  /api/hevy/workouts
POST /api/hevy/workout-matches/{workout_id}/confirm
```

### Programme builder APIs

```http
POST /api/programmes/preview
POST /api/programmes
GET  /api/programmes/{programme_id}
GET  /api/programmes/{programme_id}/revisions
POST /api/programmes/{programme_id}/activate
POST /api/programmes/{programme_id}/pause
POST /api/programmes/{programme_id}/complete
GET  /api/programmes/active
```

Example preview request:

```json
{
  "selected_routines": [
    {
      "hevy_routine_id": "routine-a",
      "position": 1,
      "participation": "required"
    },
    {
      "hevy_routine_id": "routine-b",
      "position": 2,
      "participation": "required"
    }
  ],
  "duration_weeks": 12,
  "start_date": "2026-09-01",
  "goal": {
    "type": "hypertrophy",
    "priority_muscles": ["back", "shoulders"],
    "test_date": null
  },
  "schedule": {
    "mode": "rolling",
    "sessions_per_week": 4,
    "available_weekdays": []
  },
  "preferences": {
    "planned_deloads": "adaptive",
    "progression_aggressiveness": "moderate",
    "maximum_session_minutes": 75
  }
}
```

Preview response includes:

- canonical routine snapshots;
- match coverage;
- history confidence;
- inferred baselines;
- muscle-set estimates;
- block allocation;
- per-exercise prescriptions;
- assumptions;
- warnings;
- blocking violations;
- deterministic engine version;
- preview token/hash used for activation.

### Active programme APIs

```http
GET  /api/programmes/active/today
GET  /api/programmes/active/weeks/{week_number}
POST /api/programmes/active/adapt
GET  /api/programmes/active/adaptations
GET  /api/programmes/active/routine-drift
POST /api/programmes/active/rebase
```

### Compatibility endpoint

`POST /api/programmes/select` becomes a temporary migration adapter. It must not accept `hybrid_powerbuilding`. After the Angular client is migrated, return `410 Gone` with the builder route and remove the endpoint.

## 12. Runtime architecture

The scheduled agent and web application must share a single programme service.

Suggested module boundaries:

```text
backend/programmes/
  models.py
  repository.py
  service.py
  validation.py
  preview.py
  block_allocator.py
  exercise_roles.py
  prescriptions.py
  adaptation.py
  matching.py
  hevy_source.py
  schemas.py
```

The service exposes:

- `preview_programme`;
- `activate_programme`;
- `get_active_programme`;
- `get_today_prescription`;
- `record_workout_and_adapt`;
- `detect_routine_drift`;
- `rebase_programme`.

The web routes, scheduler, notifications, check-ins, plan page, dashboard, and coach use these methods. None imports static programme constants.

The AI layer receives a structured, already validated prescription and may explain it. It does not rebuild the workout from prose.

## 13. Hevy write-back policy

Read-first remains the default.

The programme should initially remain inside Workout Agent while completed sessions continue to come from Hevy.

Optional publishing can be added later:

- create a dedicated programme folder in Hevy;
- duplicate source routines rather than overwrite them;
- include a Workout Agent marker and programme revision in notes;
- update only owned duplicates;
- detect external edits by content hash;
- show a diff before overwriting;
- respect provider routine limits;
- make publish failure non-destructive to the active programme.

Never mutate a user's original selected routine by default.

## 14. Migration and cutover plan

This should be delivered as small, ordered pull requests.

### PR 1: Architecture and contract

- Add this ADR/design document.
- Update the programme-builder skill to state that Hevy-only is the product contract.
- Create the implementation epic and child issues.
- No runtime change.

### PR 2: Canonical Hevy source cache

Continue #819.

- Add tenant-scoped snapshots and sync state.
- Preserve all routine/workout set fields.
- Add incremental workout-event sync.
- Add idempotency, pagination, retries, raw payload retention, and contract fixtures.
- Add routine drift detection.

### PR 3: Programme domain and migrations

- Add programmes, revisions, selected routines, blocks, prescriptions, week states, matches, and decisions.
- Add repository/service layer.
- Migrate current mutable JSON programmes to archived legacy revisions.
- Stop seeding Hybrid Powerbuilding for new users.
- Do not delete `program.py` yet.

### PR 4: Hevy-first builder

- Replace template cards.
- Add routine library, selection, ordering, exact routine preview, duration, goal, and schedule controls.
- Add typed preview API.
- Add loading, empty, stale, conflict, validation, and error states.
- Preserve keyboard and screen-reader operation.

### PR 5: Deterministic generation engine

- Add exercise-role classifier.
- Add history matcher and confidence.
- Add baseline inference.
- Add goal-specific block allocator.
- Add per-exercise prescription models.
- Integrate #804 and #899.
- Add property tests and golden fixtures.

### PR 6: Activation and adaptation

- Add transactional activation.
- Add week materialisation.
- Ingest completed Hevy workouts.
- Add deterministic adaptation, hysteresis, audit trail, and user overrides.
- Add drift/rebase UI.

### PR 7: Runtime cutover

- Make dashboard, plan, daily scheduler, check-ins, stats, notifications, and coach read the active programme service.
- Remove deadlift/pull-up and twelve-week assumptions.
- Replace Monday-to-Saturday mapping with rolling or configured cadence.
- Replace LLM-generated prescriptions with explanation of deterministic prescriptions.
- Show setup state when no programme exists.

### PR 8: Static programme deletion

Only after a repository-wide import search is clean:

- remove Hybrid Powerbuilding from all data and UI;
- remove static template constants and seed paths;
- remove legacy fallback rendering;
- delete `backend/program.py`;
- delete or rewrite static programme tests;
- remove static Hevy write-back;
- update README and operator docs;
- add a regression test that fails on the string `Hybrid Powerbuilding` outside migration history.

### PR 9: Optional Hevy publishing

- Create managed programme folders/routine copies.
- Add provider conflict handling and publish audit.

## 15. Testing strategy

### Unit tests

- canonicalisation and content hashing;
- set parsing for weight/reps, bodyweight, duration, distance, RPE, failure, drop, and warm-up sets;
- exercise-role classification;
- routine-workout match features and thresholds;
- baseline inference with outliers and sparse data;
- block allocation for every duration/goal combination;
- exact duration conservation;
- progression and regression rules;
- hysteresis;
- validator rules;
- source drift;
- date/week calculations.

### Property tests

- allocated block weeks always sum to programme duration;
- blocks never overlap and remain ordered;
- no prescription violates hard constraints;
- adaptation changes remain within configured bounds;
- a repeated identical input produces the same decision;
- tenant A cannot retrieve or mutate tenant B data;
- activation leaves at most one active programme per user;
- rebase never mutates an existing revision.

### Contract tests

Use anonymised Hevy fixtures for:

- unknown fields;
- empty pages;
- pagination;
- updated and deleted workout events;
- custom exercises;
- every exercise type;
- missing optional fields;
- changed routine ordering;
- rate limiting and transient failure.

### Integration tests

- sync → select → preview → activate → ingest workout → adapt;
- routine edited in Hevy after activation;
- routine deleted in Hevy;
- sparse history;
- no RPE;
- ambiguous workout match and user confirmation;
- programme replacement and archive;
- migration from a legacy static active programme.

### End-to-end tests

- first-time user with no key;
- connected user with no routines;
- routine selection and keyboard reorder;
- 12-week preview;
- blocking constraint;
- activation;
- active plan rendering;
- stale sync warning;
- compare/rebase;
- no static fallback.

### Verification gate

Every implementation PR records actual results for:

```bash
python -m compileall backend
ruff check backend tests
mypy backend
pytest -q
cd frontend && npm ci && npm test -- --watch=false
cd frontend && npm run build
```

Additional commands are added for migration/property/E2E suites. No PR claims checks that were not run.

## 16. Observability and audit

Metrics:

- Hevy sync latency, errors, pages, stale age, and event lag;
- number of routine snapshots and drift events;
- workout-match confidence distribution;
- preview failures and validation warnings;
- programme activation/completion/abandonment;
- adherence by programme and block;
- adaptation decision types;
- percentage of decisions overridden;
- prescription change magnitude;
- health-data freshness and influence;
- publish conflicts.

Each adaptation can be explained from stored inputs and rule version without replaying an LLM conversation.

## 17. Safety and product boundaries

- This is training guidance, not medical diagnosis or rehabilitation treatment.
- Pain, injury, and medical constraints produce conservative blocking or referral language.
- Health integrations are optional. Missing recovery data cannot block ordinary programme generation.
- Body-composition and wearable estimates must be treated as noisy signals.
- The system does not claim to infer readiness perfectly.
- The engine must not prescribe extreme volume changes, forced failure, or unsafe testing from an LLM suggestion.
- User-selected routines remain visible so generated changes are never hidden.
- Every automated change is reversible.

## 18. Acceptance criteria for the complete initiative

- `Hybrid Powerbuilding` is absent from all user-facing UI, API options, new data, prompts, notifications, and runtime fallbacks.
- A new user cannot activate a programme without selecting at least one Hevy routine.
- The builder loads the authenticated user's Hevy routines and displays an exact preview.
- The user can select, reorder, and classify routines.
- The user can choose duration and goal.
- Preview creates goal-specific blocks whose weeks exactly match the selected duration.
- The programme uses historical Hevy data when available and labels sparse/ambiguous inference.
- Completed workouts are matched with recorded confidence and can be corrected.
- Progression is deterministic, role-specific, and audited.
- Routine edits in Hevy never silently mutate an active programme.
- All programme-dependent runtime surfaces use the active programme service.
- Users with no active programme see setup guidance, not a static plan.
- Legacy static programmes are archived or explicitly migrated without losing history.
- `backend/program.py` and all static imports are deleted.
- Tenant-isolation, migration, property, contract, integration, and E2E tests pass.

## 19. Research basis

The product policy above is intentionally conservative. It translates broad evidence into configurable defaults rather than pretending research identifies one perfect programme.

Primary references:

1. Currier BS, D'Souza AC, Fiatarone Singh MA, et al. **American College of Sports Medicine Position Stand. Resistance Training Prescription for Muscle Function, Hypertrophy, and Physical Performance in Healthy Adults: An Overview of Reviews.** *Medicine & Science in Sports & Exercise*. 2026;58(4):851-872. DOI: https://doi.org/10.1249/MSS.0000000000003897
2. Moesgaard L, Beck MM, Christiansen L, Aagaard P, Lundbye-Jensen J. **Effects of Periodization on Strength and Muscle Hypertrophy in Volume-Equated Resistance Training Programs: A Systematic Review and Meta-analysis.** *Sports Medicine*. 2022;52:1647-1666. DOI: https://doi.org/10.1007/s40279-021-01636-1
3. Currier BS, McLeod JC, Banfield L, et al. **Resistance training prescription for muscle strength and hypertrophy in healthy adults: a systematic review and Bayesian network meta-analysis.** *British Journal of Sports Medicine*. 2023. PMID: https://pubmed.ncbi.nlm.nih.gov/37414459/
4. Huang Z, Sun J, Li D, Chen C, Wang D. **Autoregulated resistance training for maximal strength enhancement: a systematic review and network meta-analysis.** *Journal of Exercise Science & Fitness*. 2025. DOI: https://doi.org/10.1016/j.jesf.2025.07.006
5. Grgic J, Schoenfeld BJ, Orazem J, Sabol F. **Effects of resistance training performed to repetition failure or non-failure on muscular strength and hypertrophy: a systematic review and meta-analysis.** *Journal of Sport and Health Science*. 2022;11(2):202-211. DOI: https://doi.org/10.1016/j.jshs.2021.01.007
6. Schoenfeld BJ, Ogborn D, Krieger JW. **Dose-response relationship between weekly resistance training volume and increases in muscle mass: a systematic review and meta-analysis.** *Journal of Sports Sciences*. 2017. PMID: https://pubmed.ncbi.nlm.nih.gov/27433992/
7. Pancar Z, Silva AF, Clemente FM, et al. **Effects of deload periods in resistance training on muscle hypertrophy and strength endurance in untrained young men using a randomized within-subject design.** *Scientific Reports*. 2026;16:10299. DOI: https://doi.org/10.1038/s41598-026-40612-5
8. Hevy API documentation: https://api.hevyapp.com/docs/
9. Hevy's public API/GPT schema repository: https://github.com/hevyapp/hevy-gpt

## 20. Explicit non-goals

- Replacing user-selected Hevy routines with a hidden house programme.
- Reintroducing Hybrid Powerbuilding under another name.
- Guaranteeing an “optimal” programme.
- Treating every four weeks as a mandatory deload.
- Treating every final block as a peak.
- Assuming all workouts can be matched to a routine with certainty.
- Making an LLM the source of truth for load or set changes.
- Mutating original Hevy routines without an explicit publish action.
