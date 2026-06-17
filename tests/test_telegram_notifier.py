"""Tests for Telegram message splitting and MarkdownV2 escaping."""

from __future__ import annotations

from telegram_notifier import escape_markdown_v2, split_message


def test_short_message_is_single_chunk():
    assert split_message("hello world") == ["hello world"]


def test_split_respects_limit():
    text = "\n".join(f"line {i}" for i in range(100))
    chunks = split_message(text, limit=40)
    assert len(chunks) > 1
    assert all(len(chunk) <= 40 for chunk in chunks)
    # No content is lost when rejoined.
    assert "\n".join(chunks) == text


def test_split_hard_splits_overlong_line():
    text = "x" * 90
    chunks = split_message(text, limit=40)
    assert all(len(chunk) <= 40 for chunk in chunks)
    assert "".join(chunks) == text


def test_escape_markdown_v2_escapes_reserved_chars():
    assert escape_markdown_v2("a.b-c!") == "a\\.b\\-c\\!"


def test_escape_markdown_v2_leaves_plain_text():
    assert escape_markdown_v2("hello world") == "hello world"
