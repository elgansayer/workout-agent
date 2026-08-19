# Hevy-native dynamic programme architecture

**Status:** Accepted product direction  
**Date:** 2026-08-19  
**Canonical delivery issue:** #967  
**Related work:** #819, #899, #901, #961, #962

## 1. Executive decision

Workout Agent will no longer offer, seed, activate, or treat **Hybrid Powerbuilding** or any other static programme template as a starting point.

The only programme-creation path is Hevy-native:

1. sync the user's Hevy folders, routines, exercise templates and workout history;
2. let the user select and order the Hevy routines they want to train;
3. snapshot those routines with provider identifiers and provenance;
4. collect a small programme specification: duration, goal, priorities, schedule and constraints;
5. analyse the user's actual Hevy training history and data quality;
6. build an explainable sequence of goal-specific blocks;
7. apply a versioned prescription overlay to the selected routines;
8. adapt only current and future prescriptions from completed workouts, recovery data and check-ins.

The selected Hevy routines are the **exercise and session skeleton**. Workout Agent owns the **programme intent, blocks, weekly dose, progression and bounded adaptations**.

By default, Workout Agent must not mutate the user's original Hevy routines. The first production implementation is read-first and stores its prescriptions locally. A later, explicit opt-in may create managed copies in a dedicated Hevy folder, but it must never silently overwrite source routines.

This decision supersedes guidance that treats `program.py` as a selectable template or assumes `hevy_sync.py` should push every generated programme back into Hevy.

## 2. Why the current design must be replaced

The current architecture starts with one fixed 12-week, six-day plan in `program.py`. It then calculates targets and uses `hevy_sync.py` to create or update routines in Hevy. The existing inference path can read routines and recent workouts, but it imports all routines, relies heavily on titles and produces a descriptive split rather than a durable, selectable, adaptive programme.

That architecture has five structural problems:

1. **The app owns the wrong thing.** It invents a workout skeleton and treats Hevy as an output device, while users already maintain the routines they actually want in Hevy.
2. **One plan cannot represent different goals.** Hypertrophy, maximal strength, powerbuilding, maintenance during a cut, return-to-training and competition peaking require different emphasis and terminal states.
3. **A fixed calendar is brittle.** Users miss sessions, train on rotating days, edit routines and have different programme durations. A weekday-indexed split loses the intended sequence.
4. **Static four-week blocks overstate the evidence.** Periodisation can help strength, but no universal accumulation/intensification/peaking sequence is optimal for every outcome or person.
5. **Unbounded write-back is unsafe.** Routine titles change, Hevy data can be stale, provider schemas can drift and users can edit source routines. Silent updates create conflicts and destroy intent.

The replacement must be source-aware, goal-aware, confidence-aware, tenant-safe and reversible.

## 3. Product boundary

### 3.1 Hevy owns

- the user's source routine folders;
- routine identity and ordering within Hevy;
- exercises and the routine's default set structure;
- completed workout records;
- source edits, renames and deletions.

### 3.2 Workout Agent owns

- the ordered selection of routines used by a programme;
- a versioned snapshot of each selected source routine;
- programme duration and goal strategy;
- block and week structure;
- exercise roles and user overrides;
- target set ranges, rep ranges, load anchors and RIR/RPE guidance;
- progression rules;
- adaptation decisions, evidence, confidence and audit history;
- conflict state between the active snapshot and current Hevy source.

### 3.3 The user owns final intent

The user must confirm:

- which routines belong in the programme;
- their order or schedule;
- goal and priorities;
- constraints and injuries;
- ambiguous routine/workout mappings;
- material routine-source conflicts;
- exercise substitutions or destructive changes.

The engine may propose, explain and bound changes. It must not pretend low-confidence inference is a fact.

## 4. Terminology

- **Source routine:** A routine currently stored in Hevy.
- **Routine snapshot:** An immutable local copy of a source routine revision at programme activation or reconciliation.
- **Routine selection:** Ordered references to source routine IDs/snapshots used by one programme.
- **Rotation:** The intended sequence of routines independent of weekdays. A missed session delays the rotation rather than skipping a workout.
- **Programme specification:** User intent and constraints: goal, duration, priorities, schedule, experience and adaptation policy.
- **Block:** A multi-week phase with a coherent objective and prescription policy.
- **Week prescription:** The planned dose and progression rules for one programme week.
- **Exercise prescription:** A versioned overlay for a routine exercise, not a replacement exercise definition.
- **Adaptation:** A bounded, persisted decision that changes a current or future prescription.
- **Source conflict:** A difference between the active routine snapshot and the current Hevy routine revision.
- **Data confidence:** An explicit estimate of whether available history supports a proposed baseline or change.

## 5. Evidence-based programming principles

This engine is not a digital copy of one coach's favourite periodisation scheme. It should implement robust principles and expose uncertainty.

### 5.1 Progressive resistance training matters more than elaborate templates

The 2026 American College of Sports Medicine position stand synthesised 137 systematic reviews covering more than 30,000 participants. It concluded that progressive resistance training improves strength, hypertrophy, power and physical function, while relatively few prescription variables consistently change outcomes. Heavier loading favours maximal strength, higher weekly volume favours hypertrophy, and moderate loads moved with high intent favour power. Training to momentary fatigue and periodisation did not consistently improve all outcomes.

**Product implication:** begin with a viable routine the user will perform, establish a recoverable baseline, apply progressive overload and make goal-specific changes. Do not force complexity where the data do not justify it.

### 5.2 Periodisation is a strategy, not the product

A 2022 systematic review and meta-analysis found a modest strength advantage for periodised over non-periodised training when volume was equated, but no clear hypertrophy advantage. Undulating loading appeared useful for strength in trained participants. A 2026 comparison of linear and undulating approaches likewise found broadly comparable outcomes across many measures.

**Product implication:** blocks should organise intent, fatigue and specificity. They are not mandatory four-week boxes, and the engine must not claim that accumulation, intensification and peaking are universally required.

### 5.3 Specificity should drive terminal blocks

Maximal strength is best expressed after sufficient exposure to heavier, specific work. Power is best trained with movements and loads that permit high movement velocity. Hypertrophy does not require a low-repetition peak. A maintenance or fat-loss phase does not require a mythical "fat-burning rep range."

**Product implication:** only strength-test, power or event goals receive a true realisation/taper block. Hypertrophy programmes finish with an assessment, specialisation or resensitisation phase rather than an automatic peak.

### 5.4 Volume is a dose with diminishing returns

The 2026 ACSM position stand reports greater hypertrophy with higher weekly volume, including a useful population-level reference around ten or more sets per muscle per week. The 2025 dose-response meta-regression supports a positive relationship with diminishing returns and found that fractional counting of indirect work is more informative than treating every exercise set as a full set for every involved muscle.

**Product implication:** estimate the user's current direct and indirect hard-set exposure, then adjust from their demonstrated recoverable baseline. Do not jump every user to one universal set target. Store direct sets as 1.0 and meaningful indirect contribution as a configurable fraction, initially 0.5, with exercise/muscle confidence.

### 5.5 Frequency mostly distributes work

When volume is equated, training frequency has little independent effect on hypertrophy in the available meta-analytic evidence. Higher frequency can improve skill exposure, distribute high volume, shorten sessions and support strength practice.

**Product implication:** preserve the user's selected routine rotation where feasible. Recommend frequency changes only when they solve a concrete problem such as excessive per-session volume, insufficient priority-lift exposure, poor adherence or recovery bottlenecks.

### 5.6 Failure is optional and costly

Meta-analyses and recent reviews do not show that momentary muscular failure is required for hypertrophy, and failure generally creates more acute fatigue. A 2026 meta-analysis found non-failure training at least comparable for most adaptations and slightly favourable for dynamic strength.

**Product implication:** use RIR/RPE ranges as effort guidance, not mandatory failure. Reserve occasional near-failure work for appropriate exercises and users. Compounds should normally retain more repetitions in reserve than stable isolation work.

### 5.7 Autoregulation is useful, but must be bounded

Systematic reviews suggest RIR/RPE, APRE and velocity-based approaches can outperform or usefully complement fixed percentage prescriptions for maximal strength. Subjective readiness is noisy, and consumer health data can be missing or contradictory.

**Product implication:** use deterministic rules first, combine multiple signals, record confidence and cap every change. AI may explain or summarise decisions, but must not directly rewrite a programme outside the same validated bounds.

### 5.8 Deloading has more coaching consensus than trial evidence

An international Delphi study defines deloading as a temporary reduction in training stress to reduce fatigue and improve preparedness. Exact timing and dose are not established as universal laws.

**Product implication:** schedule **review points**, not compulsory deloads. A recovery week may be planned near a block boundary, triggered by converging fatigue/performance signals, shortened, skipped or replaced by a simple volume reduction.

### 5.9 Peaking evidence is limited

Research specific to powerlifting peaking remains limited, so highly precise taper claims would overstate evidence.

**Product implication:** competition/event strategies must be conservative, configurable and clearly labelled as estimates. Learn the user's response over repeated cycles rather than presenting one taper as optimal.

### 5.10 Training during energy restriction should retain the training signal

Resistance training helps preserve fat-free mass during calorie restriction. Evidence does not support automatically replacing useful loading with very high repetitions or assuming more volume is always better during a cut.

**Product implication:** a maintenance/cut strategy generally retains meaningful intensity and exercise specificity while reducing volume only when recovery, adherence or performance require it.

## 6. Design principles derived from the evidence

1. **Preserve exercises; vary prescription.** The user's routines remain recognisable across blocks.
2. **Change the minimum effective variable.** Prefer one explainable adjustment over simultaneous changes to sets, reps, load, frequency and exercises.
3. **Use observed baselines.** Start near what the user has recently completed successfully, then progress.
4. **Prioritise specificity.** Goal and priority exercises determine loading emphasis and terminal blocks.
5. **Treat volume as recoverability-constrained.** More is not automatically better.
6. **Use effort ranges.** RIR/RPE absorbs day-to-day variation better than false load precision.
7. **Separate planning horizons.** Programme, block, week and session decisions use different signals and bounds.
8. **Never rewrite history.** Completed weeks and decisions are immutable audit records.
9. **Expose confidence and provenance.** Every inferred baseline states its source window and quality.
10. **Prefer adherence over theoretical perfection.** A slightly less "optimal" rotation completed consistently is the better programme.
11. **Require confirmation for semantic changes.** Replacing exercises, changing goals and resolving source conflicts are user decisions.
12. **Degrade safely.** Missing Hevy, wearable or check-in data should produce conservative defaults, not arbitrary reductions.

## 7. End-to-end user journey

### Step 1: Connect and inspect Hevy

Show connector status, last successful sync, source freshness and errors. Fetch routine folders, routines, exercise templates and enough workout history to establish a baseline.

If the account is not Hevy Pro or the API fails, explain the limitation and do not offer a fake inferred programme.

### Step 2: Select routines

The user can:

- filter by Hevy folder;
- search routines;
- expand each routine to inspect exercises and default sets;
- select one or more routines;
- order a rotation by drag, keyboard controls or explicit position buttons;
- optionally associate routines with preferred weekdays while retaining rotation semantics;
- exclude warm-up, mobility or ad-hoc routines;
- see last performed date and recent completion count.

Routine selection is by Hevy provider ID. Titles are display data only.

### Step 3: Configure programme intent

Required fields:

- duration in weeks, initially 4–24;
- primary goal;
- intended sessions per week or a pure rotating schedule;
- experience level;
- structured constraints confirmation.

Optional fields:

- event/test date;
- priority lifts;
- priority muscles;
- secondary goal;
- available weekdays and maximum session duration;
- adaptation aggressiveness;
- planned interruptions or travel;
- exercise-role overrides.

Defaults should come from profile and Hevy history, but the user sees and can change them.

### Step 4: Analyse history

Display a concise analysis before generation:

- history window and number of usable sessions;
- inferred routine sequence and adherence;
- estimated weekly direct/fractional sets by muscle;
- priority-exercise exposure and load/rep trends;
- session-duration distribution;
- detected stalls or inconsistent logging;
- confidence and missing data.

The analysis must distinguish observations from recommendations.

### Step 5: Generate and preview blocks

Show:

- a programme summary;
- block timeline with objective and duration;
- selected Hevy routine previews;
- per-block changes to sets, rep/load zones and target RIR;
- progression method per exercise role;
- planned review/recovery points;
- assumptions, warnings and constraint violations;
- examples of how a completed set will drive the next prescription.

### Step 6: Activate

Activation must:

- run the shared structured programme validator;
- snapshot current routine revisions;
- persist the programme specification and generated version;
- mark exactly one active programme for that user;
- archive, not overwrite, an old active programme;
- establish current rotation position and programme week;
- record the engine/rule version and all inputs.

### Step 7: Train and adapt

After each synced workout:

- map the workout to an active routine/snapshot;
- update exercise and muscle progress signals;
- apply only allowed session-level changes;
- show the next routine in rotation;
- explain any change.

At weekly and block review boundaries, the engine may modify future weeks within policy. Past prescriptions remain locked.

### Step 8: Reconcile source changes

When a selected Hevy routine changes, show a diff and offer:

- keep the active snapshot until the next programme;
- accept the source change for future weeks;
- map added/removed exercises and preserve compatible prescriptions;
- create a new programme version;
- remove or replace a deleted routine.

No conflict is silently resolved.

## 8. History ingestion and analysis

### 8.1 Retrieval window

Do not use a fixed count such as 15 recent workouts as the only input.

Initial policy:

- request at least the most recent 8 weeks;
- expand up to 26 weeks when needed for sparse schedules, strength trends or event history;
- cap by a configurable workout count and API budget;
- use `/v1/workouts/events` and a cursor for ongoing incremental sync;
- retain raw provider payloads and parser/schema version for replay.

### 8.2 Workout eligibility

A workout can contribute differently to different signals:

- completed working sets inform dose and progression;
- warm-up sets do not count as hard sets;
- failed, partial or assisted repetitions require modality-aware handling;
- bodyweight, duration and distance exercises do not use load-based formulas;
- deleted or later-edited workouts must update derived data idempotently;
- ad-hoc workouts may inform exercise history but not routine adherence.

### 8.3 Routine matching

Preferred matching order:

1. explicit provider routine relation, when supplied;
2. stored user mapping;
3. exact source routine ID in locally managed metadata;
4. scored similarity across ordered exercise template IDs, set structure and title;
5. manual resolution when the best score is below threshold or the top candidates are too close.

Never use routine title as the sole identity.

Store the match method, score and runner-up margin.

### 8.4 Set and muscle volume

For each usable set store or derive:

- exercise template ID and movement family;
- set type;
- load, reps, duration or distance;
- estimated effort when available;
- primary and secondary muscle contributions;
- direct/fractional set weight;
- source confidence.

Initial fractional model:

- primary target muscle: 1.0 set;
- meaningful secondary contributor: 0.5 set;
- stabiliser or uncertain contribution: 0 unless explicitly modelled.

These values are model parameters, not immutable truths. User overrides and improved exercise metadata must be versioned.

### 8.5 Strength trend

Estimate strength only for suitable loaded exercises and sets. Initial rules:

- use a consistent e1RM formula and persist its version;
- prefer top valid sets in a sensible repetition range, initially 1–10 and configurable up to 12;
- exclude warm-ups, assisted sets, obvious unit errors and non-load modalities;
- use robust session summaries rather than the single highest noisy estimate;
- compare repeated exposures of the same exercise/template or an explicit mapped variant;
- report trends only after a minimum number of exposures.

A trend is not a tested 1RM and must be labelled accordingly.

### 8.6 Adherence and recoverability

Derive:

- planned versus completed routine exposures;
- rolling sessions per week;
- skipped/shortened sessions;
- session duration and set completion;
- repeated underperformance at comparable effort;
- unexpected drops in reps/load;
- optional check-in and recovery signals;
- stale/missing data indicators.

Do not infer "poor recovery" from one missed workout or one wearable reading.

### 8.7 Confidence model

Every generated programme and adaptation receives component confidence:

- routine identity confidence;
- workout-to-routine mapping confidence;
- history sufficiency;
- exercise-role confidence;
- volume-estimate confidence;
- strength-trend confidence;
- recovery-data freshness and agreement.

A conservative aggregate confidence determines whether the engine may act, suggest, or require confirmation.

Suggested states:

- **high:** generate and apply normal bounded progression;
- **medium:** generate conservative prescriptions and explain assumptions;
- **low:** ask for missing intent/mapping and avoid material automatic changes;
- **unavailable:** use a cold-start baseline and mark the programme as such.

## 9. Exercise roles

The same routine exercise can require different progression depending on its purpose. Infer a role, then let the user override it.

### Priority lift

A goal-defining lift or movement pattern. Strength-specific programming may use top-set/back-off, heavier exposure and higher specificity.

### Primary compound

A large multi-joint movement that carries substantial fatigue. Use conservative effort targets, smaller progression steps and stricter fatigue bounds.

### Secondary compound

Supports the primary movement or muscle goal with moderate systemic fatigue. Double progression or rep-range progression is often appropriate.

### Accessory/isolation

Stable, local-fatigue exercise. Wider repetition ranges and occasional closer-to-failure work are acceptable when constraints permit.

### Prehab/core/conditioning

Progress by quality, duration, distance, density or tolerance rather than e1RM. Never force a barbell-style load algorithm.

### Role inference features

- exercise-template metadata and movement family;
- position in routine;
- loading and repetition history;
- number of working sets;
- frequency across routines;
- goal/priority selection;
- user override.

Persist inferred role, confidence, reason and override source.

## 10. Goal strategies

A strategy supplies block objectives and prescription policies. It does not replace selected routines.

### 10.1 Hypertrophy

**Primary objective:** increase recoverable hard-set exposure and progressive performance across moderate-to-wide repetition ranges.

Possible block sequence:

1. **Foundation/baseline:** stabilise technique, effort calibration and recoverable volume.
2. **Volume or overload:** gradually add reps, load or selected sets where progress and recovery support it.
3. **Specialisation or consolidation:** bias priority muscles while holding non-priority work near maintenance, or consolidate performance if no specialisation is requested.
4. **Resensitisation/assessment:** reduce fatigue, assess response and prepare the next cycle.

No mandatory heavy peak.

### 10.2 Maximal strength

**Primary objective:** improve and express force in selected priority lifts.

Possible block sequence:

1. **Base:** build technical volume and relevant musculature.
2. **Strength development:** increase heavy/specific exposure while controlling fatigue.
3. **Intensification:** lower some volume and increase specificity/load.
4. **Realisation/taper/test:** only when a test or event is requested.

Use heavier work for priority lifts while accessories can remain in hypertrophy-friendly ranges.

### 10.3 Powerbuilding

**Primary objective:** combine priority-lift strength with broad hypertrophy.

Possible block sequence:

1. **Hypertrophy-biased base.**
2. **Blended development:** heavy priority exposure plus recoverable accessory volume.
3. **Strength-biased intensification.**
4. **Reduction/test or assessment:** chosen from user intent, not assumed.

This is a strategy generated from the user's routines, not the old Hybrid Powerbuilding template.

### 10.4 Maintenance or fat-loss phase

**Primary objective:** preserve strength, muscle and adherence under reduced recovery resources.

Possible block sequence:

1. **Baseline/retention:** maintain meaningful loading and familiar exercises.
2. **Recovery-aware maintenance:** reduce low-priority volume if performance/recovery deteriorates.
3. **Consolidation/review:** reassess after the diet phase or programme endpoint.

Do not automatically convert the routine to high repetitions. Avoid adding volume merely because the goal is fat loss.

### 10.5 Return-to-training

**Primary objective:** rebuild tolerance and consistency after a layoff, illness or disruption.

Possible block sequence:

1. **Familiarisation/ramp:** conservative sets, effort and load.
2. **Rebuild:** progress toward recent historical volume and performance.
3. **Normal development:** transition into the chosen long-term goal strategy.

Old personal bests are context, not immediate targets.

### 10.6 Power/performance

**Primary objective:** improve rapid force production or sport-relevant performance.

Use suitable movements, lower-to-moderate volume, moderate loads where velocity can remain high and explicit intent to move quickly. Do not apply power prescriptions indiscriminately to every routine exercise.

### 10.7 Event/competition peak

**Primary objective:** express a tested performance on a date.

Requirements:

- explicit event date;
- priority movements and rules;
- sufficient history and user experience;
- conservative taper parameters;
- no medical or weight-cut automation.

Because peaking evidence is limited, expose assumptions and permit manual override.

## 11. Programme duration and block allocation

Duration is a user input, not a hidden constant. Initial supported range is 4–24 weeks. Longer plans can be represented as sequential programme versions later.

### 11.1 Allocation rules

1. Determine whether the goal requires a terminal realisation/test block.
2. Determine whether the user is returning from a layoff and needs a ramp.
3. Reserve zero to two weeks for terminal assessment, taper or transition according to goal.
4. Divide remaining time into main blocks, normally 3–6 weeks.
5. Prefer fewer meaningful blocks over many cosmetic phases.
6. Add review points at block boundaries; recovery weeks are conditional unless explicitly planned.
7. Avoid changing every training variable at a boundary. Each block should have one dominant intent.

Initial duration bands:

| Duration | Typical structure |
| --- | --- |
| 4–5 weeks | One development block plus final assessment/review |
| 6–9 weeks | Two blocks, optionally with a short transition |
| 10–15 weeks | Three main objectives plus optional assessment/taper |
| 16–24 weeks | Four or five blocks with one or more formal review points |

These are allocator constraints, not fixed templates.

### 11.2 Example 12-week outputs

**Hypertrophy, no specialisation:** 4-week baseline/volume block, 5-week overload block, 2-week consolidation block, 1-week assessment/recovery option.

**Hypertrophy with priority muscles:** 3-week baseline, 4-week volume block, 4-week specialisation, 1-week assessment/recovery option.

**Strength with test date:** 4-week base, 4-week strength development, 3-week intensification, 1-week taper/test.

**Powerbuilding without test:** 4-week hypertrophy-biased base, 4-week blended development, 3-week strength bias, 1-week assessment.

**Maintenance during a cut:** 5-week retention, 5-week recovery-aware maintenance, 2-week consolidation/review. A reduced-load taper is not automatically useful.

The preview must explain why one structure was selected.

## 12. Prescription overlay

A routine snapshot remains intact. A prescription overlay can vary by programme version, block and week.

Suggested exercise-prescription fields:

```json
{
  "routine_snapshot_id": "...",
  "exercise_template_id": "...",
  "role": "primary_compound",
  "block_id": "...",
  "week_number": 5,
  "set_target": {"min": 3, "target": 3, "max": 4},
  "rep_target": {"min": 5, "max": 8},
  "effort_target": {"rir_min": 2, "rir_max": 3},
  "load_anchor": {"type": "last_successful", "value": 100, "unit": "kg"},
  "progression_method": "double_progression",
  "allowed_change": {
    "max_set_delta": 1,
    "max_load_percent": 5,
    "max_rir_delta": 1
  },
  "reason": "Recent successful exposures support progression",
  "confidence": 0.84,
  "rule_version": "programme-engine/v1"
}
```

The representation must support load, bodyweight, assisted, duration and distance modalities without fake fields.

## 13. Progression methods

### 13.1 Double progression

Suitable for many secondary compounds and accessories.

- Keep load stable while the user progresses within a rep range at target effort.
- Increase load only after the prescribed sets reach the upper rep threshold with acceptable RIR across a configured number of exposures.
- Return toward the lower end of the range after a load increase.
- Use available equipment increments and exercise-specific rounding.

### 13.2 Top-set and back-off progression

Suitable for selected priority compounds when the user understands RPE/RIR.

- Prescribe one top set in an effort/load range.
- Derive back-off sets from the achieved top set or recent stable baseline.
- Cap week-to-week load change.
- Do not infer a true max from one unusually good or bad set.

### 13.3 Rep-first progression

Suitable when small load increments are unavailable. Add repetitions within an allowed range before load.

### 13.4 Set progression

Add a set only where muscle-level volume, session duration, progress and recovery justify it. Initial guardrail: no more than one set added to an exercise in one weekly review and no more than two fractional sets added to one muscle in a week without explicit confirmation.

### 13.5 Load progression

Use recent successful exposures, target effort and equipment increments. Initial maximum automatic increase is configurable and normally 1–5%, with smaller bounds for upper-body or technically sensitive priority lifts.

### 13.6 Bodyweight, assisted, duration and distance progression

- bodyweight: repetitions, tempo/ROM quality, added load or assistance reduction;
- assisted: reduce assistance only after rep/effort criteria are met;
- duration: time or interval progression;
- distance: distance, pace or resistance progression.

Never run e1RM logic on incompatible modalities.

## 14. Adaptation hierarchy

### 14.1 Session-level decisions

Purpose: account for today's performance/readiness without changing programme intent.

Allowed initial decisions:

- proceed as planned;
- use a lower or higher load within the prescribed range;
- remove one optional set;
- increase target RIR by one;
- defer an exercise and request confirmation;
- substitute only from an explicitly approved equivalence set.

A missed session advances nothing. The next session remains the next routine in rotation unless the user changes it.

### 14.2 Exposure-level progression

After an exercise exposure:

- progress when all relevant sets meet the success criteria at target effort;
- hold when performance is improving but criteria are incomplete;
- reduce or regress only after repeated underperformance, unexpected effort or a safety signal;
- distinguish a one-off bad day from a trend.

### 14.3 Weekly review

Use adherence, completed dose, performance trend, session duration, subjective check-ins and fresh recovery data.

Change at most one primary lever per exercise or muscle group unless a safety rule requires more. Persist the evidence and rejected alternatives.

### 14.4 Block review

At a block boundary:

- compare actual versus planned exposure;
- assess response by priority outcome;
- decide whether to progress, extend, repeat, pivot, recover or end;
- regenerate only future weeks;
- preserve the active routine selection unless the user confirms a source change.

### 14.5 Programme review

At completion, create a response summary and a proposed next programme. Never silently roll into an endless cycle with hidden changes.

## 15. Recovery and deload policy

### 15.1 Multi-signal triggers

A recovery intervention should normally require more than one signal, for example:

- repeated performance regression at comparable effort;
- rising subjective fatigue or soreness;
- poor adherence caused by session burden;
- elevated resting heart rate relative to a stable personal baseline;
- suppressed HRV where the device/source is reliable and fresh;
- worsening sleep over multiple nights;
- unusually long session duration or incomplete sets;
- user request.

Provider anomalies, stale readings and missing HRV cannot independently trigger a material programme rewrite.

### 15.2 Recovery actions

Initial bounded options:

- reduce working sets by approximately 30–50%;
- retain movement practice and use a modest load reduction where appropriate;
- increase target RIR by one or two;
- remove optional intensifiers or failure work;
- shorten the intervention to one or two exposures if recovery rapidly normalises;
- request confirmation when signals conflict.

These ranges are starting policy bounds, not universal physiological constants.

### 15.3 Planned review versus automatic fourth-week deload

The block allocator may place a recovery **option** at a boundary, but the weekly engine decides to use, shorten, skip or replace it based on actual response. A calendar alone is insufficient evidence.

## 16. Cold start and sparse data

A user may have routines but little usable history.

Cold-start behaviour:

- import and preview routines normally;
- ask for experience, recent training consistency and approximate effort preference;
- preserve Hevy set/rep defaults where safe;
- begin with conservative effort and volume;
- avoid e1RM-based prescriptions without valid exposures;
- set low confidence explicitly;
- schedule an earlier review after one to two exposures per routine;
- increase only after demonstrated completion.

The product must be useful without pretending it has learned a baseline it has not observed.

## 17. Hevy connector design

Hevy's public API currently exposes paginated workouts, workout count, incremental workout events, individual workouts, routines, individual routines, exercise templates, routine folders, exercise history and user information. The documentation warns that the API may change and is available to Hevy Pro users.

Connector requirements:

- typed provider adapter and capabilities;
- per-user credential ownership;
- pagination and rate/error handling;
- incremental cursor for workout events;
- idempotent upsert/delete processing;
- raw payload, provider ID and parser/schema version retention;
- contract fixtures for known response shapes;
- schema-drift detection and graceful degradation;
- freshness and last-error status;
- reconnect and purge lifecycle;
- no title-only identity;
- no cross-user cache.

Required client additions include individual-routine retrieval and workout-event polling if they are not already wired.

## 18. Source snapshots and conflicts

A routine snapshot should include:

- `user_id`;
- provider and account/connection ID;
- Hevy routine ID and folder ID;
- title for display;
- ordered exercises and source set definitions;
- exercise-template IDs;
- raw payload;
- normalised content hash;
- provider revision/update timestamp when available;
- fetched timestamp;
- parser schema version;
- superseded snapshot ID.

Conflict states:

- unchanged;
- renamed only;
- compatible content edit;
- structural content edit;
- source deleted;
- inaccessible/stale;
- parse/schema error.

A compatible edit may preserve exercise prescriptions by exercise-template ID and occurrence. Structural edits require a preview and explicit reconciliation.

## 19. Proposed data model

All tables require `user_id TEXT NOT NULL REFERENCES users(id)` and every query must filter by it.

### `programmes`

- id, user_id, status, version;
- goal strategy and secondary goal;
- duration and dates;
- specification JSON/schema version;
- engine/rule version;
- confidence summary;
- active/archive timestamps;
- predecessor programme/version.

### `programme_routines`

- programme ID, user ID;
- routine snapshot ID;
- rotation position;
- preferred weekday optional;
- enabled state;
- source conflict state.

### `hevy_routine_snapshots`

Stores the source fields described above. Immutable after creation.

### `programme_blocks`

- programme ID and user ID;
- position, objective and strategy key;
- start/end week;
- volume/intensity/effort policy;
- review/recovery policy;
- rationale and confidence.

### `programme_weeks`

- programme/block/user IDs;
- week number and state;
- planned rotation exposures;
- dose policy;
- generated version;
- locked/completed timestamp.

### `exercise_roles`

- programme/snapshot/exercise/user IDs;
- role, movement family and muscle contributions;
- inferred confidence/reason;
- user override and version.

### `exercise_prescriptions`

- programme/week/routine/exercise/user IDs;
- modality-aware targets;
- progression method and allowed bounds;
- source baseline and confidence;
- generated/locked timestamps.

### `adaptation_decisions`

- user/programme/week/exercise scope;
- decision and before/after values;
- contributing signals with provenance/freshness;
- rule/model version;
- confidence;
- explanation;
- confirmation state;
- created/applied/reverted timestamps.

### `routine_workout_matches`

- user, workout and routine snapshot IDs;
- method, score and ambiguity;
- user-confirmed flag;
- matcher version.

### `connector_sync_cursors` and `connector_conflicts`

Use the shared connector-platform model rather than programme-specific duplicates.

### Transitional option

A versioned JSON v2 programme document may be used for the first vertical slice, provided:

- tenant scoping is correct;
- schema version and migration are explicit;
- immutable versions are preserved;
- the API does not leak storage shape as a permanent contract;
- normalisation remains the planned destination.

## 20. Proposed API surface

Names are illustrative; OpenAPI is authoritative once implemented.

### Hevy source

- `GET /api/connectors/hevy/status`
- `POST /api/connectors/hevy/sync`
- `GET /api/connectors/hevy/routine-folders`
- `GET /api/connectors/hevy/routines`
- `GET /api/connectors/hevy/routines/{id}`
- `GET /api/connectors/hevy/routines/{id}/history-summary`
- `GET /api/connectors/hevy/conflicts`

### Draft programme

- `POST /api/programmes/drafts`
- `PATCH /api/programmes/drafts/{id}/routines`
- `PATCH /api/programmes/drafts/{id}/specification`
- `POST /api/programmes/drafts/{id}/analyse`
- `POST /api/programmes/drafts/{id}/generate`
- `GET /api/programmes/drafts/{id}/preview`
- `POST /api/programmes/drafts/{id}/validate`
- `POST /api/programmes/drafts/{id}/activate`

### Active programme

- `GET /api/programmes/active`
- `GET /api/programmes/{id}`
- `GET /api/programmes/{id}/blocks`
- `GET /api/programmes/{id}/weeks/{week}`
- `GET /api/programmes/{id}/next-session`
- `GET /api/programmes/{id}/decisions`
- `POST /api/programmes/{id}/review`
- `POST /api/programmes/{id}/archive`

### Conflicts and overrides

- `POST /api/programmes/{id}/routine-conflicts/{conflict_id}/resolve`
- `PUT /api/programmes/{id}/exercise-roles/{exercise_id}`
- `POST /api/programmes/{id}/decisions/{decision_id}/confirm`
- `POST /api/programmes/{id}/decisions/{decision_id}/revert`

All endpoints require authenticated user ownership. Provider IDs supplied by clients must be resolved under the same user's connector account.

## 21. UI replacement

### Remove

- **Available Templates**;
- Hybrid Powerbuilding cards, labels and branding;
- **Active Split Preview**;
- static coaching rules derived from `program.py`;
- any button that silently pushes generated routines into Hevy.

### Add

#### Hevy routine selector

- connection/freshness state;
- folder filters and search;
- selectable routine cards;
- expandable exercise/set preview;
- last-used and completion context;
- ordered rotation editor;
- accessible keyboard reordering;
- conflict badges.

#### Programme setup

- duration control with week count and dates;
- goal strategy;
- priority lifts/muscles;
- schedule/rotation controls;
- constraints summary and validation;
- advanced adaptation settings behind progressive disclosure.

#### Programme preview

- source-backed Hevy routine preview;
- block timeline;
- per-block prescription deltas;
- current week and next session;
- confidence and assumptions;
- safety warnings;
- generation diff when settings change.

#### Active programme

- programme progress and block position;
- next routine in rotation;
- current prescription overlay;
- source-change warnings;
- recent adaptation decisions with reasons;
- explicit regenerate/reconcile/archive actions.

The UI must not imply that a generated value came from Hevy when it was inferred by Workout Agent. Label source, inference and recommendation distinctly.

## 22. Static-plan removal and migration

Complete removal is a coordinated migration, not a text rename.

### Inventory

- `backend/program.py` and its tests;
- `backend/hevy_sync.py` assumptions and default write path;
- imports of block/week/day constants in `backend/webapp/app.py` and other modules;
- `AVAILABLE_TEMPLATES`, database seeds and active programme defaults;
- `/api/programmes` and `/api/programmes/select` template contracts;
- SPA template cards and `Active Split Preview`;
- footer and PWA description;
- agent skill guidance and documentation;
- tests/fixtures asserting the static split.

### Migration policy

1. Stop exposing/activating static templates.
2. Mark existing static active programmes as `legacy_static` and inactive.
3. Preserve their historical records for audit; do not reinterpret them as Hevy-derived.
4. Prompt affected users to connect Hevy and create a new programme.
5. Generalise runtime consumers to active programme/week prescriptions.
6. Remove all `program.py` imports.
7. Delete `program.py` and obsolete static tests only after an import/reference scan is zero.
8. Constrain or replace `hevy_sync.py` with an explicit managed-copy write-back service.

A migration must be additive, idempotent and tenant-safe.

## 23. Deterministic engine before generative AI

The core planner and adaptation engine must be deterministic and testable:

- explicit strategy registry;
- versioned block allocator;
- versioned exercise-role classifier;
- progression state machines;
- bounded decision rules;
- confidence thresholds;
- shared constraint validator.

An AI provider may:

- explain a generated plan in natural language;
- summarise evidence and history;
- propose options inside the deterministic schema;
- translate coaching tone.

An AI provider may not:

- bypass structured validation;
- invent provider data;
- exceed allowed change bounds;
- silently substitute exercises;
- write raw free-form output directly into active prescriptions.

## 24. Initial safety and change bounds

These are conservative product guardrails and must remain configurable/versioned:

- no more than one automatic set added to one exercise in a weekly review;
- no more than two fractional weekly sets added to one muscle without confirmation;
- no automatic load jump above the exercise/equipment bound, normally 1–5%;
- no automatic target-effort change greater than 1 RIR in a normal review;
- no automatic exercise replacement outside an approved equivalence map;
- no material progression on low-confidence routine mapping;
- no reduction based solely on one stale/missing wearable metric;
- no completed-week mutation;
- no original Hevy routine mutation by default;
- constraint violations block activation or require an explicitly defined warning acknowledgement.

Safety-sensitive symptoms, pain or medical constraints should pause or narrow automation and direct the user to appropriate professional advice rather than diagnose.

## 25. Testing strategy

### Unit tests

- history-window selection and pagination;
- payload parsing and schema drift;
- routine identity and hashing;
- workout-to-routine matching scores;
- volume/fractional-set aggregation;
- e1RM eligibility and robust trend calculation;
- exercise-role inference;
- duration/block allocation for every strategy;
- progression state machines;
- confidence aggregation;
- bounded adaptation rules;
- conflict classification and reconciliation.

### Property-based tests

- generated blocks cover every programme week exactly once;
- block ordering and durations are valid for all supported durations;
- no change exceeds configured bounds;
- completed prescriptions never mutate;
- routine order remains stable unless explicitly changed;
- parser and migration round-trips preserve provider identity;
- one user's identifiers never resolve another user's records.

### Contract tests

- captured Hevy fixtures for routines, folders, workouts, events, updates and deletes;
- pagination and cursor behaviour;
- API error, timeout, stale credential and schema-change handling;
- OpenAPI request/response compatibility.

### Integration tests

- new user connects Hevy, syncs, selects routines, generates and activates;
- existing legacy-static user migrates without history loss;
- Hevy routine edit creates a conflict and does not alter active prescriptions;
- completed workout advances the correct rotation and progression state;
- missed workout does not skip the routine;
- programme switch archives rather than overwrites;
- cross-user isolation for every route and table.

### Frontend tests

- routine selection and keyboard ordering;
- duration/goal validation;
- routine and block previews;
- low-confidence and conflict states;
- accessible labels, focus order and error handling;
- responsive display for long routine/exercise names.

### Verification

Run the repository verification gate and relevant frontend commands. Record exact commands/results in every PR. Do not claim checks that were not run.

## 26. Observability and audit

Track without exposing sensitive data:

- connector sync duration/result/cursor lag;
- routines and workouts imported per user-safe aggregate;
- parser/schema errors;
- routine conflict counts and age;
- workout-match confidence distribution;
- generation duration and strategy/rule version;
- programme activation/abandonment;
- planned versus completed sessions;
- adaptation decision types, confirmation and revert rates;
- low-confidence fallbacks;
- cross-tenant access denials.

Every active prescription must be traceable to its source snapshot, programme version, generation rule and later adaptation decisions.

## 27. Delivery slices

### Slice 0: Decision and obsolete surface removal

- add this document and canonical epic;
- update programme-builder guidance;
- remove Hybrid branding and template-first language;
- replace the visible static preview with Hevy-source terminology without pretending routine selection already exists.

### Slice 1: Tenant-safe routine source

- complete per-user Hevy routine/folder storage;
- add individual-routine and workout-event support;
- snapshot revisions and provenance;
- add conflict detection and contract fixtures.

### Slice 2: Routine selection and preview

- source APIs;
- folder/search selector;
- ordered routine selection;
- exact imported routine preview;
- draft persistence.

### Slice 3: Programme specification and analysis

- duration, goals, priorities, schedule and constraints schema;
- configurable history window;
- adherence, volume, exercise-role and trend analysis;
- confidence report.

### Slice 4: Block planner and prescription overlay

- strategy registry;
- duration allocator;
- week/exercise prescriptions;
- deterministic preview and validation;
- cold-start policy.

### Slice 5: Activation and complete static removal

- versioned activation;
- legacy-static migration;
- generalise runtime consumers;
- remove template endpoints/seeds;
- remove `program.py` and obsolete static tests;
- disable default Hevy mutation.

### Slice 6: Workout mapping and progression

- incremental events;
- routine matching;
- rotation progression;
- exercise state machines;
- weekly review decisions.

### Slice 7: Recovery and adaptation

- recovery baselines and freshness;
- bounded session/weekly decisions;
- explanations, confirmations and reverts;
- conditional recovery weeks.

### Slice 8: Optional managed-copy write-back

- explicit opt-in;
- dedicated folder and managed routine identity;
- source/managed-copy distinction;
- diff, conflict and rollback controls.

## 28. Non-goals for the first release

- generating a completely new exercise split from scratch;
- replacing source Hevy exercises without confirmation;
- claiming an optimal medical/rehabilitation programme;
- automatic contest weight cuts;
- prescribing a true 1RM test without user intent;
- real-time velocity-based training without validated velocity data;
- fully autonomous competition peaking;
- silent Hevy write-back;
- AI-authored unstructured active plans.

## 29. Defaults selected for implementation

To avoid blocking the first vertical slice on product questions:

- duration range: 4–24 weeks;
- default duration: inferred from goal, otherwise 12 weeks;
- schedule: ordered rotation first, preferred weekdays optional;
- history baseline: 8 weeks, expandable to 26 weeks;
- write-back: off;
- progression: deterministic;
- target effort: RIR ranges where the modality supports them;
- deload: conditional, with block-boundary review points;
- source conflict: hold active snapshot until user resolution;
- low confidence: conservative cold start and early review;
- static programme migration: archive as legacy, require new Hevy-native activation.

## 30. References

1. Hevy public API documentation: https://api.hevyapp.com/docs/
2. Currier BS, et al. American College of Sports Medicine Position Stand. *Resistance Training Prescription for Muscle Function, Hypertrophy, and Physical Performance in Healthy Adults: An Overview of Reviews*. Medicine & Science in Sports & Exercise. 2026. https://doi.org/10.1249/MSS.0000000000003897
3. Moesgaard L, et al. *Effects of Periodization on Strength and Muscle Hypertrophy in Volume-Equated Resistance Training Programs: A Systematic Review and Meta-analysis*. Sports Medicine. 2022. https://pubmed.ncbi.nlm.nih.gov/35044672/
4. Pelland JC, et al. *The Resistance Training Dose Response: Meta-Regressions Exploring the Effects of Weekly Volume and Frequency on Muscle Hypertrophy and Strength Gains*. Sports Medicine. 2025. https://doi.org/10.1007/s40279-025-02344-w
5. Currier BS, et al. *Resistance training prescription for muscle strength and hypertrophy in healthy adults: a systematic review and Bayesian network meta-analysis*. British Journal of Sports Medicine. 2023. https://pubmed.ncbi.nlm.nih.gov/37414459/
6. Refalo MC, et al. *Influence of Resistance Training Proximity-to-Failure on Skeletal Muscle Hypertrophy: A Systematic Review with Meta-analysis*. Sports Medicine. 2023. https://pubmed.ncbi.nlm.nih.gov/36334240/
7. Wu S, et al. *Effects of resistance training performed to repetition non-failure on exercise performance in healthy adults: a systematic review and meta-analysis*. BMC Sports Science, Medicine and Rehabilitation. 2026. https://pubmed.ncbi.nlm.nih.gov/42410632/
8. Greig L, et al. *The Effect of Load and Volume Autoregulation on Muscular Strength and Hypertrophy: A Systematic Review and Meta-Analysis*. Sports Medicine - Open. 2022. https://pubmed.ncbi.nlm.nih.gov/35038063/
9. Weakley J, et al. *Effects of subjective and objective autoregulation methods for intensity and volume on enhancing maximal strength during resistance-training interventions: a systematic review*. PeerJ. 2021. https://pubmed.ncbi.nlm.nih.gov/33520457/
10. Bell L, et al. *Integrating Deloading into Strength and Physique Sports Training Programmes: An International Delphi Consensus Approach*. Sports Medicine - Open. 2023. https://pubmed.ncbi.nlm.nih.gov/37730925/
11. Pritchard H, et al. *Tapering and Peaking Maximal Strength for Powerlifting Performance: A Review*. Sports. 2020. https://pubmed.ncbi.nlm.nih.gov/32917000/
12. *Effects of Calorie Restriction With and Without Strength, Endurance or Mixed Training on Fat-Free and Skeletal Muscle Mass*. 2026. https://pubmed.ncbi.nlm.nih.gov/42144246/
13. Roth C, et al. *Resistance training volume does not influence lean mass preservation during energy restriction in trained males*. Scandinavian Journal of Medicine & Science in Sports. 2022. https://pubmed.ncbi.nlm.nih.gov/36114738/

## 31. Decision summary

The product should not ask, "Which pre-written programme template do you want?"

It should ask:

> Which Hevy routines do you want to train, what are you trying to achieve, how long do you have, and what does your actual training history tell us is a sensible next dose?

Everything in the implementation should follow from that question.