"""Tests for Web Push notification sending."""

from unittest import mock

from push_notifier import WebPushException, get_vapid_claims, send_push_notification


def test_get_vapid_claims() -> None:
    claims = get_vapid_claims("mailto:test@example.com")
    assert claims["sub"] == "mailto:test@example.com"


@mock.patch("push_notifier.webpush")
def test_send_push_notification_success(mock_webpush: mock.MagicMock) -> None:
    sub = {"endpoint": "https://push.example.com"}
    payload = {"title": "Test", "body": "Body"}
    
    res = send_push_notification(sub, payload, "fake-vapid-key")
    assert res is True
    mock_webpush.assert_called_once()


@mock.patch("push_notifier.webpush")
def test_send_push_notification_failure(mock_webpush: mock.MagicMock) -> None:
    mock_webpush.side_effect = WebPushException("Push failed")
    sub = {"endpoint": "https://push.example.com"}
    payload = {"title": "Test", "body": "Body"}
    
    res = send_push_notification(sub, payload, "fake-vapid-key")
    assert res is False
