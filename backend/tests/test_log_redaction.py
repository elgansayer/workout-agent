"""The central log-redaction layer (issue #766).

Every secret here is synthetic and assembled at run time from short pieces, so no
contiguous provider-shaped literal exists in the source tree for a secret scanner
to mistake for a real credential.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Iterator

import log_redaction
import pytest
from log_redaction import (
    CREDENTIAL_MARKER,
    EMAIL_MARKER,
    QUERY_MARKER,
    forget_registered_secrets,
    harden_record,
    install_log_redaction,
    log_redaction_installed,
    redact_text,
    redact_traceback,
    redact_value,
    register_environment_secrets,
    register_secret,
)

install_log_redaction()

_BODY = "Zq9" * 8
OPENAI_KEY = "sk-" + "proj-" + _BODY
GOOGLE_API_KEY = "AIza" + "Sy" + _BODY + "xyz" + "Q" * 6  # 39 characters, like the real shape
GOOGLE_ACCESS_TOKEN = "ya29." + "a0" + _BODY
GOOGLE_REFRESH_TOKEN = "1//" + "0g" + _BODY
GOOGLE_AUTH_CODE = "4/" + "0A" + _BODY + "12345678"
GITHUB_TOKEN = "ghp_" + _BODY + "Q" * 12
TELEGRAM_TOKEN = "123456789" + ":" + "AA" + _BODY + "x" + "Q" * 6
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiJ0ZXN0In0" + "." + "c2lnbmF0dXJl"
OPAQUE_PROVIDER_TOKEN = "opaque-" + "provider-" + "token-" + "12345"
OPAQUE_AUTH_CODE = "Xk3" + "9fQ2" + "mP7s" + "L1vB" + "8nRt"


@pytest.fixture(autouse=True)
def isolated_registry() -> Iterator[None]:
    forget_registered_secrets()
    yield
    forget_registered_secrets()


# ---------------------------------------------------------------------------
# Text: secrets in free text, exception messages, headers, URLs and forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"Authorization: Bearer {OPENAI_KEY}", OPENAI_KEY),
        (f"proxy-authorization: Basic {OPAQUE_PROVIDER_TOKEN}", OPAQUE_PROVIDER_TOKEN),
        (f"Cookie: theme=dark; session={OPAQUE_PROVIDER_TOKEN}", OPAQUE_PROVIDER_TOKEN),
        (f"Set-Cookie: sid={OPAQUE_PROVIDER_TOKEN}; Path=/; HttpOnly", OPAQUE_PROVIDER_TOKEN),
        (f"x-api-key: {OPAQUE_PROVIDER_TOKEN}", OPAQUE_PROVIDER_TOKEN),
        (f"request failed, retrying with Bearer {OPAQUE_PROVIDER_TOKEN}", OPAQUE_PROVIDER_TOKEN),
        (f"token {JWT} rejected", JWT),
        (f"provider said no to {OPENAI_KEY}", OPENAI_KEY),
        (f"Gemini rejected {GOOGLE_API_KEY}", GOOGLE_API_KEY),
        (f"access {GOOGLE_ACCESS_TOKEN} refresh {GOOGLE_REFRESH_TOKEN}", GOOGLE_ACCESS_TOKEN),
        (f"access {GOOGLE_ACCESS_TOKEN} refresh {GOOGLE_REFRESH_TOKEN}", GOOGLE_REFRESH_TOKEN),
        (f"auth code was {GOOGLE_AUTH_CODE}", GOOGLE_AUTH_CODE),
        (f"push to remote with {GITHUB_TOKEN}", GITHUB_TOKEN),
        (
            f"HTTPSConnectionPool(host='api.telegram.org'): url: /bot{TELEGRAM_TOKEN}/sendMessage",
            TELEGRAM_TOKEN,
        ),
        (f"connect to https://svc:{OPAQUE_PROVIDER_TOKEN}@db.example.test/path", OPAQUE_PROVIDER_TOKEN),
        (f'headers={{"Authorization": "Bearer {OPAQUE_PROVIDER_TOKEN}"}}', OPAQUE_PROVIDER_TOKEN),
        (f"headers={{'api-key': '{OPAQUE_PROVIDER_TOKEN}'}}", OPAQUE_PROVIDER_TOKEN),
        (f"api_key={OPAQUE_PROVIDER_TOKEN} and more", OPAQUE_PROVIDER_TOKEN),
        (f'{{"access_token": "{OPAQUE_PROVIDER_TOKEN}", "expires_in": 3600}}', OPAQUE_PROVIDER_TOKEN),
        (f'escaped \\"refresh_token\\": \\"{OPAQUE_PROVIDER_TOKEN}\\" json', OPAQUE_PROVIDER_TOKEN),
        (f"client_secret={OPAQUE_PROVIDER_TOKEN}&grant_type=refresh_token", OPAQUE_PROVIDER_TOKEN),
        (f"password: {OPAQUE_PROVIDER_TOKEN}", OPAQUE_PROVIDER_TOKEN),
        (f"authorization=Basic {OPAQUE_PROVIDER_TOKEN}", OPAQUE_PROVIDER_TOKEN),
        (
            "-----BEGIN PRIVATE KEY-----\n" + OPAQUE_PROVIDER_TOKEN + "\n-----END PRIVATE KEY-----",
            OPAQUE_PROVIDER_TOKEN,
        ),
    ],
)
def test_secrets_are_removed_from_free_text(text: str, secret: str) -> None:
    redacted = redact_text(text)

    assert secret not in redacted
    assert "[REDACTED" in redacted


def test_oauth_callback_url_loses_code_and_state_but_keeps_the_route() -> None:
    line = f"GET /auth?state={OPAQUE_AUTH_CODE}&code={GOOGLE_AUTH_CODE}&scope=openid HTTP/1.1"

    redacted = redact_text(line)

    assert redacted == (
        f"GET /auth?state={QUERY_MARKER}&code={QUERY_MARKER}&scope={QUERY_MARKER} HTTP/1.1"
    )


def test_url_fragments_and_form_bodies_lose_their_values() -> None:
    assert OPAQUE_PROVIDER_TOKEN not in redact_text(
        f"redirect https://app.example.test/cb#access_token={OPAQUE_PROVIDER_TOKEN}&token_type=bearer"
    )
    redacted = redact_text(f"body: code={OPAQUE_AUTH_CODE}&redirect_uri=http://x.test/cb")
    assert OPAQUE_AUTH_CODE not in redacted
    assert "redirect_uri=" in redacted


def test_a_bare_oauth_code_is_removed_but_status_words_survive() -> None:
    assert OPAQUE_AUTH_CODE not in redact_text(f"exchange failed code={OPAQUE_AUTH_CODE}")
    assert redact_text("response code=404 state=syncing") == "response code=404 state=syncing"
    assert redact_text("code: 12 state: ok") == "code: 12 state: ok"


def test_health_workout_and_prompt_pairs_are_removed_from_text() -> None:
    text = (
        'payload={"weight_kg": 81.5, "resting_hr": 52, "sleep": [7.5, 6.0], '
        '"exercise_name": "Back Squat", "prompt": "my left knee clicks when I squat"} '
        "heart_rate=61 hrv=40 body_fat_pct=18.2 steps=9000 "
        "prompt=my knee hurts today\nnext line is kept"
    )

    redacted = redact_text(text)

    for private in ("81.5", "52,", "7.5", "Back Squat", "knee", "61", "40 ", "18.2", "9000"):
        assert private not in redacted, private
    assert "next line is kept" in redacted
    assert "[REDACTED:HEALTH]" in redacted
    assert "[REDACTED:PROMPT]" in redacted
    assert "[REDACTED:WORKOUT]" in redacted


def test_email_addresses_are_removed_from_text() -> None:
    assert redact_text("signed in person@example.com today") == (
        f"signed in {EMAIL_MARKER} today"
    )
    assert "alice" not in redact_text("display_name=alice email=alice@example.com")


@pytest.mark.parametrize(
    "line",
    [
        "access_denied surface=login reason=session_revoked account=5f1c2b9a8d7e4f60 identity=ab12cd34ef56",
        "security_audit seq=12 event_type=auth.login outcome=success request_id="
        + "0123456789abcdef" * 2
        + " event_hash="
        + "ab" * 32,
        "Plan: ready. Recovery: fine. Sleep: ok. Routine: pull",
        "SELECT * FROM t WHERE a = ? AND b=? LIMIT 5",
        "Started server process [1] on 0.0.0.0:8000",
        "Daily insight generated successfully via Gemini (gemini-3.8-flash).",
        "uuid 123e4567-e89b-42d3-a456-426614174000 stays",
    ],
)
def test_operational_text_and_opaque_ids_are_preserved(line: str) -> None:
    assert redact_text(line) == line


def test_redaction_is_idempotent() -> None:
    sample = (
        f"Authorization: Bearer {OPENAI_KEY} GET /auth?code={GOOGLE_AUTH_CODE}&state=x "
        f"mail a@example.com api_key={OPAQUE_PROVIDER_TOKEN} heart_rate=61 "
        f'{{"refresh_token": "{OPAQUE_PROVIDER_TOKEN}"}} https://u:p@h.test/'
    )
    once = redact_text(sample)

    assert redact_text(once) == once


def test_hostile_inputs_are_processed_in_bounded_time() -> None:
    hostile = [
        "a=" * 40_000,
        "?" * 100_000,
        "A" * 200_000,
        "api_key=" + "[" * 5_000,
        "-----BEGIN PRIVATE KEY-----" * 2_000,
        '"' * 50_000 + "token=" + "'" * 50_000,
        "x@" * 30_000,
        "key: " * 40_000,
    ]
    started = time.perf_counter()
    for text in hostile:
        redact_text(text)
    assert time.perf_counter() - started < 10


def test_oversized_text_is_truncated_with_a_marker() -> None:
    redacted = redact_text("x" * 250_000)

    assert len(redacted) < 210_000
    assert "[truncated 50000 characters]" in redacted


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


def test_structured_payloads_are_redacted_by_field_classification() -> None:
    payload = {
        "provider": "google_health",
        "authorization": f"Bearer {OPENAI_KEY}",
        "cookie": "session=abc",
        "api_key": OPAQUE_PROVIDER_TOKEN,
        "code": OPAQUE_AUTH_CODE,
        "state": "syncing",
        "profile": {"email": "person@example.com", "weight_kg": 80.0, "hrv": 41},
        "workouts": [{"exercise_name": "Squat", "top_reps": 5}],
        "prompt": "private coaching question",
        "messages": [{"role": "user", "content": "private"}],
        "note": f"see ?code={GOOGLE_AUTH_CODE}",
        "status_code": 404,
    }

    redacted = redact_value(payload)
    rendered = repr(redacted)

    for private in (
        OPENAI_KEY,
        "session=abc",
        OPAQUE_PROVIDER_TOKEN,
        OPAQUE_AUTH_CODE,
        "person@example.com",
        "80.0",
        "Squat",
        "private",
        GOOGLE_AUTH_CODE,
    ):
        assert private not in rendered, private
    assert redacted["provider"] == "google_health"
    assert redacted["state"] == "syncing"
    assert redacted["status_code"] == 404
    assert redacted["authorization"] == CREDENTIAL_MARKER
    assert redacted["profile"] == {
        "email": "[REDACTED:IDENTIFIER]",
        "weight_kg": "[REDACTED:HEALTH]",
        "hrv": "[REDACTED:HEALTH]",
    }
    assert redacted["workouts"] == "[REDACTED:WORKOUT]"
    assert redacted["messages"] == "[REDACTED:PROMPT]"


def test_structures_keep_their_container_types_and_scalars() -> None:
    assert redact_value((1, 2.5, None, True, "ok")) == (1, 2.5, None, True, "ok")
    assert redact_value([f"token={OPAQUE_PROVIDER_TOKEN}"]) == [f"token={CREDENTIAL_MARKER}"]
    assert redact_value({"b": b"raw secret bytes"}) == {"b": "[REDACTED:BYTES:16]"}


def test_structures_are_bounded_in_depth_and_size() -> None:
    deep: dict[str, object] = {"leaf": "visible"}
    for _ in range(20):
        deep = {"next": deep}
    assert "[REDACTED:DEPTH]" in repr(redact_value(deep))

    wide = redact_value(list(range(500)))
    assert len(wide) == 201 and "more items omitted" in wide[-1]
    wide_map = redact_value({f"k{i}": i for i in range(500)})
    assert len(wide_map) == 201 and "more entries omitted" in wide_map["..."]
    # A pathological argument is never materialised past the cap.
    assert len(redact_value(range(10**12))) == 201


# ---------------------------------------------------------------------------
# Known-secret registry
# ---------------------------------------------------------------------------


def test_registered_secret_is_masked_whatever_its_shape_or_context() -> None:
    register_secret(OPAQUE_PROVIDER_TOKEN)

    text = redact_text(f"upstream echoed {OPAQUE_PROVIDER_TOKEN} verbatim, twice {OPAQUE_PROVIDER_TOKEN}")

    assert OPAQUE_PROVIDER_TOKEN not in text
    assert text.count(CREDENTIAL_MARKER) == 2


def test_registered_secret_is_masked_in_url_encoded_form() -> None:
    secret = "needs encoding/with+chars&more=1"
    register_secret(secret)

    assert "needs%20encoding" not in redact_text("GET /x?v=needs%20encoding%2Fwith%2Bchars%26more%3D1")
    assert "needs+encoding" not in redact_text("body v=needs+encoding%2Fwith%2Bchars%26more%3D1")


def test_client_facing_text_skips_the_shared_registry_but_keeps_the_patterns() -> None:
    register_secret(OPAQUE_PROVIDER_TOKEN)
    echoed = f"guess {OPAQUE_PROVIDER_TOKEN} and key {OPENAI_KEY}"

    public = redact_text(echoed, known_secrets=False)

    # The registry is shared by every tenant: masking a registered value in text
    # a caller can read would let them test guesses against other accounts' keys.
    assert OPAQUE_PROVIDER_TOKEN in public
    assert OPENAI_KEY not in public  # shape-based masking is a pure function of the text
    assert OPAQUE_PROVIDER_TOKEN not in redact_text(echoed)


def test_short_and_non_text_values_are_never_registered() -> None:
    for value in ("short", "1234567", "", None, 12345678, b"bytes-value-long"):
        register_secret(value)

    assert redact_text("short 1234567 12345678 bytes-value-long") == (
        "short 1234567 12345678 bytes-value-long"
    )


def test_registry_is_bounded_and_evicts_the_oldest_entries() -> None:
    limit = log_redaction._MAX_REGISTERED_SECRETS
    oldest = "first-registered-secret-0001"
    register_secret(oldest)
    for index in range(limit * 3):
        register_secret(f"filler-secret-value-{index:06d}")

    newest = f"filler-secret-value-{limit * 3 - 1:06d}"
    assert redact_text(oldest) == oldest  # evicted
    assert redact_text(newest) == CREDENTIAL_MARKER  # still registered


def test_re_registering_a_known_secret_keeps_the_cached_ordering() -> None:
    register_secret(OPAQUE_PROVIDER_TOKEN)
    cached = log_redaction._registered_secrets()

    register_secret(OPAQUE_PROVIDER_TOKEN)  # the vault does this on every use

    assert log_redaction._registered_secrets() is cached
    register_secret(OPAQUE_PROVIDER_TOKEN + "-rotated")
    assert log_redaction._registered_secrets() is not cached


def test_environment_secrets_are_registered_but_public_values_are_not() -> None:
    register_environment_secrets(
        {
            "WEB_AUTH_SECRET": "env-session-secret-0123456789",
            "TELEGRAM_BOT_TOKEN": "env-bot-token-0123456789",
            "GEMINI_API_KEY": "env-gemini-key-0123456789",
            "VAPID_PRIVATE_KEY": "env-vapid-private-0123456789",
            "ENCRYPTION_KEY": "env-encryption-key-0123456789",
            "VAPID_PUBLIC_KEY": "env-vapid-public-0123456789",
            "WEB_PUBLIC_URL": "https://workout.example.test/app",
        }
    )

    for secret in (
        "env-session-secret-0123456789",
        "env-bot-token-0123456789",
        "env-gemini-key-0123456789",
        "env-vapid-private-0123456789",
        "env-encryption-key-0123456789",
    ):
        assert secret not in redact_text(f"leaked {secret}")
    assert redact_text("key env-vapid-public-0123456789") == "key env-vapid-public-0123456789"
    assert redact_text("at https://workout.example.test/app") == "at https://workout.example.test/app"


def test_vault_secret_values_register_themselves_on_construction() -> None:
    pytest.importorskip("requests")  # the vault's import chain needs the full backend install
    from secret_vault import SecretValue

    secret = "vault-" + "held-" + "credential-" + "98765"
    credential = SecretValue(secret)

    assert secret not in redact_text(f"unexpected echo {secret}")
    assert secret not in redact_text(f"unexpected echo {credential.reveal()}")


# ---------------------------------------------------------------------------
# Log records
# ---------------------------------------------------------------------------


def _isolated_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger(name)
    logger.handlers[:] = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, stream


def test_the_record_factory_is_installed_once_and_is_idempotent() -> None:
    before = logging.getLogRecordFactory()

    install_log_redaction()
    install_log_redaction()

    assert log_redaction_installed()
    assert logging.getLogRecordFactory() is before


def test_log_arguments_messages_and_extra_pairs_are_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger("redaction.unit")

    logger.info("exchange failed api_key=%s", OPAQUE_PROVIDER_TOKEN)
    logger.info("echo %s and %r", GOOGLE_ACCESS_TOKEN, GOOGLE_ACCESS_TOKEN)
    logger.info(f"preformatted token={OPAQUE_PROVIDER_TOKEN}")
    logger.info("payload %s", {"heart_rate": 61, "authorization": f"Bearer {OPENAI_KEY}"})
    logger.info("mapping %(who)s %(authorization)s", {"who": "ok", "authorization": OPENAI_KEY})
    logger.warning("for %s", "person@example.com")

    assert caplog.text
    for secret in (
        OPAQUE_PROVIDER_TOKEN,
        GOOGLE_ACCESS_TOKEN,
        OPENAI_KEY,
        "person@example.com",
        "'heart_rate': 61",
    ):
        assert secret not in caplog.text, secret
    assert "'heart_rate': '[REDACTED:HEALTH]'" in caplog.text
    assert "mapping ok [REDACTED:CREDENTIAL]" in caplog.text
    for record in caplog.records:
        assert all(
            secret not in record.getMessage()
            for secret in (OPAQUE_PROVIDER_TOKEN, GOOGLE_ACCESS_TOKEN, OPENAI_KEY)
        )


def test_numeric_arguments_keep_their_type_for_printf_formatting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    logging.getLogger("redaction.unit").info("%d items in %.1f seconds, ok=%s", 3, 1.25, True)

    assert "3 items in 1.2 seconds, ok=True" in caplog.text


def test_tracebacks_are_redacted_but_keep_their_source_lines() -> None:
    logger, stream = _isolated_logger("redaction.traceback")

    def fail() -> None:
        api_key = "synthetic"
        raise RuntimeError(f"upstream rejected {api_key} with Authorization: Bearer {OPENAI_KEY}")

    try:
        fail()
    except RuntimeError:
        logger.exception("operation failed for %s", "person@example.com")

    output = stream.getvalue()
    assert "RuntimeError: upstream rejected synthetic" in output
    # The frame's source line is code, not data, so the operator still sees it.
    assert 'raise RuntimeError(f"upstream rejected {api_key} with Authorization' in output
    assert OPENAI_KEY not in output
    assert "person@example.com" not in output


def test_chained_exception_messages_are_redacted_too() -> None:
    logger, stream = _isolated_logger("redaction.chained")

    try:
        try:
            raise ValueError(f"inner token={OPAQUE_PROVIDER_TOKEN}")
        except ValueError as inner:
            raise RuntimeError("outer failure") from inner
    except RuntimeError:
        logger.exception("chained")

    output = stream.getvalue()
    assert "inner token=" in output and "outer failure" in output
    assert OPAQUE_PROVIDER_TOKEN not in output


def test_a_second_handler_with_its_own_formatter_cannot_see_the_raw_traceback() -> None:
    logger, _ = _isolated_logger("redaction.second")
    other = io.StringIO()
    handler = logging.StreamHandler(other)
    handler.setFormatter(logging.Formatter("%(message)s | %(exc_text)s"))
    logger.addHandler(handler)

    try:
        raise RuntimeError(f"secret in message {OPENAI_KEY}")
    except RuntimeError:
        logger.exception("failed")

    assert OPENAI_KEY not in other.getvalue()


def test_third_party_logger_records_are_covered_without_any_cooperation() -> None:
    logger, stream = _isolated_logger("httpx.simulated")

    logger.info(
        'HTTP Request: GET https://generativelanguage.example.test/v1/models?key=%s "HTTP/1.1 400"',
        GOOGLE_API_KEY,
    )

    assert GOOGLE_API_KEY not in stream.getvalue()
    assert "models?key=[REDACTED:QUERY]" in stream.getvalue()


def test_uvicorn_access_formatter_still_works_and_receives_redacted_arguments() -> None:
    pytest.importorskip("uvicorn")
    from uvicorn.logging import AccessFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    )
    logger = logging.getLogger("uvicorn.access.simulated")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    logger.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5000",
        "GET",
        f"/auth?state={OPAQUE_AUTH_CODE}&code={GOOGLE_AUTH_CODE}",
        "1.1",
        302,
    )

    line = stream.getvalue()
    assert line.startswith('127.0.0.1:5000 - "GET /auth?state=[REDACTED:QUERY]&code=[REDACTED:QUERY] HTTP/1.1" 302')
    assert OPAQUE_AUTH_CODE not in line and GOOGLE_AUTH_CODE not in line


def test_redaction_failure_withholds_the_message_instead_of_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(_text: object) -> str:
        raise RuntimeError("redactor bug")

    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "token=%s", (OPAQUE_PROVIDER_TOKEN,), None
    )
    monkeypatch.setattr(log_redaction, "redact_text", explode)

    hardened = harden_record(record)

    assert hardened.getMessage() == log_redaction.WITHHELD_MESSAGE
    assert OPAQUE_PROVIDER_TOKEN not in repr(hardened.__dict__)


def test_redact_traceback_redacts_messages_and_preserves_frames() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "/app/client.py", line 12, in fetch\n'
        '    headers = {"Authorization": f"Bearer {token}"}\n'
        f"HTTPError: 401 for url: https://x.test/v1?api_key={OPAQUE_PROVIDER_TOKEN}"
    )

    redacted = redact_traceback(text)

    assert OPAQUE_PROVIDER_TOKEN not in redacted
    assert '    headers = {"Authorization": f"Bearer {token}"}' in redacted
    assert 'File "/app/client.py", line 12, in fetch' in redacted
