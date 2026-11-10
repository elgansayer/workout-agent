# Record provenance and lineage

Users and the coaching system need to know where each stored value came from and whether it is authoritative. Workout Agent therefore records provenance for core records and keeps corrections as lineage instead of overwriting history.

The executable contract is `backend/provenance.py` (domain types and projections) and `backend/provenance_ledger.py` (the append-only SQLite ledger). `backend/database.py` wires both into the record writers and readers.

## The three kinds of record

| Kind | Meaning | Authoritative | Typical source |
|---|---|---|---|
| `imported` | Source data received from an external provider. | Yes, for what the provider reported | `hevy`, `google_health`, `health_connect_file` |
| `user_entered` | A value the user typed or corrected. | Yes, and it outranks the import it corrects | `manual` |
| `derived` | A summary or recommendation produced by this application, by rules or by an AI model. | No: never evidence of what the user did or measured | `ai:<vendor>/<model>`, `rules:<name>` |

The pairing of kind and source is validated, and each record family accepts only the kinds that make sense for it (`ALLOWED_KINDS`). AI output can therefore never be stored as source data, and an import can never pose as a recommendation. A writer that does not declare a source is stored as `unattributed`, which the UI renders as "source not recorded"; the system never guesses a source.

## What is stored

Every revision of a record in `record_provenance` stores:

| Field | Meaning |
|---|---|
| `source` | Stable lower-case identifier of the provider, `manual`, `ai:…`, `rules:…` or `unattributed`. |
| `external_id` | The provider's identifier for the record when it has one (for example the Hevy workout id). |
| `source_timestamp` | When the source says the value was observed or created (UTC; date precision is kept when only a date is known). |
| `ingested_at` | When this application first stored the revision (UTC). An identical re-import from the same source does not create a new revision, so it does not move this time. |
| `transformation_version` | Version label of the code that transformed the source into the stored value (`hevy-parser/1`, `google-health-body/1`, `daily-plan/1`, `manual-entry/1`, …). Bump it when the transformation changes. |
| `confidence` | 0 to 1, only where a producer supplies one; otherwise null. |
| `kind`, `revision`, `applied`, `supersedes_id`, `held_by_id` | Authority and lineage links (below). |

Missing or unparseable source metadata never blocks ingestion: the field is left null rather than invented.

### Record families

| Family (`record_type`) | Kind | Logical key | Source recorded by |
|---|---|---|---|
| `workout_history` | imported | Hevy workout id (else date and content hash) | `save_workout` |
| `exercise_progress` | imported | Hevy workout id (else date and content hash) | `save_progress` |
| `body_metrics` | imported or user_entered | date | `main.run` (Google Health or Health Connect export), `correct_body_metrics` |
| `daily_log` | derived | date | `main.run` (AI model, or `rules:fallback` when the baseline plan was used) |
| `check_ins` | derived | check-in number | `checkin.record` |
| `dashboard_insights`, `deep_correlations` | derived | `current` | `insight_cron` |
| `reasoning_logs` | derived | context id | the explanation route |

Programmes are not ledger revisions. A programme row is already an immutable, versioned record: a new preview token creates a new row and the previous one stays inactive. `provenance.programme_view()` projects the fields it stores (engine version as the transformation version, the newest Hevy routine edit as the source timestamp, the preview identifier as the external id, the creation time as the ingestion time and the engine's qualitative `confidence_label`) into the same view as a derived record from `rules:hevy-programme`. No numeric confidence is invented from the engine's high/medium/low level.

Chat messages are already distinguished by their `role` (user entry or assistant reply) and are not part of the ledger (see "Deferred").

## Lineage: corrections do not overwrite history

Each logical record `(user_id, record_type, record_key)` is a chain of revisions:

* A new value appends a revision whose `supersedes_id` points at the previous *applied* revision. Rows are never updated (a database trigger rejects `UPDATE`), so the chain is trustworthy.
* Body-metric revisions retain their values and per-field sources, so replacing a reading, whether by a later import or by a user correction, keeps the earlier values retrievable. Other record families keep the revision metadata only; their superseded text is not retained.
* Re-importing identical content from the same source adds no revision (history rebuilds are idempotent).
* **A user entry outranks an import.** If a user-entered revision is current, a later automated import for the same record is stored as a *held* revision (`applied = 0`, `held_by_id` set) and the stored row is left untouched. Repeated identical imports are not duplicated. A further user correction supersedes the last applied revision.
* A record stored before provenance tracking has no revision and reads as "source not recorded". For body metrics, the first replacement or correction captures the original reading as an `unattributed` legacy baseline (`legacy/0`) so the only copy of the original value is not lost.

### Correcting a body metric

`POST /api/body-metrics/corrections` with `{"date": "YYYY-MM-DD", "weight_kg"?, "body_fat_pct"?, "muscle_pct"?, "resting_hr"?, "hrv"?, "reason"?}`:

* the owner is always the authenticated session; owner claims in the body are rejected;
* only the listed fields are merged into the day's reading; values must be finite numbers inside plausibility bounds (`BODY_METRIC_LIMITS`), the date cannot be in the future, and a correction that changes nothing is rejected;
* the result is a `user_entered` revision superseding the previous one; for a day with no reading it is recorded as a manual entry (not labelled a correction);
* the response contains the merged reading, the new provenance and the full lineage.

`GET /api/provenance/{record_type}/{record_key}` returns the authenticated user's revisions newest first. Unknown families, unsafe keys and other users' records all return the same 404.

## Where provenance is surfaced

* **UI.** `<app-provenance-badge>` labels records on History (daily plans), Dashboard (Coach status and the active programme), Plan (the active programme), and Progress (body readings, with a correction form and a per-reading history, and the "Why did this happen?" explanation). Records without provenance are labelled "Source not recorded" rather than assumed to be verified. The text label carries the meaning; colour only reinforces it.
* **AI retrieval.** Coach (`/api/rag_search`), the daily insight and the weekly correlation prompts annotate every body reading and daily plan with a compact view (`origin`, `source`, `authoritative`, `as_of`, `corrected_by_user`, `confidence`) and include `RETRIEVAL_GUIDANCE`, which tells the model to treat `imported` and `user_entered` records as facts, `derived` records as recommendations, and `unrecorded` records as unverified. External identifiers, correction reasons and version labels are not sent to the model.
* **Generated text.** Generators return `GeneratedText`, a `str` that records which AI model (or the deterministic fallback) produced it, so a fallback plan is never attributed to an AI provider.

## Tenant isolation, privacy and retention

* `record_provenance.user_id` references `users(id)` and leads the unique index; every ledger query filters on it, and ledger writes require an owner.
* Field classes are registered in `backend/data_classification.py`: `external_id` is an identifier, `retained_content` is health data, `lineage_note` (the user's free-text reason) is prompt-class text, and `confidence` is analytics. None are logged raw.
* Retained body-metric values and correction reasons are user-owned data. Account export, deletion and connector purge flows must include `record_provenance` for the owner; nothing in the ledger is shared across users.
* The migration is additive and idempotent (`CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS` in `init_db`).

## Deferred

* Per-message provenance for chat, and the canonical `health_*` tables (integer user ids, not yet connected to a production connector lifecycle).
* A "revert to source" action and field-level history views beyond body metrics.
* Retaining superseded text of derived records.

## Verification

```bash
.venv/bin/python -m pytest -q backend/tests/test_provenance.py backend/tests/test_provenance_ledger.py \
  backend/tests/test_provenance_api.py backend/tests/test_provenance_ingestion.py
```
