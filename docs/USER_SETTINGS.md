# Per-user regional and accessibility settings

Every account stores its own time zone, locale, unit system and accessibility
preferences. They replace the deployment-wide `TZ` clock, the hard-coded
`en-GB` interface locale, the fixed "Use British English" prompt rule and the
kilogram-only displays. Everything is tenant scoped: each read and write is keyed
by the authenticated session, and no request body can name an owner.

Code: `backend/localisation.py` (pure rules), `backend/user_settings.py`
(contract, validation, persistence), `GET/POST /api/settings/display`, and
`frontend/src/app/services/display-settings.ts`.

## Explicit defaults

| Setting | API field | Default | Accepted values |
| --- | --- | --- | --- |
| Time zone | `timezone` | `UTC` | any IANA zone known to the host (`zoneinfo`) |
| Locale | `locale` | `en-GB` | `en-GB`, `en-IE`, `en-US`, `en-CA`, `en-AU`, `en-NZ` |
| Unit system | `unit_system` | `metric` | `metric`, `imperial` |
| Motion | `motion` | `system` | `system`, `reduce` |
| Contrast | `contrast` | `system` | `system`, `more` |
| Text size | `text_size` | `default` | `default`, `large`, `x-large` |

The interface copy is English, so the supported locales are the English
variants whose spelling and date/number conventions we can honour. A locale such
as `fr-FR` or `xx` is rejected on write (`400`, listing what is supported). A
value that is stored but no longer supported (for example edited by hand) reads
back as the default and is repaired by the next save; the repair is logged by
field name only.

The accessibility choices only ever add an accommodation: `system` defers to the
operating-system media queries, so a stored choice can never weaken a request
the device already makes. Because they can reveal a condition, `motion`,
`contrast` and `text_size` are classified as health-adjacent
(`data_classification.FIELD_CLASSES`): never logged by value, exportable by the
owner. `timezone`, `locale` and `unit_system` are identifiers.

## Storage

The preferences live on the existing `users` row, which already carried
`timezone` and `units` (the API's `unit_system`):

- `users.locale`, `users.motion`, `users.contrast`, `users.text_size` are added
  additively and idempotently by `user_settings.ensure_settings_schema`, called
  from `init_db` inside its transaction (`PRAGMA table_info` guarded).
- `exercise_progress.performed_at` stores the immutable UTC instant a workout
  started (see "Time-zone changes").
- The first time those columns appear on an existing database, accounts that
  still hold the untouched `UTC` default inherit the deployment `TZ` they were
  already being scheduled in, so the upgrade does not silently move anyone's
  daily run. Explicit choices are never overwritten, a re-run does nothing, and
  fresh databases are never seeded from the environment.

No new table is introduced, so no new tenant-owned table or index is required.

## Time-zone changes

Source timestamps are UTC instants and are never rewritten. A time zone only
changes *derived local-day groupings*:

- `exercise_progress.performed_at` keeps the start instant; `date` is the
  workout's calendar day in the owner's zone, derived from it when saved.
- `POST /api/settings/display` with a new `timezone` re-derives `date` for that
  user's rows that have a `performed_at`, in the same transaction as the
  preference change. The response reports `regrouped_days`.
- `hevy_payload`, `performed_at`, every metric, other users' rows and rows that
  were entered as a bare calendar day are untouched. Changing back restores the
  previous groupings exactly.
- Everything else derived from time (programme rotation, `local_today`, streaks,
  statistics windows, check-in dates, job schedules) is computed at read time
  from the saved zone, so it follows a change immediately.
- `workout_history.date` is only an ordering key (UTC), not a grouping that any
  page displays; the programme runtime derives local days from the raw payload's
  timestamps when it reads them, so there is nothing stored there to recompute.
- Calendar days entered for a day rather than an instant (`daily_log`,
  `body_metrics`, `check_ins`) record the user's local day when they were saved and
  have no source instant, so they are history rather than derived groupings and are
  left as recorded.

`localisation.local_day_bounds` and `local_day_hours` expose the real length of a
local day (23, 23.5, 24, 24.5 or 25 hours), and the tests cover New York,
London, Lord Howe (30-minute shifts) and Havana (a skipped local midnight).

## Units

Weights are always stored in kilograms. The unit system only changes how a value
is shown or typed:

- Imperial display is `kg / 0.45359237`, rounded to 0.1 lb; metric values are
  never altered or rounded.
- The smallest suggested load increase is 2.5 kg or 5 lb.
- `localisation.weight_input_to_kg` converts a typed pound value back to
  kilograms for any future input path.

## Where the settings are used

| Consumer | Behaviour |
| --- | --- |
| Pages | Dashboard, Progress and Stats receive values, chart labels and `weight_unit` already converted to the owner's units; the SPA formats dates and numbers with the owner's locale and zone (`localDate` / `localNumber`), applies `lang`, motion, contrast and text size to `<html>`, and never shifts a bare calendar day between zones. |
| Jobs | `scheduler.py` fires coaching, the daily insight and the weekly report at `RUN_AT` / `INSIGHT_*_AT` in **each user's own zone**, once per user per local day, including across DST. The daily run, self-review weekday, check-in dates and insight windows use the user's local day. |
| Notifications | Plan, rest-day and check-in text is generated from prompts carrying the owner's units, locale and zone, and deterministic review/check-in text is formatted in the owner's unit system. Stored notification timestamps stay UTC and the SPA localises them. |
| Exports | `HealthExport` carries `user_settings.export_preferences` (zone, locale, unit system, `weight_unit`, `stored_weight_unit: kg`, timestamp convention) next to unchanged source data, and the preference fields survive `sanitize_for_export`. No user-facing export route exists yet; the account export consumes this envelope. |
| AI prompts | `localisation.prompt_context` tells the model the owner's zone, local date, spelling and how to present weights; the fixed "British English" rule was removed from `COACHING_RULES` in favour of the owner's locale. Data stays in kilograms and UTC, and model output is never trusted to change stored values. |

`GET /api/settings/display` returns `settings` and the accepted `options`
(including every supported zone), so clients never hard-code the lists.
`POST /api/settings/display` is a partial update; omitted fields keep their
value, unknown fields (including any owner identifier) are rejected, and a bad
value rejects the whole update.

## Verification

```bash
cd backend
python -m pytest -q tests/test_localisation.py tests/test_user_settings.py \
  tests/test_settings_api.py tests/test_scheduler_timezones.py \
  tests/test_regional_consumers.py
cd ../frontend
npm run test:a11y-preferences
npm test -- --watch=false
```
