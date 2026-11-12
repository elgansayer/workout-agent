"""Sends push notifications using Web Push API."""

import json
import logging
from typing import Any

try:
    from pywebpush import WebPushException, webpush
except ImportError:
    webpush = None  # type: ignore

    class WebPushException(Exception):  # type: ignore
        response: Any = None

logger = logging.getLogger(__name__)

def get_vapid_claims(email: str = "mailto:admin@example.com") -> dict[str, str]:
    return {
        "sub": email
    }

def send_push_notification(
    subscription_info: dict[str, Any],
    payload_data: dict[str, Any],
    vapid_private_key: str
) -> bool:
    """Send a Web Push notification to a single device subscription."""
    if webpush is None:
        logger.warning("pywebpush is not installed; skipping push notification.")
        return False
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload_data),
            vapid_private_key=vapid_private_key,
            vapid_claims=get_vapid_claims()
        )
        return True
    except WebPushException as ex:
        # The exception text and the push service's reply embed the subscription
        # endpoint (a capability URL) and provider diagnostics, so only the
        # status is recorded.
        status = getattr(ex.response, "status_code", None)
        logger.error("WebPush delivery failed (HTTP status %s).", status or "unavailable")
        return False
