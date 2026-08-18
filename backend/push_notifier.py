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
        logger.error("WebPush Exception: %s", repr(ex))
        if ex.response is not None:
            try:
                extra = ex.response.json()
                logger.error("Remote service replied with %s", extra)
            except Exception:  # noqa: BLE001
                logger.error("Remote service replied with status %s: %s", ex.response.status_code, ex.response.text)
        return False
