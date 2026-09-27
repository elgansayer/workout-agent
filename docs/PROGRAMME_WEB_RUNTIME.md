# Programme dashboard and plan runtime

This is the web-runtime slice of #976. Dashboard and Plan share
`programme_runtime.resolve_programme_runtime` and read only the authenticated
user's active programme and persisted Hevy workout history.

- The next workout follows the latest synced completion in the saved routine
  order. Passing a calendar day does not skip an uncompleted session.
- A provider routine ID is authoritative when present. Older payloads without
  that relation require an exact, unique exercise-template signature. Workout
  titles never establish identity. Ambiguous or unrelated completions display
  a review state instead of guessing the next prescription.
- Week and block use the user's stored timezone and programme start date.
  Legacy definitions without a start date use the saved activation timestamp,
  never a moving "today" fallback.
- Exercise sets, rep ranges and other prescription fields come from the current
  block. The saved source definition and completed history remain unchanged.
- Not-started and completed programmes do not present a due workout. Missing
  programme data produces setup or review guidance.
- The dashboard refreshes when the app regains focus or becomes visible and
  also provides an explicit refresh button.

The runtime does not call Hevy on page views. Its message and completion time
describe the latest persisted history. If a workout has been logged in Hevy
but not ingested, sync history in Settings; refreshing the dashboard reads the
database but does not perform a provider sync. With no synced completion the
first routine remains next, with an explicit explanation.

The wider #976 work remains open: scheduled coaching still has legacy static
programme consumers, and durable exposure/adaptation records and full calendar
scheduling are not implemented by this change. The screen deliberately labels
the result "Next workout" rather than claiming a mandatory workout today.

Verification includes synthetic tests for completion order, duplicate syncs,
missed days, renamed routines, ambiguous matches, current-block targets,
immutable snapshots, programme boundaries, timezone rollover, and two-user API
isolation. No live account credentials are needed for these tests.
