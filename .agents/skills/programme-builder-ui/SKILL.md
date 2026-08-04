---
name: programme-builder-ui
description: 'Build or extend the workout-programme selection/creation UI and its backend, letting a user choose a template or build a custom split instead of inheriting the hardcoded program.py schedule. Use for any /plan route or programme-related feature work.'
---

# Programme Builder UI

## Why This Exists

Per `AGENTS.md` §6/§7, there is currently no way for a user to choose or
build their own workout programme — `/plan` only renders the one hardcoded
6-day split from `program.py` read-only. This is one of the four core
product principles (§6.3), not a nice-to-have.

## Existing Building Blocks (don't rebuild these)

- **`program.py`** — the current hardcoded 12-week block-periodised split
  (`Block`, `LiftScheme`, `COACHING_RULES`). Treat this as **one selectable
  template** ("Hybrid Powerbuilding — Elgan's original"), not the only
  option, once the builder exists.
- **`programme_inference.py` + `hevy_reader.py`** — already implement
  inferring a user's actual split (PPL, upper/lower, bro split, full body,
  custom), frequency, and next-routine suggestion from their live Hevy data.
  Currently unwired (§7) — this is the natural "detect what I'm already
  doing" option in the builder, alongside manually-chosen templates. Wiring
  these in as one of the builder's programme sources is in scope for this
  skill, not a separate task.
- **`hevy_sync.py`** — pushes a chosen/generated programme's routines to
  Hevy as actual loggable routines. Any new programme source (template or
  inferred) needs to flow through the same push path so the user's Hevy app
  stays in sync with what the dashboard shows.

## Procedure

1. **Data model** — add a `programmes` table (or extend `programme_state`
   once it's migrated per `multi-tenant-migration`) storing: `user_id`,
   `source` (`template` | `inferred` | `custom`), `template_key` (if
   applicable), structured day/exercise definition (JSON is fine, matching
   the existing `workout_history.hevy_payload` JSON-blob convention), and
   `active` flag (one active programme per user at a time).
2. **Selection UI** — a new page or a new mode on `/plan`: list available
   templates (start with `program.py`'s split as the first entry), an
   "Infer from my Hevy history" option (calls `programme_inference.py`,
   shows the detected split for confirmation before activating), and a
   manual builder (day count, exercises per day, sets/reps ranges) for fully
   custom programmes. Follow the `fastapi-route` skill for the route/auth
   conventions.
3. **Switching programmes** should not silently discard history —
   `exercise_progress`/`workout_history` are keyed by exercise name and date,
   not programme id, so switching is safe by construction; just update the
   `active` programme pointer and let `programme_state.current_day` reset to
   day 1 of the new programme.
4. **Continuous adjustment** — once a programme is active, the existing
   automatic-adjustment logic (`apply_autonomous_adjustments` in
   `gemini_engine.py`, `insights.py`'s stall/regression detection,
   `checkin.py`'s periodic check-in) should operate on whichever programme
   is currently active for that user, not assume `program.py`'s hardcoded
   structure — this may require generalising those functions to take a
   programme definition parameter instead of importing `program.py`'s
   constants directly.

## Verification

Run the `verification-gate` skill's steps. Manually walk the flow in a
browser: pick a template → see it reflected in `/plan` → confirm
`hevy_sync.py` pushes routines matching the selection → confirm switching
programmes doesn't corrupt `exercise_progress` history for exercises shared
between the old and new programme.

## Gotchas

- This is a large feature — split it into small tasks (schema, selection UI,
  template source, inferred source, custom builder, sync integration) rather
  than one giant change, per `AGENTS.md` §4's "check for existing/overlapping
  work" guidance and to keep each verification-gate pass fast.
- Don't let the builder UI bypass `COACHING_RULES`-equivalent safety
  constraints (joint-friendly exercise substitutions, deload logic) — those
  need to become per-programme or per-user constraints (from
  `user_preferences.constraints`), not disappear when a user picks a custom
  programme.
