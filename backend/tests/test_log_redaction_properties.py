"""Property-based tests for the central log-redaction layer (issue #766).

Hand-written examples show the layer works on realistic inputs; these generate the
surrounding text and the secrets so the invariants hold for inputs nobody wrote
down: a secret never survives whatever surrounds it, redaction is idempotent,
opaque correlation ids are never touched, and nothing here can raise.
"""

from __future__ import annotations

import logging
import string
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st
from log_redaction import (
    forget_registered_secrets,
    harden_record,
    redact_text,
    redact_value,
    register_secret,
)

PROPERTY_SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

# Surrounding prose that cannot itself be read as a quote, bracket, separator or
# key name, so the secret is the only thing the redactor has to find.
NOISE = st.text(alphabet=string.ascii_letters + string.digits + " .,;", max_size=40)
# A generated credential with no recognisable provider shape: only its context
# (a key name, a header, a URL position) identifies it.
SECRETS = st.text(alphabet=string.ascii_letters + string.digits, min_size=12, max_size=40)
CREDENTIAL_KEYS = st.sampled_from(
    ["api_key", "access_token", "refresh_token", "client_secret", "password", "cookie"]
)
HEALTH_KEYS = st.sampled_from(["weight_kg", "resting_hr", "hrv", "heart_rate", "sleep"])
SENSITIVE_QUERY_NAMES = st.sampled_from(["code", "state", "token", "api_key", "id_token", "key"])

TEMPLATES = (
    "token={s}",
    "api_key={s}",
    "Authorization: Bearer {s}",
    "Bearer {s}",
    "Cookie: session={s}",
    "x-api-key: {s}",
    'payload={{"access_token": "{s}", "expires_in": 3600}}',
    "headers={{'api-key': '{s}'}}",
    "GET /auth?code={s}&state=ok HTTP/1.1",
    "https://user:{s}@host.test/path",
    "client_secret={s}&grant_type=refresh_token",
)


@pytest.fixture(autouse=True)
def isolated_registry() -> Iterator[None]:
    forget_registered_secrets()
    yield
    forget_registered_secrets()


@PROPERTY_SETTINGS
@given(secret=SECRETS, before=NOISE, after=NOISE, template=st.sampled_from(TEMPLATES))
def test_a_secret_never_survives_its_surrounding_text(
    secret: str, before: str, after: str, template: str
) -> None:
    assume(secret not in before and secret not in after)  # noise must not repeat it
    text = f"{before} {template.format(s=secret)} {after}"

    assert secret not in redact_text(text)


@PROPERTY_SETTINGS
@given(secret=SECRETS, before=NOISE, after=NOISE, key=CREDENTIAL_KEYS)
def test_a_credential_key_masks_its_value_in_every_syntax(
    secret: str, before: str, after: str, key: str
) -> None:
    assume(secret not in before and secret not in after)
    for text in (
        f"{before} {key}={secret} {after}",
        f'{before} {{"{key}": "{secret}"}} {after}',
        f"{before} {{'{key}': '{secret}'}} {after}",
        f'{before} \\"{key}\\": \\"{secret}\\" {after}',
        f"{before} {key}: {secret}\n{after}",
    ):
        assert secret not in redact_text(text), text


@PROPERTY_SETTINGS
@given(value=SECRETS, key=HEALTH_KEYS, before=NOISE, after=NOISE)
def test_health_fields_are_masked_when_written_as_pairs(
    value: str, key: str, before: str, after: str
) -> None:
    assume(value not in before and value not in after)
    for text in (f"{before} {key}={value} {after}", f'{before} {{"{key}": "{value}"}} {after}'):
        assert value not in redact_text(text), text


@PROPERTY_SETTINGS
@given(secret=SECRETS, before=NOISE, after=NOISE)
def test_a_registered_secret_never_survives_anywhere(
    secret: str, before: str, after: str
) -> None:
    register_secret(secret)

    for text in (f"{before}{secret}{after}", f"{before} {secret} {after}", secret * 3):
        assert secret not in redact_text(text)


@PROPERTY_SETTINGS
@given(name=SENSITIVE_QUERY_NAMES, value=SECRETS, tail=st.sampled_from(["", "&x=1", "#frag"]))
def test_every_query_and_form_value_is_masked_but_the_name_is_kept(
    name: str, value: str, tail: str
) -> None:
    redacted = redact_text(f"GET /callback?{name}={value}{tail} HTTP/1.1")

    assert value not in redacted
    assert f"?{name}=[REDACTED:QUERY]" in redacted


@PROPERTY_SETTINGS
@given(
    request_id=st.text(alphabet="0123456789abcdef", min_size=32, max_size=32),
    event_hash=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
    account=st.text(alphabet="0123456789abcdef-", min_size=8, max_size=36),
)
def test_opaque_correlation_ids_are_never_touched(
    request_id: str, event_hash: str, account: str
) -> None:
    line = (
        f"security_audit seq=7 outcome=success request_id={request_id} "
        f"event_hash={event_hash} account={account} reason=session_revoked"
    )

    assert redact_text(line) == line


@PROPERTY_SETTINGS
# Counterexample found by a long search: an empty query value followed by a
# fragment was redacted as a credential first and re-labelled as a query value on
# the second pass.
@example(parts=["?CODE=", "#"])
@given(
    parts=st.lists(
        st.one_of(
            st.text(max_size=30),
            st.sampled_from(
                [
                    "token=",
                    "?code=",
                    "&state=",
                    "Bearer ",
                    "Authorization: ",
                    "password: ",
                    '"api_key": "',
                    "\\",
                    '"',
                    "'",
                    "[",
                    "]",
                    "{",
                    "}",
                    "(",
                    "https://u:p@",
                    "@example.com",
                    "-----BEGIN PRIVATE KEY-----",
                    "eyJabcdefg.abcdefgh.",
                    "prompt=",
                    "heart_rate=",
                    "\n",
                ]
            ),
        ),
        max_size=25,
    )
)
def test_redaction_is_idempotent_and_never_raises(parts: list[str]) -> None:
    text = "".join(parts)

    once = redact_text(text)

    assert redact_text(once) == once


@PROPERTY_SETTINGS
@given(
    key=st.one_of(CREDENTIAL_KEYS, HEALTH_KEYS, st.sampled_from(["prompt", "email", "workout"])),
    value=SECRETS,
    wrapper=st.sampled_from(["plain", "list", "nested"]),
)
def test_structured_values_under_sensitive_keys_are_replaced_whole(
    key: str, value: str, wrapper: str
) -> None:
    payload: object = value
    if wrapper == "list":
        payload = [value, {"inner": value}]
    elif wrapper == "nested":
        payload = {"deep": {"deeper": [value]}}

    redacted = redact_value({"provider": "hevy", key: payload, "ok": 1})

    assert value not in repr(redacted)
    assert redacted["provider"] == "hevy" and redacted["ok"] == 1


@PROPERTY_SETTINGS
@given(
    template=st.text(max_size=40),
    arguments=st.lists(
        st.one_of(st.text(max_size=20), st.integers(), st.floats(allow_nan=True), st.none()),
        max_size=4,
    ),
)
def test_hardening_a_record_never_raises_whatever_the_message_and_arguments(
    template: str, arguments: list[object]
) -> None:
    record = logging.LogRecord("p", logging.INFO, __file__, 1, template, tuple(arguments), None)

    hardened = harden_record(record)

    try:
        hardened.getMessage()
    except (TypeError, ValueError):
        # A malformed ``%`` format string is the caller's logging error, reported
        # by the logging module itself; it must not be an unhandled redaction error.
        pass
