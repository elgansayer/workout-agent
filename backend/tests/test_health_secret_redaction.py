from health_secret_redaction import redact


def test_connector_secrets_are_redacted_recursively():
    value = {"provider": "oura", "oauth": {"access_token": "secret"}, "items": [{"api_key": "secret2"}]}
    redacted = redact(value)
    assert redacted["oauth"]["access_token"] == "[REDACTED]"
    assert redacted["items"][0]["api_key"] == "[REDACTED]"
    assert redacted["provider"] == "oura"
