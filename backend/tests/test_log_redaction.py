"""Central log/diagnostic redaction: representative secrets must never survive.

Every credential below is synthetic. Values are assembled from fragments so
that no complete provider-shaped token literal sits in the repository.
"""

from __future__ import annotations

import ast
import io
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from synthetic_secrets import (
    ANTHROPIC_KEY,
    BASIC_CREDENTIALS,
    CLIENT_SECRET,
    EMAIL,
    FERNET_TOKEN,
    GEMINI_KEY,
    GITHUB_TOKEN,
    GOOGLE_ACCESS,
    GOOGLE_CODE,
    GOOGLE_REFRESH,
    HEVY_KEY,
    JWT_TOKEN,
    OPENAI_KEY,
    SESSION_COOKIE,
    SYSTEM_PROMPT,
    TELEGRAM_TOKEN,
    USER_PROMPT,
    VAPID_PRIVATE_KEY,
)

from data_classification import safe_log_value
from log_redaction import (
    MAX_REDACTION_CHARS,
    REDACTED_CREDENTIAL,
    REDACTED_IDENTIFIER,
    TRUNCATION_NOTICE,
    install_log_redaction,
    pseudonymise_identifier,
    redact_text,
    redact_value,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def restore_excepthook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)


# ---------------------------------------------------------------------------
# Representative secrets per category (acceptance criterion 1)
# ---------------------------------------------------------------------------

AUTHORIZATION_SAMPLES = [
    (f"Authorization: Bearer {JWT_TOKEN}", [JWT_TOKEN]),
    (f"authorization: Basic {BASIC_CREDENTIALS}", [BASIC_CREDENTIALS]),
    (f"Proxy-Authorization: Bearer {OPENAI_KEY}", [OPENAI_KEY]),
    (f"headers={{'Authorization': 'Bearer {OPENAI_KEY}', 'Accept': 'json'}}", [OPENAI_KEY]),
    (f'{{"Authorization":"Bearer {GOOGLE_ACCESS}","x":1}}', [GOOGLE_ACCESS]),
    (f"retrying with Bearer {GOOGLE_ACCESS} after 401", [GOOGLE_ACCESS]),
]

API_KEY_SAMPLES = [
    (f"api-key: {HEVY_KEY}", [HEVY_KEY]),
    (f"x-api-key: {ANTHROPIC_KEY}", [ANTHROPIC_KEY]),
    (f"GEMINI_API_KEY={GEMINI_KEY} loaded", [GEMINI_KEY]),
    (f"calling https://example.invalid/v1/models?key={GEMINI_KEY}&alt=json", [GEMINI_KEY]),
    (f"Incorrect API key provided: {OPENAI_KEY}. You can find yours", [OPENAI_KEY]),
    (f"{{'api_key': '{HEVY_KEY}', 'model': 'x'}}", [HEVY_KEY]),
    (f"client_secret={CLIENT_SECRET}&grant_type=refresh_token", [CLIENT_SECRET]),
    (f"hevy_api_key = {HEVY_KEY}", [HEVY_KEY]),
]

OAUTH_CODE_SAMPLES = [
    (f"GET /auth?code={GOOGLE_CODE}&state=abc123state&scope=openid HTTP/1.1", [GOOGLE_CODE, "abc123state"]),
    (
        "GET /google-health/callback?state=Zm9yZ2VkU3RhdGU&code=4%2F0AXencodedcode_0123456789",
        ["Zm9yZ2VkU3RhdGU", "4%2F0AXencodedcode_0123456789"],
    ),
    (
        f"body: grant_type=authorization_code&code={GOOGLE_CODE}&redirect_uri=https://x.invalid/cb",
        [GOOGLE_CODE],
    ),
    (f'{{"code": "{GOOGLE_CODE}", "grant_type": "authorization_code"}}', [GOOGLE_CODE]),
    (f"unexpected token response for code {GOOGLE_CODE}", [GOOGLE_CODE]),
    ("callback ?code_verifier=SyntheticPkceVerifier_0123456789&x=1", ["SyntheticPkceVerifier_0123456789"]),
]

TOKEN_SAMPLES = [
    (f"refresh_token={GOOGLE_REFRESH} access_token={GOOGLE_ACCESS}", [GOOGLE_REFRESH, GOOGLE_ACCESS]),
    (f"stored credential {GOOGLE_REFRESH} was rotated", [GOOGLE_REFRESH]),
    (f"id_token: {JWT_TOKEN}", [JWT_TOKEN]),
    (f"push with {GITHUB_TOKEN}", [GITHUB_TOKEN]),
    (f"github_token={GITHUB_TOKEN}", [GITHUB_TOKEN]),
    (
        f"401 Client Error: Unauthorized for url: https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        [TELEGRAM_TOKEN],
    ),
    (f"decrypt failed for {FERNET_TOKEN}", [FERNET_TOKEN]),
    (f"connect postgres://svc:{CLIENT_SECRET}@db.invalid:5432/app", [CLIENT_SECRET]),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqSynthetic\n-----END PRIVATE KEY-----", ["MIIEvQIBADANBgkqSynthetic"]),
    (f"vapid_private_key={VAPID_PRIVATE_KEY}", [VAPID_PRIVATE_KEY]),
]

COOKIE_SAMPLES = [
    (f"Cookie: session={SESSION_COOKIE}; csrf=SyntheticCsrfValue99", [SESSION_COOKIE, "SyntheticCsrfValue99"]),
    (f"Set-Cookie: session={SESSION_COOKIE}; Path=/; HttpOnly; SameSite=lax", [SESSION_COOKIE]),
    (f"cookies={{'session': '{SESSION_COOKIE}', 'theme': 'dark'}}", [SESSION_COOKIE]),
    (f"'cookie': 'session={SESSION_COOKIE}'", [SESSION_COOKIE]),
    (f"unexpected value {SESSION_COOKIE} in jar", [SESSION_COOKIE]),
]

HEALTH_SAMPLES = [
    (
        "recovery payload: {'resting_hr': 52, 'hrv': 61, 'sleep_hours': 7.43, 'weight_kg': 82.35}",
        ["52", "61", "7.43", "82.35"],
    ),
    ('{"body_fat_pct": 14.27, "muscle_pct": 41.9}', ["14.27", "41.9"]),
    ("Health Connect file: {'resting_hr': 49}", ["49"]),
    ("metrics weight_kg=80.125 hrv=58.5", ["80.125", "58.5"]),
    ("health_payload={'steps': 10432, 'sleep': {'deep': 91, 'rem': 104}}", ["10432", "91", "104"]),
]

PROMPT_SAMPLES = [
    (f'{{"prompt": "{USER_PROMPT}", "model": "m"}}', [USER_PROMPT]),
    (f"user_prompt='{USER_PROMPT}' attempt=1", [USER_PROMPT]),
    (f'system_prompt="{SYSTEM_PROMPT}"', [SYSTEM_PROMPT]),
    (f"request body: {{'messages': [{{'role': 'user', 'content': '{USER_PROMPT}'}}]}}", [USER_PROMPT]),
    (f"reasoning={{'text': '{USER_PROMPT}'}}", [USER_PROMPT]),
    (f"content: {USER_PROMPT}", [USER_PROMPT.split()[0] + " " + USER_PROMPT.split()[1]]),
]

WORKOUT_SAMPLES = [
    (
        "hevy payload: {'workouts': [{'title': 'Pull day', 'exercises': [{'name': 'Deadlift', 'sets': [{'weight_kg': 142.5, 'reps': 5}]}]}]}",
        ["Pull day", "Deadlift", "142.5"],
    ),
    ("routine_id=abc123 exercise_name='Bulgarian split squat'", ["Bulgarian split squat"]),
]

IDENTIFIER_SAMPLES = [
    (f"Unauthorized email: {EMAIL} attempted sign-in", [EMAIL]),
    ("user_id=5b2c1d3e-1111-2222-3333-444455556666 failed", ["5b2c1d3e-1111-2222-3333-444455556666"]),
    ("GET https://api.open-meteo.com/v1/forecast?latitude=51.5085&longitude=-0.1257&current=temp", ["51.5085", "-0.1257"]),
    ("push endpoint=https://push.example.invalid/send/SyntheticEndpointToken", ["SyntheticEndpointToken"]),
]


@pytest.mark.parametrize(
    ("text", "secrets"),
    [
        *AUTHORIZATION_SAMPLES,
        *API_KEY_SAMPLES,
        *OAUTH_CODE_SAMPLES,
        *TOKEN_SAMPLES,
        *COOKIE_SAMPLES,
        *HEALTH_SAMPLES,
        *PROMPT_SAMPLES,
        *WORKOUT_SAMPLES,
        *IDENTIFIER_SAMPLES,
    ],
)
def test_representative_secret_never_survives_redact_text(
    text: str, secrets: list[str]
) -> None:
    redacted = redact_text(text)

    for secret in secrets:
        assert secret not in redacted
    assert "[REDACTED" in redacted
    # Redaction is stable: re-redacting already redacted text changes nothing.
    assert redact_text(redacted) == redacted


@pytest.mark.parametrize(
    "text",
    [
        "Week 3 of 12, Block 1: Hypertrophy. Mode: schedule, RUN_AT: 07:00, TZ: UTC",
        "Fetched 12 routines from Hevy.",
        "Application startup complete.",
        "exit code=1 after 3 retries (status=ok)",
        "Created issue: https://github.com/example/workout-agent/issues/42",
        "Ignoring unparseable time '25:99'",
        "Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)",
        "Unknown programme goal: strength_test",
    ],
)
def test_ordinary_operational_text_is_left_intact(text: str) -> None:
    assert redact_text(text) == text


def test_redaction_markers_are_class_labelled() -> None:
    assert REDACTED_CREDENTIAL == safe_log_value("api_key", "x")
    assert REDACTED_IDENTIFIER == safe_log_value("email", "x")
    redacted = redact_text("resting_hr=52 prompt='hello there' workouts=[1, 2]")
    assert "resting_hr=[REDACTED:HEALTH]" in redacted
    assert "prompt='[REDACTED:PROMPT]'" in redacted
    assert "workouts=[REDACTED:WORKOUT]" in redacted


def test_redacted_json_stays_well_formed() -> None:
    import json

    original = json.dumps(
        {"api_key": HEVY_KEY, "prompt": USER_PROMPT, "model": "m", "n": 3}
    )

    parsed = json.loads(redact_text(original))

    assert parsed["api_key"] == REDACTED_CREDENTIAL
    assert parsed["prompt"] == "[REDACTED:PROMPT]"
    assert parsed["model"] == "m"


def test_unterminated_sensitive_value_fails_closed() -> None:
    redacted = redact_text(f'{{"prompt": "{USER_PROMPT} and more unterminated')

    assert USER_PROMPT not in redacted
    assert "more unterminated" not in redacted


def test_sensitive_name_is_found_behind_a_very_long_prefix() -> None:
    padded = "a" * 300 + f"api_key={HEVY_KEY}"

    assert HEVY_KEY not in redact_text(padded)


def test_oversized_input_is_truncated_not_scanned() -> None:
    text = "x" * MAX_REDACTION_CHARS + f" api_key={HEVY_KEY}"

    redacted = redact_text(text)

    assert HEVY_KEY not in redacted
    assert redacted.endswith(TRUNCATION_NOTICE)
    assert len(redacted) <= MAX_REDACTION_CHARS + len(TRUNCATION_NOTICE)


def test_pathological_inputs_are_redacted_in_bounded_time() -> None:
    # Each of these took tens of seconds when the patterns could rescan a long
    # run from every position; the bound is generous so slow CI cannot flake.
    size = 60_000
    pathological = [
        "a" * size,
        "a." * (size // 2),
        "a" * (size // 2) + " " * (size // 2),
        "eyJ-" * (size // 4),
        "http://a:b@" * (size // 11),
        "QUJD" * (size // 4),
        "k=" * (size // 2),
        "prompt=[" * (size // 8),
    ]

    started = time.perf_counter()
    for text in pathological:
        redact_text(text)

    assert time.perf_counter() - started < 5.0


# ---------------------------------------------------------------------------
# Structured redaction
# ---------------------------------------------------------------------------


def test_redact_value_handles_nested_payloads_by_field_and_by_shape() -> None:
    payload = {
        "provider": "google_health",
        "refresh_token": GOOGLE_REFRESH,
        "profile": {"email": EMAIL, "weight_kg": 80.0, "note": f"token {GITHUB_TOKEN}"},
        "workouts": [{"exercise_name": "Squat", "top_reps": 5}],
        "system_prompt": SYSTEM_PROMPT,
        "detail": f"upstream said Bearer {OPENAI_KEY}",
        "raw": f"cookie: session={SESSION_COOKIE}".encode(),
    }

    redacted = redact_value(payload)

    rendered = repr(redacted)
    for secret in (
        GOOGLE_REFRESH,
        EMAIL,
        "80.0",
        GITHUB_TOKEN,
        "Squat",
        SYSTEM_PROMPT,
        OPENAI_KEY,
        SESSION_COOKIE,
    ):
        assert secret not in rendered
    assert redacted["provider"] == "google_health"
    assert redacted["system_prompt"] == "[REDACTED:INTERNAL]"


SENSITIVE_KEYS = [
    "api_key",
    "refresh_token",
    "Authorization",
    "client_secret",
    "resting_hr",
    "weight_kg",
    "prompt",
    "user_prompt",
    "content",
    "workout",
    "email",
]


@settings(max_examples=75, deadline=None)
@given(
    keys=st.lists(st.sampled_from(SENSITIVE_KEYS), min_size=1, max_size=6, unique=True),
    nested=st.booleans(),
)
def test_property_sensitive_keys_never_leak_their_values(
    keys: list[str], nested: bool
) -> None:
    sentinels = {key: f"sentinel-{index}-{key}-value" for index, key in enumerate(keys)}
    structure: Any = dict(sentinels)
    if nested:
        structure = {"outer": [{"inner": structure}, ("tuple", structure)]}

    rendered = repr(redact_value(structure))

    for sentinel in sentinels.values():
        assert sentinel not in rendered


@settings(max_examples=200, deadline=None)
@given(text=st.text(max_size=400))
def test_property_redaction_never_raises_and_is_stable(text: str) -> None:
    once = redact_text(text)

    assert redact_text(once) == once


@settings(max_examples=150, deadline=None)
@given(
    prefix=st.text(max_size=60),
    suffix=st.text(max_size=60),
    template=st.sampled_from(
        [
            "Authorization: Bearer {s}",
            "api_key={s}",
            "'refresh_token': '{s}'",
            "GET /auth?code={s}&state=x",
            "Cookie: session={s}",
            "https://svc:{s}@host.invalid/",
        ]
    ),
)
def test_property_planted_secret_never_survives_surrounding_noise(
    prefix: str, suffix: str, template: str
) -> None:
    secret = "SyntheticSecretValue0123456789abcdef"
    assume(secret not in prefix + suffix)
    planted = f"{prefix}\n{template.format(s=secret)}\n{suffix}"

    assert secret not in redact_text(planted)


# ---------------------------------------------------------------------------
# Real logging pipelines
# ---------------------------------------------------------------------------


def _stream_logger(name: str) -> tuple[logging.Logger, io.StringIO, logging.Handler]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, stream, handler


def test_message_and_positional_args_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("leak.positional")

    header_line = f"Authorization: Bearer {JWT_TOKEN}"

    with caplog.at_level(logging.INFO, logger="leak.positional"):
        logger.info("Calling provider with %s and header %s", OPENAI_KEY, header_line)
        logger.info("payload=%s", {"api_key": HEVY_KEY, "resting_hr": 52})

    for secret in (OPENAI_KEY, JWT_TOKEN, HEVY_KEY, "52"):
        assert secret not in caplog.text
    assert "Calling provider with" in caplog.text


def test_mapping_style_args_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("leak.mapping")

    with caplog.at_level(logging.INFO, logger="leak.mapping"):
        logger.info("token %(token)s for %(who)s", {"token": f"tok={GOOGLE_ACCESS}", "who": EMAIL})

    assert GOOGLE_ACCESS not in caplog.text
    assert EMAIL not in caplog.text


def test_extra_fields_are_redacted_by_field_name_and_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("leak.extra")
    note = f"x {GITHUB_TOKEN}"

    with caplog.at_level(logging.INFO, logger="leak.extra"):
        logger.info(
            "event",
            extra={
                "prompt": USER_PROMPT,
                "api_key": HEVY_KEY,
                "context": {"resting_hr": 52, "note": note},
                "benign": 7,
            },
        )

    record = caplog.records[-1]
    rendered = repr(record.__dict__)
    for secret in (USER_PROMPT, HEVY_KEY, GITHUB_TOKEN, "'resting_hr': 52"):
        assert secret not in rendered
    assert record.benign == 7  # type: ignore[attr-defined]


def test_exception_tracebacks_and_chained_causes_are_redacted() -> None:
    logger, stream, handler = _stream_logger("leak.traceback")
    try:
        try:
            raise ValueError(f"upstream rejected refresh_token={GOOGLE_REFRESH}")
        except ValueError as cause:
            raise RuntimeError(f"sync failed for {EMAIL}: Bearer {OPENAI_KEY}") from cause
    except RuntimeError:
        logger.exception("Sync crashed")
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    for secret in (GOOGLE_REFRESH, EMAIL, OPENAI_KEY):
        assert secret not in output
    assert "Sync crashed" in output
    assert "Traceback (most recent call last)" in output
    assert "RuntimeError" in output and "ValueError" in output


def test_stack_info_is_redacted() -> None:
    logger, stream, handler = _stream_logger("leak.stack")
    secret_marker = "stack_secret=" + GITHUB_TOKEN
    try:
        logger.warning("with stack", stack_info=True)
    finally:
        logger.removeHandler(handler)

    assert secret_marker not in stream.getvalue()
    assert "with stack" in stream.getvalue()


def test_unformattable_log_calls_fail_closed_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("leak.badformat")

    with caplog.at_level(logging.INFO, logger="leak.badformat"):
        logger.info("two placeholders %s %s", OPENAI_KEY)  # noqa: PLE1206 - deliberately malformed

    assert OPENAI_KEY not in caplog.text
    assert "redaction failed" in caplog.text


def test_third_party_handlers_added_later_receive_redacted_records() -> None:
    logger, stream, handler = _stream_logger("thirdparty.sdk")
    try:
        logger.debug("Request options: %s", {"json_data": {"messages": [{"content": USER_PROMPT}]}})
        logger.debug("Sending header api-key: %s", HEVY_KEY)
    finally:
        logger.removeHandler(handler)

    assert USER_PROMPT not in stream.getvalue()
    assert HEVY_KEY not in stream.getvalue()


def test_uvicorn_access_log_drops_query_strings_and_keeps_formatter_contract() -> None:
    AccessFormatter = pytest.importorskip("uvicorn.logging").AccessFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        AccessFormatter(
            '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
        )
    )
    logger = logging.getLogger("uvicorn.access")
    original = (logger.handlers[:], logger.level, logger.propagate)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "10.0.0.5:51234",
            "GET",
            f"/api/rag_search?q=my%20resting%20heart%20rate%20is%2048&code={GOOGLE_CODE}",
            "1.1",
            200,
        )
        logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.5:51234", "GET", "/api/progress", "1.1", 200)
    finally:
        logger.handlers, logger.level, logger.propagate = original[0], original[1], original[2]

    lines = stream.getvalue().strip().splitlines()
    assert lines[0] == f'10.0.0.5:51234 - "GET /api/rag_search?{REDACTED_CREDENTIAL} HTTP/1.1" 200 OK'
    assert lines[1] == '10.0.0.5:51234 - "GET /api/progress HTTP/1.1" 200 OK'
    assert "resting" not in stream.getvalue()
    assert GOOGLE_CODE not in stream.getvalue()


def test_uvicorn_coloured_startup_lines_keep_their_formatted_values() -> None:
    DefaultFormatter = pytest.importorskip("uvicorn.logging").DefaultFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(DefaultFormatter("%(levelprefix)s %(message)s", use_colors=True))
    logger = logging.getLogger("uvicorn.error")
    saved = (logger.handlers[:], logger.level, logger.propagate)
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "Started server process [%d]",
            4242,
            extra={"color_message": "Started server process [\x1b[36m%d\x1b[0m]"},
        )
    finally:
        logger.handlers, logger.level, logger.propagate = saved

    assert "Started server process [4242]" in stream.getvalue()
    assert "%d" not in stream.getvalue()


def test_unexpected_calls_on_the_access_logger_are_still_redacted() -> None:
    access = logging.getLogger("uvicorn.access")
    saved = (access.handlers[:], access.level, access.propagate)
    logger, stream, _handler = _stream_logger("uvicorn.access")
    try:
        logger.info("custom note %s", f"api_key={HEVY_KEY}")
        logger.info("two args %s %s", "a", f"Bearer {OPENAI_KEY}")
    finally:
        access.handlers, access.level, access.propagate = saved

    assert HEVY_KEY not in stream.getvalue()
    assert OPENAI_KEY not in stream.getvalue()
    assert "custom note" in stream.getvalue()


def test_uncaught_exceptions_are_logged_redacted_not_printed(
    caplog: pytest.LogCaptureFixture, restore_excepthook: None, capsys: pytest.CaptureFixture[str]
) -> None:
    install_log_redaction()
    try:
        raise RuntimeError(f"fatal: api_key={OPENAI_KEY} for {EMAIL}")
    except RuntimeError:
        exc_info = sys.exc_info()

    with caplog.at_level(logging.CRITICAL, logger="workout_agent.uncaught"):
        sys.excepthook(*exc_info)

    assert "Uncaught exception" in caplog.text
    assert "RuntimeError" in caplog.text
    assert OPENAI_KEY not in caplog.text
    assert EMAIL not in caplog.text
    assert OPENAI_KEY not in capsys.readouterr().err


def test_install_is_idempotent() -> None:
    install_log_redaction()
    factory = logging.getLogRecordFactory()
    hook = sys.excepthook

    install_log_redaction()

    assert logging.getLogRecordFactory() is factory
    assert sys.excepthook is hook


# ---------------------------------------------------------------------------
# Pseudonymisation
# ---------------------------------------------------------------------------


def test_pseudonymised_identifier_is_stable_distinct_and_opaque() -> None:
    user_a = "5b2c1d3e-1111-2222-3333-444455556666"
    user_b = "9f8e7d6c-1111-2222-3333-444455556666"

    tag = pseudonymise_identifier(user_a)

    assert tag == pseudonymise_identifier(user_a)
    assert tag != pseudonymise_identifier(user_b)
    assert user_a not in tag and "5b2c1d3e" not in tag
    assert tag.startswith("id:") and len(tag) == len("id:") + 12


# ---------------------------------------------------------------------------
# Every logging entry point installs the layer (fail closed by construction)
# ---------------------------------------------------------------------------

# Repository-maintenance tools that audit repository structure: they log file
# names, sizes and commit subjects, never credential values or user data.
_MAINTENANCE_TOOLS = {"commit_hygiene.py", "dead_code_sweep.py"}


def _calls(tree: ast.AST, dotted: str) -> list[ast.Call]:
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            f"{func.value.id}.{func.attr}"
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if name == dotted:
            found.append(node)
    return found


def test_every_module_that_configures_logging_installs_redaction() -> None:
    offenders: list[str] = []
    checked = 0
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        relative = path.relative_to(BACKEND_ROOT)
        if relative.parts[0] in {"tests", "fixtures"} or path.name in _MAINTENANCE_TOOLS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _calls(tree, "logging.basicConfig"):
            continue
        checked += 1
        if not _calls(tree, "install_log_redaction"):
            offenders.append(str(relative))

    assert checked >= 5  # main, scheduler, insight_cron, sync_history, connector_health
    assert offenders == []


def test_web_package_installs_redaction_on_import() -> None:
    source = (BACKEND_ROOT / "webapp" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert _calls(tree, "install_log_redaction")
