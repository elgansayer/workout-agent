# Readiness, onboarding and Coach capability states

Every authenticated surface reports the same server-derived capability state. It is computed from
tenant-scoped facts at request time and is never persisted as a client claim.

## States

| Phase | Meaning | Next action |
| --- | --- | --- |
| `setup_required` | No training source, no synced data, or no usable AI provider. | Finish the incomplete setup step in Settings. |
| `connecting` | A source is connected but no first sync has produced data. | Wait for the first sync. |
| `syncing` | A sync is in progress or a connected source has no usable data yet. | Wait for sync to finish. |
| `ready` | Fresh training data and a server-verified AI provider are available. | Use dashboard and Coach. |
| `stale` | Data exists but the source is disconnected or the last sync is older than seven days. | Reconnect or run a Hevy history sync. |
| `degraded` | The source or provider reported an error that blocks coaching. | Open Settings and follow the reported reason. |

## Capability payload

`GET /api/status` returns the canonical payload:

```json
{
  "phase": "setup_required",
  "message": "Connect Hevy to import your routines and training history.",
  "next_action": "/settings",
  "data": {"available": false, "verified": false, "reason": "No training data source is connected."},
  "provider": {"available": false, "verified": false, "reason": "No AI provider key is configured."},
  "coach": {"grounded": false, "message": "Coach needs an AI provider and a connected training data source."},
  "setup_steps": []
}
```

The same `readiness` object is attached to `/api/dashboard`, `/api/plan`, `/api/progress`,
`/api/stats`, `/api/history`, `/api/checkins`, `/api/settings`, `/api/programmes` and
`/api/chat/history` so the UI cannot drift from the API.

## Coach honesty

- The Coach welcome copy, banners and input state follow the server payload.
- The Coach prompt includes training data only when `data.available` is true.
- Without data, the prompt explicitly tells the model it has no access and must not invent numbers.
- `GET /api/xai_reasoning/{context}` and `GET /api/project_peak` return `409` when no training
  data exists instead of producing a data-grounded answer.
- Provider availability is inspected from the authenticated user's stored preference and key, plus
  the installed SDK, without constructing an SDK client or making a network request.
