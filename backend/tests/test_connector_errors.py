from connectors.errors import map_http_error


def test_provider_errors_have_stable_retry_semantics():
    assert map_http_error(401, provider="oura").code == "unauthorized"
    assert not map_http_error(403, provider="oura").retryable
    assert map_http_error(429, provider="oura").retryable
    assert map_http_error(503, provider="oura").retryable
