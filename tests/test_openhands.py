"""Tests for issue #46: dummy text file created in the root directory."""

from pathlib import Path


def test_openhands_txt_exists_and_has_expected_content() -> None:
    """The root-level test_openhands.txt must contain the expected message."""
    root = Path(__file__).resolve().parent.parent
    marker = root / "test_openhands.txt"
    assert marker.exists(), "test_openhands.txt is missing from the root directory"
    assert marker.read_text(encoding="utf-8") == "Hello from OpenHands"
