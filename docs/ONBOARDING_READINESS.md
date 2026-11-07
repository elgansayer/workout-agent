# Onboarding and account readiness

First-run onboarding is a resumable checklist backed by server state. The SPA
does not store completion locally: opening `/onboarding`, reloading the page, or
signing in from another device shows the same progress.

## Steps

| Order | Step | Required | Verified state |
| --- | --- | --- | --- |
| 1 | Account preferences | Yes | `user_preferences.goals` is non-empty |
| 2 | Connect Hevy | Yes | An encrypted `hevy` API key exists for the user |
| 3 | Add health data | No | Google Health token or a connected health connector |
| 4 | Choose an AI provider | No | The preferred provider has a key (or the server Gemini key is configured) |
| 5 | Select your programme | Yes | An active, non-empty Hevy-derived programme exists |
| 6 | Enable notifications | No | At least one push subscription is stored |

`ready` means every required step is complete. `complete` means every required
step is complete and every optional step is either complete or deliberately
skipped.

## API

- `GET /api/onboarding` returns the ordered steps, derived statuses, progress,
  `next_action`, and `next_required_action` for the authenticated user.
- `POST /api/onboarding/skip` with `{"step": "<id>"}` records a deliberate skip
  for an optional step. Required steps are rejected with `400`.

Only skips are persisted (`onboarding_progress`, keyed by `user_id` and `step`).
Completion is recomputed from verified persistence on every request, so removing
a key, disconnecting a health provider, or deactivating a programme correctly
reopens the corresponding step.

## Home page behaviour

`/api/dashboard` includes the same onboarding payload. While the account is not
ready, the dashboard renders the next required action and links to the full
checklist. The checklist can always be reached from the **Setup** navigation
entry.

## Optional consequences

Skipping an optional step is explained in the UI and returned as
`consequence` from the API:

- Health data: recovery and readiness guidance stays limited.
- AI provider: coach replies and generated insights are unavailable.
- Notifications: workout and check-in reminders are not delivered.

Every skipped step can be completed later from the checklist or Settings.
