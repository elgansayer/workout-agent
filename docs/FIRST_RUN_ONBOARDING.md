# First-run onboarding

Workout Agent derives a resumable first-run setup sequence from the signed-in
tenant's own records. The browser never reports a step as complete; the server
reads the same stores the rest of the product uses.

## Steps

| Step | Required | Verified from |
| --- | --- | --- |
| `preferences` | no | a saved `user_preferences` row |
| `hevy` | yes | a stored Hevy API key |
| `health_data` | no | a `connected` health connection |
| `ai_provider` | no | the tenant's provider key or the server Gemini fallback |
| `programme` | yes | an active programme |
| `notifications` | no | at least one push subscription |

Optional steps can be skipped. The skip is persisted per tenant in
`onboarding_skips`; it is not stored in the browser. A later verified completion
always wins over an older skip. Required steps (`hevy` and `programme`) cannot be
skipped.

`next_step` is the first pending required step, so the home page always offers
the highest-value action. Once the required steps are complete the account is
`account_ready`; remaining optional work is still listed on the setup page.

## API

- `GET /api/onboarding` returns `steps`, `next_step`, `account_ready`,
  `completed_count`, `skipped_count`, `total_steps` and `progress_percent`.
- `POST /api/onboarding/skip` with `{"step": "<id>"}` records a skip for an
  optional step and returns the new state. Skipping a required or unknown step
  is a `400`.
- `POST /api/onboarding/resume` with `{"step": "<id>"}` removes a skip.

All three routes require the authenticated session. The owner is never accepted
from the request body, and every read or write is filtered by that session's
`user_id`.

The dashboard payload carries the same onboarding state so the home page can
show the next action before the account is ready. The setup page is `/onboarding`.

## Data lifecycle

`onboarding_skips` is a user-owned table. It is included in account erasure,
legacy-data adoption and the ownerless-write guards like every other
tenant-scoped record.
