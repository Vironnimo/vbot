"""Blocking IMAP and SMTP connections of the email Channel.

Every method blocks and runs on the email Channel's worker pool, except
``MailboxConnection.interrupt``, which the Event Loop calls to end a blocking
read (IDLE) promptly when the Channel stops. TLS always uses the shared
context; mailboxes are opened read-only and mails fetched with ``BODY.PEEK``,
so the Channel never changes a flag in the user's mailbox.
"""

from __future__ import annotations

import base64
import contextlib
import imaplib
import re
import smtplib
import socket
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from core.channels.config import ChannelConfigError, ChannelError, EmailSettings
from core.utils.tls import shared_ssl_context

_TIMEOUT_SECONDS = 30
# Larger mails are read from their headers only; their body is not fetched.
MAIL_FETCH_MAX_BYTES = 50 * 1024 * 1024
_STATUS_VALUE = re.compile(rb"(UIDNEXT|UIDVALIDITY)\s+(\d+)", re.IGNORECASE)
_SIZE_VALUE = re.compile(rb"RFC822\.SIZE\s+(\d+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FolderStatus:
    uid_validity: int
    uid_next: int


class MailboxConnection(Protocol):
    """One logged-in IMAP connection, as the email Channel uses it."""

    @property
    def supports_idle(self) -> bool: ...

    def status(self, folder: str) -> FolderStatus: ...

    def select(self, folder: str) -> None:
        """Open ``folder`` read-only."""

    def uids_after(self, last_uid: int) -> list[int]:
        """Return the selected folder's UIDs above ``last_uid``, ascending."""

    def fetch(self, uid: int) -> tuple[bytes, bool]:
        """Return one mail of the selected folder and whether only its header was read."""

    def idle(self, seconds: float) -> None:
        """Wait in IDLE until the selected folder changes or ``seconds`` pass."""

    def interrupt(self) -> None:
        """End a blocking read from another thread; the connection is unusable afterwards."""

    def close(self) -> None: ...


class MailSender(Protocol):
    def __call__(self, settings: EmailSettings, password: str, message: EmailMessage) -> None:
        """Send ``message`` to its To address; raise ``ChannelError`` on failure."""


def quote_folder(folder: str) -> str:
    """A folder name as an IMAP quoted string in modified UTF-7 (RFC 3501 5.1.3)."""
    encoded: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            data = base64.b64encode("".join(pending).encode("utf-16-be")).decode("ascii")
            encoded.append(f"&{data.rstrip('=').replace('/', ',')}-")
            pending.clear()

    for character in folder:
        if 0x20 <= ord(character) <= 0x7E:
            flush()
            encoded.append("&-" if character == "&" else character)
        else:
            pending.append(character)
    flush()
    escaped = "".join(encoded).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class ImapMailbox:
    """``MailboxConnection`` over ``imaplib.IMAP4_SSL``."""

    def __init__(self, settings: EmailSettings, password: str) -> None:
        try:
            client = imaplib.IMAP4_SSL(
                settings.imap_host,
                settings.imap_port,
                ssl_context=shared_ssl_context(),
                timeout=_TIMEOUT_SECONDS,
            )
        except (OSError, imaplib.IMAP4.error) as error:
            raise ChannelError(
                f"Cannot reach the IMAP server ({type(error).__name__})", retryable=True
            ) from None
        try:
            client.login(settings.username, password)
        except imaplib.IMAP4.error:
            with contextlib.suppress(Exception):
                client.logout()
            raise ChannelConfigError("The IMAP server refused the login") from None
        except OSError as error:
            raise ChannelError(
                f"IMAP login failed ({type(error).__name__})", retryable=True
            ) from None
        self._client = client

    @property
    def supports_idle(self) -> bool:
        return "IDLE" in self._client.capabilities

    def status(self, folder: str) -> FolderStatus:
        typ, data = self._client.status(quote_folder(folder), "(UIDNEXT UIDVALIDITY)")
        values = {
            key.upper(): int(value)
            for line in data
            if isinstance(line, bytes)
            for key, value in _STATUS_VALUE.findall(line)
        }
        if typ != "OK" or b"UIDVALIDITY" not in values or b"UIDNEXT" not in values:
            raise ChannelConfigError(f"The IMAP folder {folder!r} cannot be opened")
        return FolderStatus(uid_validity=values[b"UIDVALIDITY"], uid_next=values[b"UIDNEXT"])

    def select(self, folder: str) -> None:
        typ, _data = self._client.select(quote_folder(folder), readonly=True)
        if typ != "OK":
            raise ChannelConfigError(f"The IMAP folder {folder!r} cannot be opened")

    def uids_after(self, last_uid: int) -> list[int]:
        typ, data = self._client.uid("SEARCH", "UID", f"{last_uid + 1}:*")
        if typ != "OK":
            raise ChannelError("IMAP search failed", retryable=True)
        uids = {int(value) for line in data if line for value in line.split()}
        # "n:*" also names the newest mail when every UID is below n.
        return sorted(uid for uid in uids if uid > last_uid)

    def fetch(self, uid: int) -> tuple[bytes, bool]:
        typ, data = self._client.uid("FETCH", str(uid), "(RFC822.SIZE)")
        size_match = _SIZE_VALUE.search(b" ".join(item for item in data if isinstance(item, bytes)))
        header_only = size_match is not None and int(size_match.group(1)) > MAIL_FETCH_MAX_BYTES
        section = "(BODY.PEEK[HEADER])" if header_only else "(BODY.PEEK[])"
        typ, data = self._client.uid("FETCH", str(uid), section)
        for item in data:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                return item[1], header_only
        if typ != "OK":
            raise ChannelError("IMAP fetch failed", retryable=True)
        # The mail is gone (moved or deleted) since the search.
        return b"", False

    def idle(self, seconds: float) -> None:
        with self._client.idle(duration=seconds) as idler:
            for typ, _data in idler:
                if typ.upper() in {"EXISTS", "RECENT"}:
                    return

    def interrupt(self) -> None:
        with contextlib.suppress(OSError):
            self._client.sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.logout()


def send_smtp(settings: EmailSettings, password: str, message: EmailMessage) -> None:
    """Send one mail; classify every failure for reply delivery.

    A failure before the message data was handed over cannot have delivered it;
    one while handing it over without the server's answer may have.
    """
    recipient = str(message["To"])
    context = shared_ssl_context()
    try:
        if settings.smtp_security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT_SECONDS, context=context
            )
        else:
            client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT_SECONDS)
            client.starttls(context=context)
        client.login(settings.username, password)
    except smtplib.SMTPAuthenticationError:
        raise ChannelError("The SMTP server refused the login") from None
    except (OSError, smtplib.SMTPException) as error:
        raise ChannelError(
            f"Cannot reach the SMTP server ({type(error).__name__})", retryable=True
        ) from None
    try:
        try:
            code, _reply = client.mail(settings.address)
            if code != 250:
                raise _refused("sender", code)
            code, _reply = client.rcpt(recipient)
            if code not in {250, 251}:
                raise _refused("recipient", code)
        except OSError, smtplib.SMTPServerDisconnected:
            raise ChannelError("The SMTP server closed the connection", retryable=True) from None
        try:
            client.data(message.as_bytes())
        except smtplib.SMTPDataError as error:
            raise _refused("message", error.smtp_code) from None
        except OSError, smtplib.SMTPException:
            raise ChannelError(
                "The SMTP server did not confirm the message", possibly_delivered=True
            ) from None
    finally:
        with contextlib.suppress(Exception):
            client.quit()


def _refused(what: str, code: int) -> ChannelError:
    return ChannelError(f"The SMTP server refused the {what} ({code})", retryable=400 <= code < 500)
