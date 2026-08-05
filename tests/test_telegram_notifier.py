"""Tests for Telegram message sending, splitting, and MarkdownV2 escaping."""

from __future__ import annotations

from unittest import mock

import requests

from telegram_notifier import (
    escape_markdown_v2,
    send_telegram_message,
    split_message,
)


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


class TestSendTelegramMessage:
    """Tests for send_telegram_message with mocked network."""

    @mock.patch("telegram_notifier.requests.post")
    def test_sends_single_chunk(self, mock_post: mock.Mock) -> None:
        mock_resp = mock.Mock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        result = send_telegram_message("token123", "chat456", "hello")
        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        payload = call_args.kwargs["json"]
        assert payload["chat_id"] == "chat456"
        assert payload["text"] == "hello"

    @mock.patch("telegram_notifier.requests.post")
    def test_sends_markdown_v2_escaped(self, mock_post: mock.Mock) -> None:
        mock_resp = mock.Mock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        result = send_telegram_message(
            "token123",
            "chat456",
            "a.b-c!",
            parse_mode="MarkdownV2",
        )
        assert result is True
        payload = mock_post.call_args.kwargs["json"]
        assert payload["text"] == "a\\.b\\-c\\!"
        assert payload["parse_mode"] == "MarkdownV2"

    @mock.patch("telegram_notifier.requests.post")
    def test_returns_false_on_network_error(self, mock_post: mock.Mock) -> None:
        mock_post.side_effect = requests.RequestException("timeout")

        result = send_telegram_message("token123", "chat456", "hello")
        assert result is False

    @mock.patch("telegram_notifier.requests.post")
    def test_returns_false_on_http_error(self, mock_post: mock.Mock) -> None:
        mock_resp = mock.Mock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError("403")
        mock_post.return_value = mock_resp

        result = send_telegram_message("token123", "chat456", "hello")
        assert result is False

    @mock.patch("telegram_notifier.requests.post")
    def test_splits_long_message(self, mock_post: mock.Mock) -> None:
        mock_resp = mock.Mock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        long_text = "x" * 5000
        result = send_telegram_message("token123", "chat456", long_text)
        assert result is True
        assert mock_post.call_count == 2
