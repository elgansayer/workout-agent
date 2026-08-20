# Repository inventories

Workout Agent generates route, environment-variable, connector, and GitHub Actions workflow inventories directly from repository source. The generator is the canonical source for security review, onboarding, documentation, and test-gap analysis, so those consumers cannot silently drift into separate hand-maintained lists.

Generated reports are disposable build output. They are intentionally not committed because the source code is authoritative and the generator is deterministic.

## Generate inventories

From the repository root:

```bash
python backend/scripts/repository_inventory.py --check
python backend/scripts/repository_inventory.py --format json --output /tmp/workout-agent-inventory.json
python backend/scripts/repository_inventory.py --format markdown --output /tmp/workout-agent-inventory.md
```

The JSON document contains four canonical inventories:

- `routes`: FastAPI method/path pairs, handler, owner, sensitivity, authentication boundary, and discovered test references.
- `environment_variables`: variables consumed by source/deployment files, source locations, owner, secret/configuration classification, `.env.example` coverage, and test references.
- `connectors`: built-in provider classes, authorization capability, metric families, owner, sensitivity, and test references.
- `workflows`: workflow source/name, triggers, secret usage, authentication context, owner, and test references.

Every item includes the common `owner`, `sensitivity`, `authentication`, and `test_coverage` metadata contract. `--check` fails if a required inventory is empty, IDs collide, common metadata is missing, or the shared consumer views are malformed.

## Shared consumer views

The generator derives these views from the same in-memory canonical inventory:

```bash
python backend/scripts/repository_inventory.py --view security
python backend/scripts/repository_inventory.py --view onboarding
python backend/scripts/repository_inventory.py --view documentation
python backend/scripts/repository_inventory.py --view tests
```

`security` focuses on personalised routes, secrets, connectors, and workflows that consume secrets. `onboarding` lists documented environment configuration and available connectors. `documentation` exposes the public product surface. `tests` lists inventory items without a matching test reference. These views are projections, not independent inventories.

## CI policy

The `Repository inventory` workflow runs the focused unit suite, validates a fresh inventory against the current checkout, and renders both JSON and Markdown into the runner's temporary directory. CI does not require generated snapshots to be committed, so adding a route, connector, variable, workflow, or test updates the inventory automatically.

When adding a new source convention that the scanner cannot discover deterministically, extend the scanner and its fixture tests in the same pull request. Do not add a second hand-maintained inventory.
