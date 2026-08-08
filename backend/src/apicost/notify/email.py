"""Email delivery — ``EmailSender`` protocol plus two implementations.

BUILD_SPEC §2 names Resend, "interface-abstracted; Postmark/SendGrid
swappable". The protocol is the point: alert delivery is the part of this
product a user notices failing, and being able to change providers without
touching the alerting logic is worth one indirection.

Two implementations ship. ``SmtpSender`` talks to whatever ``smtp_host`` points
at, which in development is mailpit on :1025 — so the full alert path,
including the rendered message, is exercised locally with no account and no
network. ``ResendSender`` is the production transport.

Nothing here ever raises into the caller. A failed alert email must not take
down the worker draining the ledger behind it.
"""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

import httpx

from apicost.config import Settings, get_settings
from apicost.core.logging import get_logger

__all__ = [
    "EmailSender",
    "Message",
    "ResendSender",
    "SmtpSender",
    "get_sender",
]

_logger = get_logger(__name__)


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str
    html: str | None = None


@runtime_checkable
class EmailSender(Protocol):
    async def send(self, message: Message) -> bool:
        """Deliver one message. Returns whether it was accepted."""
        ...


class SmtpSender:
    """Plain SMTP. Mailpit in development; any relay in production.

    ``smtplib`` is synchronous, so the actual send goes to a thread. Blocking
    the event loop here would stall the ledger drain sharing it — the alert
    would arrive and the queue behind it would back up.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def send(self, message: Message) -> bool:
        try:
            await asyncio.to_thread(self._send_sync, message)
            _logger.info("email_sent", subsystem="notify", transport="smtp")
            return True
        except Exception as exc:
            # Logged by type and message only. An SMTP exception can echo the
            # server banner and, on an authenticated relay, the credentials
            # used (hard rule 3).
            _logger.warning(
                "email_send_failed",
                subsystem="notify",
                transport="smtp",
                error_type=type(exc).__name__,
            )
            return False

    def _send_sync(self, message: Message) -> None:
        email = EmailMessage()
        email["From"] = self._settings.email_from
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.text)
        if message.html:
            email.add_alternative(message.html, subtype="html")

        with smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port, timeout=10) as smtp:
            smtp.send_message(email)


class ResendSender:
    """Resend's HTTP API (BUILD_SPEC §2)."""

    ENDPOINT = "https://api.resend.com/emails"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def send(self, message: Message) -> bool:
        api_key = self._settings.resend_api_key.get_secret_value()
        if not api_key:
            _logger.warning("email_not_configured", subsystem="notify", transport="resend")
            return False

        payload: dict[str, object] = {
            "from": self._settings.email_from,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self.ENDPOINT,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except Exception as exc:
            _logger.warning(
                "email_send_failed",
                subsystem="notify",
                transport="resend",
                error_type=type(exc).__name__,
            )
            return False

        if response.status_code >= 400:
            # The status code, never the body: an auth failure from Resend
            # echoes part of the key back (hard rule 3).
            _logger.warning(
                "email_rejected",
                subsystem="notify",
                transport="resend",
                status=response.status_code,
            )
            return False

        _logger.info("email_sent", subsystem="notify", transport="resend")
        return True


def get_sender(settings: Settings | None = None) -> EmailSender:
    """Pick a transport from config.

    Resend when a key is present, SMTP otherwise. That ordering means a
    development machine with no key still delivers to mailpit rather than
    silently dropping alerts, which is the failure mode that lets a broken
    alert template reach production unnoticed.
    """
    resolved = settings or get_settings()
    if resolved.resend_api_key.get_secret_value():
        return ResendSender(resolved)
    return SmtpSender(resolved)
