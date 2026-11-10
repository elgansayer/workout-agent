# Log redaction, opaque errors, and protected diagnostics

Workout Agent handles credentials and health, workout, and coaching data. This page is the runtime contract that keeps that data out of logs and error pages. It enforces the "never log" rules in `docs/DATA_CLASSIFICATION.md` centrally instead of relying on every call site to remember them.

The implementation lives in:

| Module | Responsibility |
|---|---|
| `backend/log_redaction.py` | text and structure redaction, the process-wide logging hook, pseudonymised identifiers |
| `backend/data_classification.py` | the field vocabulary (what counts as credential, identifier, health, workout, prompt, analytics) |
| `backend/diagnostics.py` | opaque error references and the protected diagnostics file |
| `backend/webapp/error_boundary.py` | the web middleware and exception handlers that return opaque errors |

## What is redacted

`redact_text()` replaces the following with class-labelled markers such as `[REDACTED:CREDENTIAL]`. The same rules apply to log messages, positional and mapping arguments, `extra=` fields, exception tracebacks, stack info, HTTP error `detail` values, and diagnostics.

| Category | Examples | Rule |
|---|---|---|
| Authorization headers | `Authorization: Bearer …`, `Proxy-Authorization`, a dict/JSON `'Authorization': '…'` | header value redacted to end of line or closing quote; stand-alone `Bearer <token>` too |
| API keys | `api-key:`/`x-api-key:` headers, `api_key=…`, `?key=…`, OpenAI/Anthropic `sk-…`, Google `AIza…` | by field name and by key format |
| OAuth codes and state | `?code=…&state=…`, `code_verifier`, `{"code": "…"}`, Google `4/0…` codes | query-string/form parameters and JSON fields |
| Tokens and secrets | access/refresh/ID tokens, JWTs, GitHub tokens, Telegram bot tokens (including inside `/bot<token>/` URLs), client secrets, Fernet ciphertext, PEM private keys, `scheme://user:password@host` | by field name and by key format |
| Cookies | `Cookie:`, `Set-Cookie:`, `cookies={…}`, signed session cookies | header value redacted; signed-cookie shape recognised anywhere |
| Health fields | `resting_hr`, `hrv`, `weight_kg`, `body_fat_pct`, `sleep…`, `recovery…`, `health_payload` | value redacted whole, including nested containers |
| Workout payloads | `workouts`, `exercises`, `sets`, `hevy_payload`, `routine_*` | value redacted whole |
| Prompt payloads | `prompt`, `user_prompt`, `system_prompt`, `content`, `messages`, `reasoning` | value redacted whole |
| Identifiers | e-mail addresses anywhere in text, `user_id=`, `email=`, push `endpoint=`, `latitude=`/`longitude=` query parameters | redacted |
| Request query strings in the web server's access log | `GET /api/rag_search?q=<question>` | the whole query string is dropped, because it can hold OAuth codes and the user's question |

Field names are classified by `data_classification.classify_field()`. Registered internal fields such as `system_prompt` are redacted from logs; unregistered fields are not. Redaction is deliberately conservative: a false positive costs a little debugging context, a false negative leaks a credential or personal data.

User IDs are random UUIDs and are logged as a stable tag from `pseudonymise_identifier()` (`id:<12 hex characters>`), so an operator can correlate lines without the log carrying the identifier. To find the tag for a known user:

```bash
python3 -c "import hashlib,sys; print('id:' + hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])" <user-uuid>
```

Only use that helper for high-entropy identifiers. Low-entropy values such as e-mail addresses are redacted, never hashed.

## How it is applied

`install_log_redaction()` is idempotent and process-wide. It

- replaces the log-record factory, so every logger and every handler receives already-redacted records, including third-party and web-server loggers (`uvicorn.*`, `httpx`, provider SDKs) and handlers added later;
- wraps `Logger.makeRecord`, because `extra=` attributes are attached after the factory runs;
- routes uncaught exceptions on the main thread through logging (`sys.excepthook`) instead of the interpreter's unredacted traceback printout (production code starts no background threads);
- fails closed: a record that cannot be redacted is replaced by a fixed message rather than emitted, and a log call never raises because of redaction;
- bounds its cost: input beyond `MAX_REDACTION_CHARS` is cut off and flagged `[TRUNCATED]`, and the scanner is linear in the input size.

It is installed by every process that handles user data or credentials: `webapp` (on package import), `main.py`, `scheduler.py`, `insight_cron.py`, `scripts/sync_history.py`, and `connector_health.py`. `backend/tests/test_log_redaction.py` fails if any other backend module calls `logging.basicConfig` without installing it. The only exceptions are the repository-maintenance tools `commit_hygiene.py` and `dead_code_sweep.py`, which audit repository structure and log only file names, sizes, and commit subjects. The test suite itself runs with the layer installed (`backend/conftest.py`), so no test depends on logging behaviour production does not have.

## Rules for contributors

- Log metadata, not payloads: counts, lengths, identifiers' pseudonyms, HTTP status, exception class names. Never log a prompt, an AI response, a connector payload, a request or response body, or a header map.
- Prefer a structure with field names (`extra={"prompt": …}` or a mapping) to a bare string. Names are what the redactor classifies; an arbitrary sentence of free text can only be recognised by its shape.
- Do not interpolate exceptions from HTTP clients into log messages when the request URL or body carries a secret (for example the Telegram bot token in the URL). Log `type(exc).__name__` and the HTTP status.
- Do not `print()` errors or response text from request-handling code: `print` bypasses logging and therefore redaction. Interactive operator CLIs that intentionally show a credential to its owner (for example the one-time `google_health_auth.py` refresh-token printout) are not logs.
- Log user IDs only through `pseudonymise_identifier()`.
- Mark credential-bearing dataclass fields `field(repr=False)` (as `config.Config` does), so formatting the object into a message or exception cannot disclose them.
- A new credential format belongs in `_CREDENTIAL_SHAPES` in `log_redaction.py`, and a new sensitive field name in `FIELD_CLASSES` in `data_classification.py`, each with a test.
- A new process entry point that calls `logging.basicConfig` must call `install_log_redaction()` first (enforced by a test).

## Error responses

`webapp/error_boundary.py` is installed as the outermost application middleware (`webapp/app.py`), inside the production security-header layer (`webapp/secure_app.py`).

- **Unhandled exception** → HTTP 500 with only a generic message and an opaque reference:

  ```json
  {"detail": "An unexpected error occurred. Reference: err_0123456789abcdef", "error_id": "err_0123456789abcdef"}
  ```

  Browser page navigations (a non-`/api/` path with `Accept: text/html`) get a small HTML page with the same reference. The reference is 64 random bits; it encodes nothing about the failure, the user, or the time. The response never contains the exception class, message, stack, file path, query string, or request data. The exception is **not** re-raised, so the ASGI server does not print a second, unprotected copy.
- **Deliberate `HTTPException`** (400, 401, 404, 502, …) keeps its status, headers, and user-facing `detail`, but the `detail` passes through the redactor, so a secret echoed into a message (for example an API key pasted into the wrong field) is scrubbed.
- **Request validation (422)** returns only the location (`loc`), type, and message of each problem. FastAPI's default behaviour of echoing the rejected `input` back is removed.
- **Sign-in rejection** (`/auth`) shows a fixed "Access denied" page and logs a fixed line; neither repeats the submitted e-mail address.
- If the failure happens after a response has started (for example mid-stream), the response cannot be replaced; the failure is recorded in diagnostics and the server ends the connection.

## Protected diagnostics

Every unhandled exception is appended as one JSON line to `errors.jsonl` in the diagnostics directory:

```json
{"error_id": "err_…", "occurred_at": "…Z", "method": "GET", "route": "/api/xai_reasoning/{context_id}", "exception": "RuntimeError", "trace": "Traceback (most recent call last): …"}
```

- **Location:** `DIAGNOSTICS_DIR`, or a `diagnostics` directory beside `DATABASE_PATH` (in the Docker deployment: `./data/diagnostics/` on the host). It is documented in `.env.example`.
- **Protection:** the directory is created `0700` and the file `0600` (tightened if it already existed with looser permissions), written without following symlinks. Keep the volume readable only by the service account and the operators who may see failure detail.
- **Content:** the trace is passed through the redactor before it is written, so even this sink never holds raw credentials or personal data. Only the route *template* is recorded, never the concrete URL, query string, headers, body, or any user identifier. A trace is capped at 32,000 characters.
- **Rotation:** at 5 MiB the file is renamed to `errors.jsonl.1` (replacing any earlier one), so at most about 10 MiB is kept. Delete the files at any time; they are diagnostics, not records.
- **Lookup:** a user quotes the reference; the operator runs

  ```bash
  grep -h 'err_0123456789abcdef' data/diagnostics/errors.jsonl* \
    | python3 -c 'import json,sys; [print(json.loads(l)["trace"]) for l in sys.stdin]'
  ```

- **The general application log** gets one line per failure (`error_id`, method, route template, exception class) and no traceback or message, so it can be searched by reference without carrying the detail.
- **If the sink cannot be written** (read-only volume, full disk), the redacted trace is emitted to the server log instead so the failure is not lost; the user still sees only the opaque reference.

Do not forward `errors.jsonl` to a third-party log service without the same access controls: although redacted, a trace still describes the system's internals.

## Limits and operator responsibilities

- The redactor recognises sensitive values by field name and by credential format. A bare sentence of user text logged with no field name beside it cannot be recognised, which is why call sites must log metadata rather than payloads.
- The reverse proxy and any CDN keep their own access logs. They record request URLs including query strings, such as the OAuth callback (`/auth?code=…`, `/google-health/callback?code=…`) and the coach endpoint (`/api/rag_search?q=…`). Configure them not to log query strings (for nginx, a `log_format` using `$uri` instead of `$request`/`$request_uri`; for Apache, `%U` instead of `%r`) or to redact them, and restrict who can read them. Moving the coach question from a query parameter to a request body would remove prompts from URLs altogether and is tracked as follow-up work.
- Container runtime logs (`docker compose logs`) contain what the processes write to stdout/stderr. With the layer installed that is redacted application logging; output from other programs in the container is outside its control.

## Verification

```bash
cd backend
python -m pytest -q tests/test_log_redaction.py tests/test_error_boundary.py \
  tests/test_secret_leak_regressions.py tests/test_data_classification.py
python scripts/check_data_policy.py
```

- `test_log_redaction.py`: representative secrets in every category (authorization headers, API keys, OAuth codes, tokens, cookies, health fields, prompt and workout payloads, identifiers) never survive; ordinary operational text is untouched; property tests for stability and for planted secrets; real logging pipelines including uvicorn's access formatter, tracebacks and `extra=`; bounded-time checks on pathological input; the entry-point enforcement test.
- `test_error_boundary.py`: opaque 500 JSON/HTML, scrubbed `HTTPException` and validation responses, private diagnostics (permissions, content, rotation, fallback), ASGI containment semantics, the real application, and the production entry point with its security headers.
- `test_secret_leak_regressions.py`: one test per known leak site (Telegram, Web Push, Google OAuth exchange and refresh, weather, AI provider failures, insight generation, scheduler and agent user IDs) that injects synthetic secrets and inspects the captured logs.
