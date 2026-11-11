# Hevy history engine

**Status:** implemented foundation for #972
**Related:** #819 (source cache), #970 (programme persistence), #971 (builder preview)

The public Hevy workout schema does not expose the source `routine_id`. The
history engine reconstructs likely routine matches from immutable routine
snapshots and derives conservative exercise baselines from the supporting sets.
All decisions are deterministic, versioned and tenant scoped. No LLM runs
inside this path.

Module: `backend/hevy_history.py`

## Rule versions

| Constant | Current value | Meaning |
| --- | --- | --- |
| `MATCHER_VERSION` | `hevy-history-matcher-v1` | Feature weights, score bands and tie-breaking |
| `BASELINE_VERSION` | `hevy-baseline-v1` | Set classification, robust statistics and confidence rules |
| `HISTORY_RULE_VERSION` | `hevy-history-v1` | Aggregate analysis output contract |

Any material change to weights, thresholds or baseline maths must bump the
relevant version so stored matches and baselines remain explainable.

## Routine-workout matching

Candidate features and initial weights (starting hypotheses, not permanent
physical constants):

| Feature | Weight | Notes |
| --- | --- | --- |
| Exercise-template weighted Jaccard | 0.34 | Weights use planned/working set counts, not titles |
| Ordered exercise-sequence similarity | 0.17 | Longest common subsequence over template order |
| Normalised title similarity | 0.11 | Token overlap; title alone cannot force a match |
| Set-count / set-structure similarity | 0.13 | Total work sets plus per-exercise distribution |
| Superset structure similarity | 0.06 | Neutral when provider omits superset metadata |
| Rolling-cycle position | 0.10 | Expected next routine after the last confident match |
| Timestamp relative to snapshot version | 0.09 | Older workouts against a newer revision score lower |

Workouts are processed oldest to newest so the cycle feature is stable.

Confidence bands:

| Band | Score | Downstream use |
| --- | --- | --- |
| `high` | >= 0.78 | Automatic routine-specific adaptation |
| `probable` | >= 0.52 | Reduced adaptation bounds |
| `ambiguous` | >= 0.35, or a near-tie with another probable candidate | Ask for confirmation or use exercise-level evidence only |
| `unmatched` | < 0.35 | Never assign a routine |

A user correction is stored as a workout, title or exercise-signature pattern in
`routine_match_overrides` and overrides future automatic matching for that
pattern without mutating raw provider data.

## Exercise baselines

For every observed exercise template the engine:

- separates `warmup`, `normal`, `failure` and `dropset` sets (unknown provider
  set types degrade to `normal` and are reported);
- handles weight/reps, bodyweight, assisted, duration, distance and custom
  metrics without inventing volume or e1RM for unsupported modalities;
- estimates e1RM only from valid loaded working sets using Epley, then uses the
  median of exposure bests rather than an all-time maximum;
- excludes outliers using median absolute deviation before computing the
  robust e1RM;
- reports load/reps/sets trends per week, variability, RPE median/IQR, exposure
  frequency, days between exposures and days since the last exposure;
- infers equipment increments from observed load differences or explicit user
  settings, and reports when no increment is inferable;
- flags stale history, incomplete sessions, unknown provider fields, excluded
  personal records, sparse windows and low-confidence routine matches;
- returns an explicit confidence, evidence window and supporting workouts.

The evidence window scales with programme duration (two cycles, minimum 84 days,
maximum 365 days). Sparse history expands the window and sets
`calibration_required` instead of presenting a short sample as authoritative.

## Typed analysis output

`analyse_history()` returns:

- `routine_snapshots` — immutable canonical routine revisions;
- `matches` — score, feature vector, method/version, confidence, snapshot ID,
  ambiguity and user-confirmation state;
- `routine_coverage` — per selected routine match counts and whether it is
  covered by high/probable evidence;
- `unmatched_workout_count` and `ambiguous_workout_count`;
- `baselines` keyed by exercise template with evidence window and supporting
  workout exposure details;
- `calibration_required`, aggregate `confidence` and `warnings`.

The programme preview consumes this output: low-confidence matches and
baselines tighten or disable automatic set-change bounds.

## API surface

- `GET /api/programmes/history` returns the current user's match coverage,
  unmatched/ambiguous counts, per-exercise baselines, evidence windows and
  warnings. Optional `routine_ids=a,b` scopes routine coverage.
- `POST /api/programmes/history/override` stores a user confirmation or
  correction by `workout`, `title` or `exercise_signature` pattern. The next
  preview and history response apply it automatically; raw workouts are never
  modified.

Both routes require an authenticated tenant session and are covered by the
mutation-security middleware.

## Cache and provider budget

The incremental local cache written by `scripts/sync_history.py` (#819) is the
canonical evidence source. The web loader merges cached workouts with the
bounded recent provider page, deduplicating by workout ID and preferring the
most recent provider revision. Provider calls remain bounded; the engine does
not call exercise-history endpoints per exercise.

## Persistence

`database.py` adds tenant-scoped tables:

- `hevy_routine_snapshots` — immutable content-addressed routine revisions;
- `routine_workout_matches` — score, features, method/version, confidence,
  ambiguity and user confirmation;
- `routine_match_overrides` — user corrections by stable pattern;
- `exercise_history_baselines` — latest derived baseline per rule version.

Every read and write requires a non-empty `user_id` and filters by it. Snapshot
rows are insert-only; changed routines produce a new snapshot ID.

## Verification

Focused checks used while implementing #972:

```bash
python -m compileall -q backend
python -m ruff check backend/hevy_history.py backend/dynamic_programme.py \
  backend/database.py backend/hevy_reader.py backend/webapp/app.py \
  backend/tests/test_hevy_history.py
python -m mypy backend/hevy_history.py backend/dynamic_programme.py \
  backend/database.py backend/hevy_reader.py
python -m pytest -q backend/tests/test_hevy_history.py \
  backend/tests/test_dynamic_programme.py backend/tests/test_hevy_reader.py \
  backend/tests/test_database.py backend/tests/test_sync_history.py
python backend/scripts/check_data_policy.py
```
