"""Minimal SMTP delivery for account-security e-mail.

Recovery links and change-verification messages must leave the application, but
the deployment may not use a hosted e-mail API. This module therefore speaks
plain SMTP using the standard library and reads every credential from the
environment. It has no provider-specific SDK and adds no runtime dependency.

Configuration (all optional; delivery is disabled until host and sender are set):

``WEB_SMTP_HOST``
    SMTP server hostname.
``WEB_SMTP_PORT``
    Port. Defaults to 587 with STARTTLS, or 465 with implicit TLS.
``WEB_SMTP_USERNAME`` / ``WEB_SMTP_PASSWORD``
    Optional SMTP credentials. Never logged or returned.
``WEB_SMTP_FROM``
    Envelope/header sender address.
``WEB_SMTP_STARTTLS``
    ``1``/``true`` (default) to upgrade a plaintext connection with STARTTLS.
``WEB_SMTP_SSL``
    ``1``/``true`` for implicit TLS (usually port 465).
``WEB_SMTP_TIMEOUT_SECONDS``
    Socket timeout, default 15.

A missing configuration raises :class:`EmailNotConfigured` rather than pretending
to deliver, so a caller cannot report a recovery message as sent when it was not.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    """Base class for e-mail delivery failures."""


class EmailNotConfigured(EmailDeliveryError):
    """The deployment has not configured an outbound SMTP server."""


def _flag(value: str | None, default: bool) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _port(value: str | None, default: int) -> int:
    text = (value or "").strip()
    if not text:
        return default
    try:
        port = int(text)
    except ValueError as exc:
        raise EmailNotConfigured("WEB_SMTP_PORT must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise EmailNotConfigured("WEB_SMTP_PORT is out of range.")
    return port


@dataclass(frozen=True, slots=True)
class EmailConfig:
    host: str
    port: int
    sender: str
    username: str | None
    password: str | None
    starttls: bool
    implicit_tls: bool
    timeout: float

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    @classmethod
    def from_environ(cls) -> EmailConfig:
        implicit_tls = _flag(os.environ.get("WEB_SMTP_SSL"), False)
        return cls(
            host=os.environ.get("WEB_SMTP_HOST", "").strip(),
            port=_port(os.environ.get("WEB_SMTP_PORT"), 465 if implicit_tls else 587),
            sender=os.environ.get("WEB_SMTP_FROM", "").strip(),
            username=os.environ.get("WEB_SMTP_USERNAME", "").strip() or None,
            password=os.environ.get("WEB_SMTP_PASSWORD", "").strip() or None,
            starttls=_flag(os.environ.get("WEB_SMTP_STARTTLS"), not implicit_tls),
            implicit_tls=implicit_tls,
            timeout=float(os.environ.get("WEB_SMTP_TIMEOUT_SECONDS", "15").strip() or 15),
        )


def send_email(to: str, subject: str, body: str) -> None:
    """Deliver one plain-text message, raising a sanitised error on failure."""

    config = EmailConfig.from_environ()
    if not config.configured:
        raise EmailNotConfigured("Outbound e-mail is not configured.")
    if not to or not subject or not body:
        raise EmailDeliveryError("An e-mail needs a recipient, subject and body.")

    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    try:
        if config.implicit_tls:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                config.host, config.port, timeout=config.timeout, context=context
            )
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
        with client:
            client.ehlo()
            if config.starttls and not config.implicit_tls:
                client.starttls(context=context)
                client.ehlo()
            if config.username and config.password:
                client.login(config.username, config.password)
            client.send_message(message)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        # Never include the address, credentials or server response in the log.
        logger.warning("SMTP delivery failed (%s).", type(exc).__name__)
        raise EmailDeliveryError("E-mail delivery failed.") from exc


__all__ = [
    "EmailConfig",
    "EmailDeliveryError",
    "EmailNotConfigured",
    "send_email",
]
