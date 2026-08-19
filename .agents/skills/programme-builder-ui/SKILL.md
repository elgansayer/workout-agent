---
name: programme-builder-ui
description: 'Build or extend the Hevy-first workout-programme builder, routine selection/preview, duration and goal controls, generated blocks, activation, or programme runtime. Use for any /plan or /programmes feature. Static programme templates are forbidden.'
---

# Programme Builder UI

## Product Contract

Read `docs/HEVY_FIRST_DYNAMIC_PROGRAMMES.md` before changing programme code.
It is the canonical architecture and supersedes the earlier template/custom-
builder model.

Workout Agent has one programme source: **routines selected by the authenticated
user from their Hevy account**.

Do not add, preserve, seed, select, render, or fall back to:

- `Hybrid Powerbuilding`;
- any other built-in programme template;
- a hidden six-day or weekday split;
- a static `program.py` definition;
- an LLM-generated routine that bypasses explicit Hevy routine selection.

Hevy routines define the exercise topology. Workout Agent adds duration, goal,
blocks, prescriptions, validation, progression, and adaptations around immutable
snapshots of the selected routines.

## Existing Building Blocks

- **`hevy_client.py`** already reads routines, folders, exercise templates,
  workouts, exercise history, and provider metadata. Extend it with pagination,
  retries, incremental workout events, and contract tests rather than replacing
  it.
- **`hevy_reader.py`** already canonicalises some routine and workout data. It
  must preserve every relevant per-set field, including set type, weight, reps,
  distance, duration, RPE, custom metric, order, rest, notes, and supersets.
- **`programme_inference.py`** is discovery code, not the target programme
  engine. Reuse split/muscle analysis where sound, but do not keep its current
  all-routines, four-week synthetic programme contract.
- **`program.py`** is legacy code to remove after all consumers use the dynamic
  programme service. Do not use it as a template or source for new work.
- **Issue #819** is the canonical Hevy routine import/snapshot work. Continue it
  instead of opening a duplicate.
- **Issues #804 and #899** define shared programme-constraint validation. Preview,
  create, activation, rebase, edit, and adaptation must use that validator.

## Required Builder Flow

1. **Hevy state**
   - Show connection and last successful sync.
   - Show stale, partial, empty, and provider-error states.
   - Provide `Refresh from Hevy`.
   - When no routines exist, show setup guidance; never show a static programme.

2. **Routine library**
   - Group routines by Hevy folder.
   - Let the user select and deselect routines.
   - Display the exact routine structure before selection.
   - Preserve exercise/set ordering and provider fields.
   - Support keyboard-accessible reordering of selected routines.
   - Let the user mark selected routines required, optional, or rotating.

3. **Programme settings**
   - Require duration, start date, goal, and at least one selected routine.
   - Support rolling order or an explicit calendar cadence.
   - Support priority exercises/muscles, test date, maximum session length,
     constraints, concurrent training, progression aggressiveness, and planned
     versus adaptive fatigue management.
   - Use typed request/response models.

4. **Generated preview**
   - Show selected Hevy routine snapshots.
   - Show routine-match/history confidence.
   - Show inferred baselines and exercise roles.
   - Show exact programme dates and goal-specific blocks.
   - Show per-exercise prescription/progression rules.
   - Show assumptions, warnings, and blocking violations.
   - Weeks across blocks must sum exactly to the selected duration.

5. **Activation**
   - Recheck source hashes and constraints transactionally.
   - Persist an immutable programme revision.
   - Leave at most one active programme per user.
   - Archive or pause the previous programme without deleting history.
   - Materialise the first week and emit an audit event.

6. **Continuous adaptation**
   - Match completed Hevy workouts to routine snapshots with an explicit score
     and confidence band; the public workout schema has no routine ID.
   - Let users confirm ambiguous matches.
   - Apply deterministic, role-specific progression with bounded changes and
     hysteresis.
   - Record every decision, input, confidence, and engine version.
   - LLMs may explain decisions but must not be the only progression engine.

7. **Routine drift**
   - Active revisions keep their imported snapshots when Hevy changes.
   - Offer keep, compare/merge, rebase, or replace.
   - Never silently overwrite an active programme or the user's source routine.

## Programme and Block Rules

A block is a target envelope, not a mandatory four-week phase. Block allocation
must depend on goal, duration, history confidence, recovery/adherence risk, and
an optional test date.

- Hypertrophy programmes need development and review/fatigue management, not a
  compulsory peak.
- Strength programmes may increase specificity and include a taper/test only
  when the goal includes a dated test.
- Recomposition/energy-deficit programmes preserve useful intensity and cap
  recoverability-sensitive volume.
- Maintenance programmes minimise unnecessary programme volatility.
- Return-to-training programmes calibrate conservatively.
- Short programmes must not fabricate three phases merely to fill a timeline.

Do not hard-code deadlift, pull-up, stage-prep, body-fat, weekday, or Sunday-rest
assumptions. Exercise roles are programme-specific.

## Domain Requirements

New programme work should use tenant-scoped first-class entities for:

- Hevy routine snapshots and sync state;
- programmes and immutable revisions;
- selected routine order/cadence;
- blocks;
- exercise prescriptions;
- week states;
- routine-workout matches;
- adaptation decisions;
- source drift and user overrides.

Do not expand the mutable `template_key` plus JSON-blob model as the final
architecture. Every table and query follows the `multi-tenant-migration` skill.

## Runtime Cutover

The web app, `/plan`, dashboard, scheduler, check-ins, statistics, notifications,
coach, and Hevy integration must consume one shared programme service. A UI-only
change is incomplete while runtime code imports `program.py` constants.

Delete `program.py` only after a repository-wide import/reference search is
clean and legacy active programmes have been archived or explicitly migrated.
Add a regression test that prevents `Hybrid Powerbuilding` from returning
outside migration history.

## Verification

Follow the `verification-gate` skill and record real results. At minimum cover:

- tenant isolation;
- Hevy pagination, unknown fields, update/delete events, and provider failure;
- routine snapshot/hash drift;
- selection, ordering, exact preview, and keyboard operation;
- every goal/duration block allocation;
- block-week duration conservation;
- sparse history and ambiguous match confidence;
- deterministic prescriptions and adaptation hysteresis;
- activation transaction and one-active-programme invariant;
- migration from a legacy static programme;
- no-programme setup state and absence of static fallback;
- frontend tests and production build.

## Delivery Discipline

This initiative must be split into dependency-ordered pull requests: source
cache, programme domain, builder, generation engine, activation/adaptation,
runtime cutover, static deletion, then optional managed publishing to Hevy.
Do not land a half-static, half-dynamic runtime.
