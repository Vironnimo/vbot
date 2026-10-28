"""Inbound email that an email Channel hands to the mail triggers of its Channel.

An email Channel routes a mail from an allowed, authenticated sender into a
conversation. Every other mail goes to the :class:`InboundMailSink` the Runtime
binds (Cron's email jobs), so Channels never import automation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# How much body text one mail carries; longer text is cut and marked.
MAIL_TEXT_MAX_CHARS = 16_000


@dataclass(frozen=True, slots=True)
class MailAttachment:
    """One attachment of an inbound mail, described but not stored."""

    filename: str
    media_type: str
    size: int


@dataclass(frozen=True, slots=True)
class InboundMail:
    """One email an email Channel received that no conversation takes.

    Its content comes from whoever sent the mail: consumers treat every field
    except ``channel_id``, ``received_at``, ``folder`` and
    ``sender_authenticated`` as untrusted data, never as instructions.
    Timestamps are canonical UTC text (``core/utils/timestamps.py``).
    """

    channel_id: str
    # RFC 5322 Message-ID including its angle brackets; the key that makes a
    # repeated hand-over of the same mail recognizable.
    message_id: str
    received_at: str
    folder: str
    # Lowercased address of the From header and its display name ("" when none).
    sender: str
    sender_name: str
    # Whether the receiving mail server reported DMARC or an aligned DKIM pass
    # for the sender's domain; an unauthenticated From may be forged.
    sender_authenticated: bool
    # Lowercased To and Cc addresses.
    recipients: tuple[str, ...]
    subject: str
    # The Date header, or None when it is missing or cannot be read.
    sent_at: str | None
    # The body as plain text (HTML converted), at most MAIL_TEXT_MAX_CHARS.
    text: str
    text_cut: bool
    attachments: tuple[MailAttachment, ...] = ()


class InboundMailSink(Protocol):
    """Takes the mails of email Channels that no conversation takes."""

    async def accept_mail(self, mail: InboundMail) -> None:
        """Take one mail for the email triggers of its Channel.

        Returns once every trigger that wants the mail holds it durably, or no
        trigger wants it. Raising means the mail was not taken: the Channel
        keeps it unprocessed and hands it over again later. A hand-over can
        therefore repeat after a failure or restart; the sink takes each
        ``message_id`` of a Channel at most once per trigger.
        """
        ...
