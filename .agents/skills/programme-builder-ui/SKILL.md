---
name: programme-builder-ui
description: 'Build or extend the Hevy-native programme flow: select source routines, configure duration/goals, preview blocks, activate a versioned prescription overlay, and reconcile source changes. Use for any /programmes or programme-related API/UI work.'
---

# Hevy-native Programme Builder

## Product decision

Per `docs/HEVY_NATIVE_DYNAMIC_PROGRAMMES.md` and #967, Workout Agent has one programme-creation model:

1. sync the authenticated user's Hevy routines and history;
2. select and order source routines by provider ID;
3. choose duration, goal, priorities, schedule and constraints;
4. analyse the user's actual training history and data confidence;
5. generate goal-specific blocks and week/exercise prescription overlays;
6. activate a versioned programme and adapt only current/future prescriptions.

Do **not** add or preserve static programme templates. `program.py`'s Hybrid Powerbuilding split is legacy code to be removed after all runtime consumers are generalised. It is not a selectable template, seed, default or fallback.

The selected Hevy routines are the exercise/session skeleton. Workout Agent owns blocks, weekly dose, progression and bounded adaptations. Original Hevy routines are read-only by default.

## Canonical references

- Architecture and fitness rationale: `docs/HEVY_NATIVE_DYNAMIC_PROGRAMMES.md`
- Delivery epic: #967
- Hevy routine import/conflicts: #819
- Structured constraints/activation validation: #899
- Per-user connector contracts/cursors: #901
- Recovery features and bounded adaptations: #961 and #962
- Engineering rules: `AGENTS.md`
- Route/auth conventions: `fastapi-route` skill
- Multi-tenant schema conventions: `multi-tenant-migration` skill
- Completion checks: `verification-gate` skill

## Existing building blocks to extend

- **`hevy_client.py`** already reads paginated routines, folders, workouts, exercise templates, history and user information. Extend it through the shared connector contract; add individual-routine and workout-event support where missing.
- **`hevy_reader.py`** normalises Hevy routines and workouts. Generalise it for configurable history windows, provider identity, raw-payload provenance and per-user connector ownership.
- **`programme_inference.py`** describes routine/split usage from Hevy. Evolve it into evidence extraction for the planner; do not create a second inference module with overlapping responsibilities.
- **`programmes` persistence/API/UI** already exists in an early template-oriented form. Migrate its contract rather than adding a parallel builder.
- **`hevy_sync.py`** is legacy outbound behaviour. It must not remain the default activation path. Any later write-back must be explicit, opt-in and use managed copies rather than silently changing source routines.

## Required domain separation

### Hevy source layer

Owns connector status, source routine/folder/exercise/workout records, provider IDs, pagination/cursors, raw payloads, revisions, freshness and conflicts.

### Programme draft layer

Owns selected routine IDs/order, programme specification, analysis report, generated block preview, validation findings and generation version.

### Active programme layer

Owns immutable source snapshots, blocks, weeks, exercise roles/prescriptions, rotation position, decisions and audit history.

### Adaptation layer

Owns deterministic signals, bounded decisions, confidence, explanations, confirmations and reverts. It never mutates completed weeks.

Do not put connector payload parsing, programme generation and UI response shaping into one large route function.

## Procedure

### 1. Check canonical work first

Read #967 and its related issues/PRs. Search open and closed work before editing. Continue the current schema/connector/planner instead of introducing another representation.

### 2. Preserve tenant ownership

Every new table includes `user_id TEXT NOT NULL REFERENCES users(id)`. Every read/write resolves provider IDs under the authenticated user's connector and filters by `user_id`. Add explicit cross-user isolation tests for routes and repository functions.

### 3. Build source routine selection

The UI must support:

- connector/freshness/error state;
- folder filtering and routine search;
- exercise/set previews from Hevy;
- selection by provider ID;
- ordered rotation editing with keyboard-accessible controls;
- last-used/recent-completion context;
- source revision/conflict state.

Never use title as identity. Never label inferred values as Hevy source values.

### 4. Store an explicit programme specification

At minimum:

- duration weeks, initially 4–24;
- primary goal strategy;
- selected routine IDs and order;
- sessions per week or rotating schedule;
- experience;
- structured constraints confirmation.

Support optional priorities, event date, available days, maximum session duration, planned interruptions and adaptation aggressiveness. Defaults must be visible and editable.

### 5. Analyse real history

Use a configurable history window, initially at least eight weeks and expandable for sparse data. Derive observations such as adherence, routine sequence, fractional muscle sets, exercise load/rep trends, session duration, stalls and confidence.

Cold-start users receive conservative prescriptions and an early review. Do not invent a learned baseline.

### 6. Generate blocks by goal and duration

Use a versioned strategy registry and deterministic allocator. Blocks are normally 3–6 weeks, but duration and terminal needs control the output. Do not force four-week accumulation/intensification/peaking blocks.

Only strength-test, event or power goals need a true realisation/taper phase. Hypertrophy, maintenance/cut and return-to-training require different strategies. Block boundaries are review points; recovery weeks are conditional unless explicitly planned.

### 7. Generate prescription overlays

Keep source routine exercises recognisable. Store modality-aware targets and progression methods by exercise role. Typical roles are priority lift, primary compound, secondary compound, accessory/isolation and prehab/core/conditioning.

Persist source baseline, rationale, confidence, rule version and allowed change bounds. Exercise substitutions require approved mappings and normally user confirmation.

### 8. Validate and activate atomically

Run the shared structured validator for import, edit, generation and activation. On activation:

- snapshot current source routine revisions;
- create an immutable programme version;
- archive the previous active programme;
- activate exactly one programme for the user;
- initialise rotation/week state;
- record engine/rule versions and inputs.

Do not silently migrate a static active programme into a Hevy-derived definition. Archive it as legacy and require a new activation.

### 9. Adapt future scope only

After workouts sync, map them to routine snapshots using provider relations or a scored matcher with confidence. Progress/hold/reduce within the configured bounds. Persist contributing evidence and explanation.

Completed prescriptions are immutable. A missed session does not skip a weekday workout; the next routine remains next in the rotation.

### 10. Reconcile source changes

Compare current Hevy routine revisions with active snapshots. Show a diff and require a deliberate keep/accept/map/version action for structural changes. Do not silently merge source edits into the active programme.

## UI language

Use these concepts:

- **Select routines from Hevy**
- **Hevy routine preview**
- **Programme duration**
- **Goal and priorities**
- **Block timeline**
- **Current week prescription**
- **Source changed**
- **Why this changed**

Remove these concepts:

- **Available Templates**
- **Hybrid Powerbuilding**
- **Active Split Preview**
- static `program.py` coaching rules
- automatic "push generated programme to Hevy" language

## Guardrails

- Read original Hevy routines by default; write-back is a separate opt-in managed-copy feature.
- Never identify routines only by title.
- Never run e1RM progression on bodyweight/duration/distance modalities.
- Never trigger a material reduction from one stale or missing wearable metric.
- Never let AI output bypass deterministic schema validation or change bounds.
- Never mutate completed weeks.
- Never hide low confidence.
- Never activate a programme that violates blocking user constraints.
- Never create a new unscoped programme or connector table.

## Migration sequence

1. Remove obsolete branding/template-first guidance.
2. Add tenant-safe Hevy routine snapshots, revisions and conflicts.
3. Add routine selection/order/preview and draft persistence.
4. Add programme specification, history analysis and confidence.
5. Add deterministic block planner and prescription overlay.
6. Add versioned activation and legacy-static migration.
7. Generalise runtime consumers and delete all `program.py` imports.
8. Delete `program.py` and obsolete tests after a reference scan is zero.
9. Add workout mapping/progression and recovery adaptations.
10. Add optional managed-copy write-back only after source safety is complete.

Do not delete `program.py` early while import-time consumers still depend on it; do not leave it reachable after the migration is complete.

## Verification

Follow the `verification-gate` skill and record exact results.

In addition, verify:

- Hevy API contract fixtures for routines, folders, workouts/events, updates and deletes;
- generated blocks cover every requested week exactly once;
- all automatic changes stay within configured bounds;
- completed prescriptions remain immutable;
- routine source edits create conflicts rather than silent programme changes;
- active programme rotation survives missed sessions;
- legacy static data migrates without history loss;
- every API/repository path rejects cross-user access;
- frontend routine selection and ordering are keyboard accessible;
- no user-facing or runtime-selectable Hybrid Powerbuilding reference remains.

Manually walk the production-equivalent flow:

1. connect/sync Hevy;
2. select and order routines;
3. configure duration/goal/constraints;
4. inspect history assumptions and confidence;
5. preview routines and block timeline;
6. activate;
7. sync a completed workout;
8. inspect next routine and explained progression;
9. edit a source routine in a fixture and resolve the conflict.

A passing unit suite is not enough if the new planner is orphaned or the old static route remains active.