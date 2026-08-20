# Property-based boundary testing

Workout Agent uses Hypothesis to exercise parser, provider-import, tenant-isolation, and programme-validation boundaries with generated records in addition to hand-written examples.

## What is covered

`backend/tests/test_property_based_boundaries.py` generates bounded Hevy set/workout/routine payloads, malformed optional records, duplicate and reordered records, timezone offsets, programme durations and goals, and tenant-separated workout imports.

The suite locks down these invariants:

- parser and import functions are deterministic and do not mutate provider payloads;
- malformed list entries fail safely or are ignored where the parser contract permits them;
- set ordering does not change the selected top set, while provider dictionary key ordering does not change canonical source hashes;
- parsed numeric values remain finite and inside the generated safety bounds;
- timezone-aware workout timestamps preserve elapsed duration;
- programme request serialisation round-trips stably and invalid duration bounds fail closed;
- generated blocks are deterministic, contiguous, individually bounded, and conserve the exact requested programme duration;
- generated workout imports remain isolated by `user_id`.

## Reproducing and promoting failures

Hypothesis automatically shrinks a failing generated example to a smaller counterexample and prints a reproducible example in the pytest failure output.

When a failure represents a real bug:

1. fix the underlying implementation rather than weakening the invariant;
2. copy the minimal counterexample into `backend/tests/fixtures/property_regressions.json` when it is useful across multiple properties, or add it as an `@example(...)` beside the affected property;
3. keep the generated property test so nearby cases continue to be explored;
4. run the focused suite before committing.

The JSON fixture is deliberately synthetic. It must never contain real Hevy payloads, health data, user identifiers, API keys, OAuth tokens, cookies, or other personal data.

## Commands

From the repository root:

```bash
python -m pip install -r backend/requirements.txt
cd backend
python -m pytest -q tests/test_property_based_boundaries.py
```

For a larger local search:

```bash
cd backend
pytest -q tests/test_property_based_boundaries.py --hypothesis-show-statistics
```

The `Property based boundaries` GitHub Actions workflow runs the focused suite whenever the parser/import/programme/database boundaries, its dependency declaration, fixtures, or the property tests change.
