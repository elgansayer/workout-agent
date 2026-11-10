# Programme domain foundation

The Hevy-first programme architecture stores programmes as tenant-scoped,
immutable revisions. This document describes the foundation delivered for
issue #970. Runtime surfaces migrate to it separately; the legacy
`programmes.definition` blob remains a read-only compatibility path in the
meantime.

## Storage model

All tables declare `user_id TEXT NOT NULL REFERENCES users(id)` and every
repository predicate includes the authenticated tenant.

| Table | Purpose |
| --- | --- |
| `programmes` | Identity and lifecycle: name, status, goal, duration, dates, current revision, predecessor. |
| `programme_revisions` | Immutable revision number, reason, engine version, settings and selected-source summary. |
| `programme_routines` | Ordered selected Hevy routine snapshots with provider identity, hash, cadence and conflict state. |
| `programme_blocks` | Ordered, non-overlapping week ranges with objective, targets, adaptation policy and exit criteria. |
| `exercise_prescriptions` | Deterministic modality-aware set/rep/effort/load/rest and progression rules. |
| `programme_week_states` | Planned versus actual dose, adherence, fatigue and review state. |
| `adaptation_decisions` | Inputs, output, confidence, rule version and from/to revision audit record. |
| `routine_workout_matches` | Workout-to-routine mapping score, confidence, method and user confirmation. |

Indexes are tenant-prefixed. A partial unique index,
`idx_programmes_one_active_per_user`, enforces at most one `active` programme
per user. Database triggers reject updates and deletes of
`programme_revisions`, so revisions stay immutable even outside the service.

## Service boundary

```python
from programme_domain import ProgrammeService

service = ProgrammeService(db_path)
service.preview_draft(user_id, draft)
service.persist_draft(user_id, draft)
service.activate(user_id, programme_id)
service.pause(user_id, programme_id)
service.complete(user_id, programme_id)
service.archive(user_id, programme_id)
service.get_active_programme(user_id)
service.get_active_revision(user_id)
service.get_programme_week(user_id, programme_id, week_number)
service.create_revision_from_adaptation(user_id, programme_id, draft, decision)
service.create_revision_from_rebase(user_id, programme_id, draft)
service.list_revision_history(user_id, programme_id)
```

Drafts carry typed selected routine snapshots, explicit rotation positions,
duration, goal, blocks and prescriptions. Validation rejects cross-tenant
routine snapshots, duplicate or non-contiguous rotation positions,
out-of-duration or overlapping blocks, and prescriptions that reference an
unselected routine or unknown block.

Activation archives any previous active programme and activates the target in
one `BEGIN IMMEDIATE` transaction. Editing, adaptation and rebase operations
append a new revision and move the programme's `current_revision_id`; previous
revisions are never mutated.

## Migration behaviour

`database.init_db()` performs an additive, idempotent migration:

1. Adds the programme lifecycle columns and creates the normalized tables and
   tenant indexes when absent.
2. Converts any active or historical static programme to `source =
   'legacy_static'` and `status = 'archived'`, preserving its original
   definition in an immutable `legacy_static` revision.
3. Repairs legacy duplicate active rows, then creates the one-active unique
   index and immutability triggers.
4. Stops seeding a static programme for new users. The legacy
   `get_active_programme`/`set_active_programme` functions remain available only
   while current runtime consumers migrate, and `get_programme_templates()`
   continues to return an empty list.

Re-running `init_db()` on a fully or partially migrated database completes
missing tables/indexes and never creates duplicate legacy revisions.
