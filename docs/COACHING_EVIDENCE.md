# Coaching evidence and provenance

Coach distinguishes observations from inference. Every personalised reply is
built from a deterministic evidence bundle before the AI provider is called.

## Retrieval contract

`backend/coaching_evidence.py` builds a tenant-scoped bundle from records already
read with the authenticated user's ID. Each item records:

- a stable evidence ID (`E1`, `E2`, ...) used for citations;
- the source and source record ID (`daily_log:12`, `exercise_progress:44`, ...);
- the source record date;
- freshness relative to the request time (`fresh`, `aging`, `stale`, `unknown`);
- the transformation version for that source (`daily-log-v1`,
  `exercise-progress-v1`, `epley-e1rm-v1`, `body-metrics-v1`);
- a short human-readable summary.

Records outside the requested window, dated in the future, or belonging to a
different user are excluded before the prompt is built. The bundle itself carries
the context transformation version (`coaching-context-v1`).

## Response contract

`GET /api/rag_search` streams Server-Sent Events:

- `evidence`: the evidence bundle (`window_start`, `window_end`, `items`);
- `delta`: answer text, chunked;
- `status`: `grounding` (`observed`, `inferred`, `incomplete`, `unknown`) plus
  the cited evidence IDs;
- `done`: end of stream.

The model is instructed to cite evidence inline as `[E1]`. Any citation that is
not in the authenticated, time-bound bundle is stripped before the reply is
streamed or stored. The reply is labelled `observed` when it cites valid
evidence, `inferred` when evidence exists but nothing is cited, `unknown` when
no records match, and `incomplete` when no answer was produced.

Chat history persists the bundle alongside the assistant message so the same
evidence and grounding label render after a reload.

## Optional time window

`/api/rag_search` accepts `start`/`end` (aliases: `start_date`/`end_date`) as
ISO dates. The default window is the last 90 days. A future end date is clamped
to today, and `start` after `end` returns HTTP 400.
