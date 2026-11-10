"""Minimal SMTP delivery for account recovery and security notices.

The web process never invents a delivery: when SMTP is not configured the
caller receives :class:`EmailDeliveryNotConfigured` and can log a safe
operational warning. Recovery requests deliberately keep the same public
response either way so the endpoint cannot be used to probe addresses.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage as _SMTPMessage


class EmailDeliveryError(RuntimeError):
    """Base class for account-email delivery failures."""


class EmailDeliveryNotConfigured(EmailDeliveryError):
    """SMTP settings are incomplete; no message was sent."""


@dataclass(frozen=True, slots=True)
class AccountEmail:
    recipient: str
    subject: str
    body: str


def _value(name: str) -> str:
    return os.environ.get(name, "").strip()


def _truthy(name: str, default: bool) -> bool:
    raw = _value(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def smtp_configured() -> bool:
    """Whether the minimum SMTP settings are present."""

    return bool(_value("SMTP_HOST") and _value("SMTP_FROM"))


def send_email(message: AccountEmail) -> None:
    """Send an account email over SMTP.

    STARTTLS is the default for the common submission port. Set
    ``SMTP_USE_SSL=1`` for implicit TLS (usually port 465) or
    ``SMTP_USE_TLS=0`` only on an already protected internal relay.
    """

    host = _value("SMTP_HOST")
    sender = _value("SMTP_FROM")
    if not host or not sender:
        raise EmailDeliveryNotConfigured("SMTP_HOST and SMTP_FROM are required.")
    try:
        port = int(_value("SMTP_PORT") or "587")
    except ValueError as exc:
        raise EmailDeliveryNotConfigured("SMTP_PORT must be an integer.") from exc
    username = _value("SMTP_USERNAME")
    password = _value("SMTP_PASSWORD")
    use_ssl = _truthy("SMTP_USE_SSL", False)
    use_tls = _truthy("SMTP_USE_TLS", not use_ssl)
    timeout = 15.0

    smtp_message = _SMTPMessage()
    smtp_message["From"] = sender
    smtp_message["To"] = message.recipient
    smtp_message["Subject"] = message.subject
    smtp_message.set_content(message.body)

    try:
        if use_ssl:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                host,
                port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        with client:
            client.ehlo()
            if use_tls and not use_ssl:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if username:
                client.login(username, password)
            client.send_message(smtp_message)
    except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
        raise EmailDeliveryError(
            f"Account email delivery failed ({type(exc).__name__})."
        ) from exc


__all__ = [
    "AccountEmail",
    "EmailDeliveryError",
    "EmailDeliveryNotConfigured",
    "send_email",
    "smtp_configured",
]
