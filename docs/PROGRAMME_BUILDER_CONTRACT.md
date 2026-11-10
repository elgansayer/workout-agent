# Hevy-first programme builder contract

**Status:** Implemented
**Source issue:** #971
**Architecture:** `docs/HEVY_NATIVE_DYNAMIC_PROGRAMMES.md`

This document describes the shipped contract of the Hevy-first programme
builder: the API surface, the states the UI must render, the validation rules
that block activation, and the guarantees the builder makes about the user's
source routines.

## 1. Product rules

1. The only source of programme material is the **authenticated user's own
   Hevy account**. Routines are identified by provider ID and content hash,
   never by title.
2. The builder never renders a static programme. A user without a Hevy
   connection or without routines sees setup guidance.
3. Original Hevy routines stay read-only. The programme is a prescription
   overlay.
4. Nothing is activated while a blocking violation exists, and nothing is
   activated from a preview whose source revisions have changed.

## 2. Builder steps

| Step | Surface | Contract |
| --- | --- | --- |
| 1 | Hevy state | connection state, authenticated owner, last successful sync, stale flag, resource counts, `Refresh from Hevy` |
| 2 | Routine library | routines grouped by Hevy folder in provider order, search, folder filter, selection checkboxes, exact set/exercise preview, unsupported fields, drift status |
| 3 | Selected routines | ordered list, keyboard and pointer reordering, `required` / `optional` / `rotating` roles, rolling or weekly cadence |
| 4 | Settings | duration (4–52 weeks), start date, structured goal, priority muscles/exercises, experience, sessions per week, training days, maximum session length, progression aggressiveness, planned/adaptive fatigue management, test date, concurrent training and constraints |
| 5 | Preview | source snapshots and hashes, rotation and cadence, history coverage and confidence, inferred baselines and exercise roles, muscle-set estimates, exact programme dates, goal-specific blocks, prescriptions and progression rules, assumptions, warnings and blocking violations, engine version and preview token |
| 6 | Activation | source reconfirmation, shared validator, conflict remediation, duplicate-submit protection, transaction failures surfaced |

## 3. API

### `GET /api/programmes`

Returns the builder state.

```json
{
  "active_programme": { "...": "..." },
  "templates": [],
  "connection": {
    "state": "connected | partial | disconnected | auth_error | rate_limited | error",
    "detail": null,
    "username": "hevy-account-name",
    "owner": "signed-in-user@example.com",
    "last_successful_sync": "2026-08-20T09:00:00+00:00",
    "stale": false,
    "warnings": [],
    "counts": { "routines": 4, "folders": 2, "exercise_templates": 41, "recent_workouts": 8 }
  },
  "source": { "username": "...", "folders": [], "routines": [] },
  "duration": { "minimum_weeks": 4, "maximum_weeks": 52, "default_weeks": 12 },
  "goals": [],
  "cadence": { "modes": [], "weekdays": [], "roles": [] }
}
```

`templates` is always empty: static templates are no longer presented.

### `POST /api/programmes/refresh`

Re-reads the tenant's Hevy account and records the sync result. The response
always uses HTTP 200 so the UI can render the state:

```json
{ "state": "ok | partial | auth_error | rate_limited | error", "detail": null,
  "source": {}, "counts": {}, "warnings": [],
  "last_successful_sync": "2026-08-20T09:00:00+00:00", "stale": false }
```

A partial read never replaces the recorded last successful sync.

### `POST /api/programmes/preview`

Typed request (`ProgrammePreviewRequest`):

| Field | Type | Notes |
| --- | --- | --- |
| `selected_routine_ids` | `list[str]` | ordered Hevy routine IDs, at least one |
| `routine_selections` | `list[{routine_id, role, preferred_weekday?}]` | `required`, `optional` or `rotating` |
| `duration_weeks` | `int` | 4–52 |
| `goal` | `str` | structured goal key |
| `start_date` | `date` | programme start |
| `sessions_per_week` | `int?` | advisory, compared with the cadence |
| `cadence` | `{mode: "rolling" \| "weekly", weekdays: [int]}` | weekday 0 = Monday |
| `experience` | `str` | beginner / intermediate / advanced |
| `max_session_minutes` | `int?` | 20–240 |
| `adaptation_aggressiveness` | `str` | conservative / balanced / aggressive |
| `priority_muscles` / `priority_exercises` | `list[str]` | optional priorities |
| `event_date` | `date?` | test or competition date |
| `fatigue_management` | `str` | `planned` or `adaptive` |
| `constraints` | `{concurrent_training, injuries, notes, max_sessions_per_week}` | structured constraints |

The response is `{"preview": {...}}` with the fields listed in step 5. The
preview always contains `blocking_violations`; a preview with violations is
still returned so the user can see what to fix.

`422` is returned when the request is invalid or when a selected routine is
not part of the authenticated user's current Hevy source.

### `POST /api/programmes/activate`

Same payload plus `preview_token` and the `source_hashes` the user previewed.

* `409` — the token or the source hashes no longer match. The body carries
  `conflict` (changed, added and removed routines) and `remediation` steps.
* `422` — the preview still has blocking violations. The body carries the
  `violations`.
* `200` — the programme was activated atomically.

### `POST /api/programmes/select` (removed)

Static programme selection and the template compatibility adapter have been
removed. Clients must use `/api/programmes/preview` and
`/api/programmes/activate`.

## 4. Cadence and roles

* **rolling** — the user's routine order is preserved and the rotation
  advances one routine per training day. With every day selected this matches
  the historical daily rotation exactly.
* **weekly** — routines are placed on explicit weekdays. Required routines
  claim a weekday each, rotating routines fill the remaining days in rotation
  order, and optional routines are dropped (with a warning) when the week has
  no free day.
* A routine that can never be scheduled is a **blocking violation**.
* No Monday-to-Saturday pattern and no Sunday rest card is ever forced.

## 5. Validation

`dynamic_programme.validate_programme_preview` is the shared validator. It
runs for the preview and again for the activation.

Blocking violations:

| Code | Meaning |
| --- | --- |
| `no_scheduled_routines` | nothing is selected |
| `routine_not_scheduled` | a required routine has no training day |
| `session_limit_exceeded` | the schedule exceeds the confirmed session limit |
| `event_before_start` | the test date is before the programme start |
| `event_outside_programme` | the test date is after a strength-test programme ends |

Everything else is a non-blocking warning, for example low history
confidence, sessions longer than the maximum session length, uncovered
priority muscles, muscle-set estimates outside the reference range, a
requested frequency that differs from the cadence, and source drift against
the active programme.

## 6. Safety and tenancy

* Every route resolves the authenticated user first and reads only that
  user's Hevy account and stored programme rows.
* The source hashes are recomputed on the server at activation; a client
  cannot activate a preview for routines it did not preview.
* Activation writes the programme row and its start date in one transaction.
* Muscle-set estimates use the documented fractional model (direct set 1.0,
  secondary contribution 0.5) and are review triggers, never prescriptions.
* The engine version and the preview token make every generated programme
  reproducible and auditable.

## 7. Accessibility

* Every control has a label, the routine selection checkboxes and the role
  selector are named per routine, and the reorder buttons are named per
  routine.
* Reordering works without drag-and-drop: buttons plus `Ctrl`/`⌘` with the
  arrow keys on the selected-routine list.
* Status changes (refresh, preview, activation, conflicts) are announced
  through `role="status"` / `role="alert"` regions.
* Errors name the affected field or routine and never discard the user's
  input.

## 8. Verification

```bash
python -m compileall backend
python -m pytest -q backend/tests/test_dynamic_programme.py backend/tests/test_webapp_programmes.py backend/tests/test_programme_runtime.py
cd frontend && npm run build && npm test -- --watch=false
```
