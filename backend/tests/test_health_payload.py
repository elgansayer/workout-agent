import pytest

from health_payload import MAX_PAYLOAD_BYTES, parse_bounded_json


def test_bounded_json_parses_valid_payload():
    assert parse_bounded_json(b'{"id":"synthetic"}') == {"id": "synthetic"}


def test_bounded_json_rejects_invalid_or_oversized_payload():
    with pytest.raises(ValueError, match="invalid"):
        parse_bounded_json(b"not-json")
    with pytest.raises(ValueError, match="size"):
        parse_bounded_json(b"x" * (MAX_PAYLOAD_BYTES + 1))
