# Log redaction and opaque error responses

Connector payloads, request headers, prompts, forms and exception messages can
all reach the log stream or an HTTP response. A provider SDK or HTTP library will
put a bearer token, an API key in a URL or an OAuth authorisation code into an
exception message without being asked. This page is the contract for how the
application keeps those values out of logs and error responses, and for where the
detail goes instead.

Code: `backend/log_redaction.py` (the central layer),
`backend/webapp/error_responses.py` (error responses and diagnostics),
`backend/data_classification.py` (what counts as sensitive),
`backend/scripts/check_data_policy.py` (static enforcement).

## What is redacted

`install_log_redaction()` replaces the process-wide `LogRecord` factory, so it
covers **every** record whatever created it: application loggers, `uvicorn`
(including its access log), `httpx`, `authlib`, `urllib3` and any other library.
Nothing has to opt in, and no handler or formatter has to be configured.

| Surface | Handling |
| --- | --- |
| Positional and `%(name)s` arguments | Structures (dicts, lists, tuples) are redacted by field classification; strings by pattern. Numbers, booleans and `None` keep their type, so `%d` still works. Arity is preserved because some formatters (uvicorn's access formatter) read `record.args` directly. |
| Rendered message | Redacted again when the message is formatted, so a `key=value` pair split across the template and its argument is still caught. |
| Tracebacks and stack text | Rendered once, redacted, and cached on the record, so no standard formatter can re-render the raw exception. Exception messages and `raise ... from` chains are redacted. The source line of each frame is left intact: it is code, and redacting it makes the trace useless to the operator it is kept for. |
| Failure of the redactor itself | The message is withheld (`[log message withheld: redaction failed]`). It never falls back to the raw text. |

### Patterns and fields

| Class | Removed |
| --- | --- |
| Authorization headers | `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`, `Api-Key`, `X-Auth-Token`, `X-Access-Token`, `X-Goog-Api-Key`, CSRF tokens (rest of the line); `Bearer <token>` anywhere; `user:password@` in URLs |
| API keys and tokens | OpenAI / Anthropic / DeepSeek `sk-…`, Google `AIza…`, `ya29.…`, `1//…`, `GOCSPX-…`, Google authorisation codes `4/…`, GitHub `gh?_…` / `github_pat_…`, Slack `xox?-…`, AWS `AKIA…`, Telegram bot tokens, JWTs, PEM private-key blocks |
| OAuth codes and state | The **value of every URL query or form parameter** (`?code=…&state=…`, `#access_token=…`); names are kept so the route stays readable. A bare `code=` / `state=` pair is removed unless the value is plainly a status (`code=404`, `state=syncing`). |
| Credentials by key name | The value of any `key=value`, `"key": value` or dict-repr pair whose key is a credential field (`api_key`, `*_token`, `secret`, `password`, `authorization`, `cookie`, `private_key`, `csrf`, `signature`, …), including nested JSON and escaped JSON |
| Health, workout and prompt fields | Values keyed by health fields (`weight_kg`, `resting_hr`, `hrv`, `sleep`, `heart_rate`, `steps`, …), workout fields (`workout`, `exercise_name`, `routine_*`, `sets`, …) and prompt payloads (`prompt`, `messages`, `contents`, `reasoning`, `content`, …). An unquoted `prompt=` value runs to the end of the line. |
| Personal data | E-mail addresses; values of `email`, `display_name`, `ip_address`, `endpoint`, `device_id`, `subject` pairs; structured `user_id`/identifier fields |
| Known secrets | The exact value (and its URL-encoded forms) of every secret the vault decrypts or accepts, and of every secret-bearing environment variable (`*_SECRET`, `*_TOKEN`, `*_PASSWORD`, `*_API_KEY`, `ENCRYPTION_KEY`, `*_HMAC_KEY`, `*_PRIVATE_KEY`), wherever and however they appear. This catches credentials with no recognisable shape. |

Classification is delegated to `data_classification.py`, the single source of
truth, with a small set of extra whole-token names for connector, OAuth and LLM
payloads. Redaction is idempotent and linear in the length of the input.

### What is deliberately not redacted

Opaque identifiers the operator correlates on: account ids (`account=<id>`),
request ids, audit `event_hash` values and UUIDs. Free-form prose that carries no
key name and no recognisable shape cannot be recognised: **a call site must still
not log a prompt, a model response or a provider body.** The static check below
rejects the common ways of doing that, and the call sites that used to
(`insight_cron` logged an unusable AI response, `push_notifier` logged the push
service's reply) now log a size or a status code only.

### The known-secret registry

`register_secret()` keeps the exact value of each secret the vault decrypts or
accepts (and of each secret-bearing environment variable) so it is masked in any
later log line, including its URL-encoded forms. It is process memory only,
bounded to 256 values (oldest evicted first), never written anywhere, and ignores
values shorter than eight characters.

It is applied to **logs and audit metadata only**. It is never applied to text
returned to a client (`redact_text(..., known_secrets=False)` in
`webapp/error_responses.py`): the registry is shared by every tenant, so masking a
registered value in a response would let a caller who echoes a guess back learn
whether that guess is another account's live credential. Shape-based redaction is
a pure function of the text and has no such effect.

## Error responses

`webapp/error_responses.py` is installed on the FastAPI application (inside the
request-correlation middleware, so every response carries `X-Request-ID`) and
around the outer layers of the production entry point (`webapp.secure_app`).
Behaviour is identical in every environment: a misconfigured `APP_ENV` cannot
turn detailed errors on.

| Situation | Client receives | Diagnostics log receives |
| --- | --- | --- |
| Unhandled exception | `500` `{"detail": "An internal error occurred.", "error_id": "err_<16 hex>"}` (a minimal HTML page with the reference for browser navigation outside `/api/`); `Cache-Control: no-store` | One `server_error` record with the traceback |
| `HTTPException` ≥ 500 | The curated `detail`, scrubbed of secret-shaped text, plus an `error_id` | The same record, including the `raise … from` cause that was previously dropped |
| `HTTPException` < 500 | Unchanged shape; `detail` scrubbed of secret-shaped text (structured details keep their keys) | Nothing |
| Validation error (`422`) | `loc`, `type` and `msg` only. The submitted `input`, the validator `ctx` and the documentation `url` are **not** returned, so a credential, prompt or health value in a rejected request is never echoed back | Nothing |
| Failure after the response has started streaming | The stream is aborted by the server | A correlating record (no duplicate trace; the server logs it) |

The `error_id` is random (`secrets.token_hex(8)`); it encodes nothing about the
failure. A client-supplied `X-Request-ID` is still ignored.

## Protected diagnostics

The detailed trace is written **only** to the `workout_agent.error_diagnostics`
logger, as one `ERROR` record per failure:

```
server_error error_id=err_9f2c41d07a3b55e1 request_id=3f1c… status=500 method=GET path=/api/stats exception=RuntimeError
Traceback (most recent call last):
  …
```

That log stream is the protected diagnostics channel. It is never reachable over
HTTP (there is deliberately no diagnostics endpoint for traces: tenants must not
read another tenant's failures, and there is no administrative role yet), it has
already passed through the redaction layer, and the path is escaped so a hostile
URL cannot forge extra log lines. Treat it as sensitive operational data:
restrict who can read `docker compose logs web agent`, ship it only to a
restricted store, and apply the shortest retention that fits incident response.
Route the logger by name if you want it in a separate, more tightly controlled
sink.

To investigate a report, ask the user for the `error_id` shown in the response
(or the `X-Request-ID` header) and search the logs:

```bash
docker compose logs --since=24h web | grep -A40 'error_id=err_9f2c41d07a3b55e1'
```

The `request_id` in the record is the same value recorded in security-audit rows
(`docs/SECURITY_AUDIT_LOGGING.md`), so the two can be joined.

## Where it is installed

`install_log_redaction()` is called by every process that configures logging, and
the static check fails CI if one does not:

| Process | Where |
| --- | --- |
| Web (uvicorn) | `webapp/__init__.py` (first import) and `webapp/app.py` (again, after `.env` has been loaded) |
| Agent / scheduler | `main.py`, `scheduler.py`, `insight_cron.py`, `scripts/sync_history.py` |
| Maintenance tools | `connector_health.py`, `commit_hygiene.py`, `dead_code_sweep.py` |

A new entry point that calls `logging.basicConfig` / `dictConfig` / `fileConfig`
must call `install_log_redaction()` next to it.

## Static enforcement

`backend/scripts/check_data_policy.py` (run by the *Data classification policy*
workflow and the backend tests) fails when:

* a logging call passes a credential- or prompt-named value directly
  (wrap it in `safe_log_value()`, `redact_for_log()` or `redact_text()`);
* a logging call passes a raw HTTP response body (`response.text`,
  `exc.response.json()`, …) — log the status code instead;
* a non-test module configures logging without `install_log_redaction()`.

`test_log_redaction.py` and `test_data_classification.py` are the only test
files allowed to log synthetic secrets directly, because proving the redaction
requires doing so.

## Residual risk and deferred scope

* Unrecognisable free text is not redacted (see above). Fix the call site.
* The registry keeps decrypted secrets in process memory for longer than the vault does (bounded, never persisted). A memory disclosure of the web process already exposes the credentials being used; it does not widen that.
* The web server's access log still records the client address and the request
  path. Query values are removed; the address is whatever the reverse proxy
  presents (the container runs with `--no-proxy-headers`). Disable the access log
  (`uvicorn --no-access-log`) or pseudonymise addresses at the proxy if that is
  not acceptable for your deployment.
* A reverse proxy in front of the application writes its own access log, including `/auth?code=…&state=…`. Configure it to omit the query string (see `docs/REVERSE_PROXY_SECURITY.md`).
* `LogRecord` `extra=` attributes are attached after the record factory runs and
  are not redacted. No handler in this application renders them; do not put
  sensitive values in `extra`.
* Redaction protects what the process writes. Logs already written before this
  layer existed, container runtime logs, backups and any external sink must be
  reviewed by the operator under `docs/OPERATIONS_RUNBOOKS.md`.
* The browser does not yet display the `error_id`; it is in the response body and
  the `X-Request-ID` header for support requests.

## Verification

```bash
.venv/bin/python -m pytest -q backend/tests/test_log_redaction.py \
  backend/tests/test_log_redaction_web.py backend/tests/test_error_responses.py \
  backend/tests/test_data_policy_check.py
.venv/bin/python backend/scripts/check_data_policy.py
```
