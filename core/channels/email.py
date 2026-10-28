"""Email Channel: a mail account read over IMAP and written over SMTP.

A mail from an allowed sender whose domain the receiving server authenticated,
and that no machine sent, enters that correspondent's conversation; every other
mail goes to the bound ``InboundMailSink`` (Cron's email jobs). The Channel
never changes the mailbox: folders open read-only, mails are fetched with
``BODY.PEEK`` and a per-folder UID watermark in ``channels.db`` marks what was
handled. Blocking IMAP and SMTP work runs on the ``channel-email-io`` pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import imaplib
from collections.abc import Callable
from dataclasses import replace
from email.utils import parseaddr
from typing import Any, override

from core.attachments import AttachmentStore
from core.channels._email_messages import (
    ParsedAttachment,
    ParsedMail,
    build_mail,
    conversation_text,
    parse_mail,
    reply_subject,
    subject_from_text,
)
from core.channels._email_state import EmailMailState, MailboxWatermark, MailThread
from core.channels._email_transport import (
    ImapMailbox,
    MailboxConnection,
    MailSender,
    send_smtp,
)
from core.channels.adapter import (
    ChannelAdapter,
    ConversationFacts,
    DeniedChatFacts,
    DeniedChatLog,
    FileData,
    QuotedMessageFacts,
    ReceivedMessageStore,
    RouteFacts,
    content_blocks_for_attachment,
)
from core.channels.config import (
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    EmailSettings,
    normalize_email_address,
)
from core.channels.engine import ChannelConversationEngine
from core.channels.mail import InboundMailSink
from core.chat.content_blocks import ContentBlock
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("channels.email")
_IO_POOL = BoundedWorkerPool(name="channel-email-io", max_workers=8)
# Servers end an IDLE after about 30 minutes; renewing earlier keeps it alive.
IDLE_SECONDS = 9 * 60
# Without IDLE, or with several folders to watch, the Channel checks this often.
POLL_SECONDS = 60
# Waits before handling a mail again that its conversation or triggers could not take.
RETRY_SECONDS = (30, 60, 120, 300, 600)
_HEADER_ONLY_TEXT = "(This mail is larger than 50 MB; vBot read only its headers.)"

# Tests patch this seam instead of the process-wide ``asyncio.sleep``.
_sleep = asyncio.sleep


class EmailChannelAdapter(ChannelAdapter):
    """Platform I/O of one email Channel."""

    platform = "email"
    platform_display_name = "Email"

    def __init__(
        self,
        config: ChannelConfig,
        engine: ChannelConversationEngine,
        credential_resolver: Callable[[str], str],
        *,
        attachment_store: AttachmentStore | None,
        received_messages: ReceivedMessageStore,
        mail_state: EmailMailState,
        mail_sink: Callable[[], InboundMailSink | None],
        open_mailbox: Callable[[EmailSettings, str], MailboxConnection] = ImapMailbox,
        send_mail: MailSender = send_smtp,
    ) -> None:
        if config.email is None:
            raise ChannelConfigError("An email Channel needs its email settings")
        password = credential_resolver(config.token_env_var)
        if not isinstance(password, str) or not password:
            raise ChannelConfigError(f"Missing Email credential in {config.token_env_var}")
        self._config = config
        self._settings = config.email
        self._password = password
        self._engine = engine
        self._attachment_store = attachment_store
        self._received = received_messages
        self._mail_state = mail_state
        self._mail_sink = mail_sink
        self._open_mailbox = open_mailbox
        self._send_mail = send_mail
        self._allowed = frozenset(config.allowed_chat_ids)
        # The account the watermarks count for: another server or user restarts them.
        self._account = f"{self._settings.imap_host.lower()}/{self._settings.username}"
        self._denied = DeniedChatLog()
        self._mailbox: MailboxConnection | None = None
        self._connected = False
        self._retries = 0

    # -- Connection -------------------------------------------------------------------

    @override
    async def start(self) -> None:
        self._engine.attach(self)
        try:
            mailbox = await _IO_POOL.run(self._open_mailbox, self._settings, self._password)
        except ChannelError:
            raise
        except Exception:
            raise ChannelError("Email connection failed", retryable=True) from None
        self._mailbox = mailbox
        try:
            # The first pass also proves that every folder exists.
            handled = await self._sync(mailbox)
            self._connected = True
            self._report_connected()
            while True:
                await self._wait(mailbox, retry=not handled)
                handled = await self._sync(mailbox)
        except ChannelError:
            raise
        except OSError, imaplib.IMAP4.error:
            raise ChannelError("Email connection lost", retryable=True) from None
        finally:
            self._connected = False
            self._mailbox = None
            with contextlib.suppress(Exception):
                await _IO_POOL.run(mailbox.close)

    @override
    async def stop(self) -> None:
        self._connected = False
        mailbox = self._mailbox
        if mailbox is not None:
            mailbox.interrupt()

    def connection_status(self) -> dict[str, Any]:
        return {"connected": self._connected}

    @override
    def denied_chats(self) -> list[DeniedChatFacts]:
        return self._denied.entries()

    async def _mailbox_io(self, mailbox: MailboxConnection, function: Any, *args: Any) -> Any:
        """Run one blocking mailbox call; a cancellation ends a blocking read at once."""
        work = asyncio.ensure_future(_IO_POOL.run(function, *args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            mailbox.interrupt()
            with contextlib.suppress(Exception):
                await work
            raise

    async def _wait(self, mailbox: MailboxConnection, *, retry: bool) -> None:
        if retry:
            delay = RETRY_SECONDS[min(self._retries, len(RETRY_SECONDS) - 1)]
            self._retries += 1
            await _sleep(delay)
            return
        self._retries = 0
        folders = self._settings.folders
        if not mailbox.supports_idle:
            await _sleep(POLL_SECONDS)
            return
        # IDLE watches one folder; the others are checked after each IDLE.
        await self._mailbox_io(mailbox, mailbox.select, folders[0])
        seconds = IDLE_SECONDS if len(folders) == 1 else POLL_SECONDS
        await self._mailbox_io(mailbox, mailbox.idle, seconds)

    # -- Inbound ----------------------------------------------------------------------

    async def _sync(self, mailbox: MailboxConnection) -> bool:
        """Handle every new mail of every folder in order; False when one must wait."""
        for folder in self._settings.folders:
            if not await self._sync_folder(mailbox, folder):
                return False
        return True

    async def _sync_folder(self, mailbox: MailboxConnection, folder: str) -> bool:
        status = await self._mailbox_io(mailbox, mailbox.status, folder)
        database = self._mail_state.database
        watermark = await database.run_async(self._mail_state.watermark, self._config.id, folder)
        if (
            watermark is None
            or watermark.account != self._account
            or watermark.uid_validity != status.uid_validity
        ):
            if watermark is not None:
                _LOGGER.warning(
                    "Email folder restarts at its newest mail: the mailbox changed (channel=%s)",
                    self._config.id,
                )
            # A new or changed mailbox starts at its next mail, without a backlog.
            await database.run_async(
                self._mail_state.save_watermark,
                self._config.id,
                folder,
                MailboxWatermark(self._account, status.uid_validity, status.uid_next - 1),
            )
            return True
        if status.uid_next - 1 <= watermark.last_uid:
            return True
        await self._mailbox_io(mailbox, mailbox.select, folder)
        for uid in await self._mailbox_io(mailbox, mailbox.uids_after, watermark.last_uid):
            raw, header_only = await self._mailbox_io(mailbox, mailbox.fetch, uid)
            if raw and not await self._handle_mail(
                raw, header_only, folder, status.uid_validity, uid
            ):
                return False
            watermark = replace(watermark, last_uid=uid)
            await database.run_async(
                self._mail_state.save_watermark, self._config.id, folder, watermark
            )
        return True

    async def _handle_mail(
        self, raw: bytes, header_only: bool, folder: str, uid_validity: int, uid: int
    ) -> bool:
        """Route one mail; False when its conversation or triggers could not take it yet."""
        folder_key = hashlib.sha256(folder.encode("utf-8")).hexdigest()[:12]
        mail: ParsedMail = await _IO_POOL.run(
            parse_mail,
            raw,
            fallback_message_id=f"<{uid_validity}.{uid}.{folder_key}@{self._config.id}.vbot.invalid>",
        )
        if header_only:
            mail = replace(mail, text=_HEADER_ONLY_TEXT, text_cut=True, attachments=())
        database = self._mail_state.database
        if await database.run_async(self._mail_state.is_outbound, self._config.id, mail.message_id):
            return True
        allowed = mail.sender in self._allowed
        trusted = mail.sender_authenticated or not self._settings.require_sender_authentication
        if allowed and not trusted:
            # The operator sees why an allowed sender gets no answer.
            self._denied.record(chat_id=mail.sender, kind="direct", display_name=mail.sender_name)
        if allowed and trusted and not mail.automatic:
            return await self._to_conversation(mail)
        sink = self._mail_sink()
        if sink is None:
            return True
        try:
            await sink.accept_mail(
                mail.inbound_mail(
                    channel_id=self._config.id, folder=folder, received_at=utc_now_timestamp()
                )
            )
        except Exception:
            log = _LOGGER.warning if self._retries == 0 else _LOGGER.debug
            log(
                "Email mail not taken by its triggers; it is handed over again later (channel=%s)",
                self._config.id,
                exc_info=True,
            )
            return False
        return True

    async def _to_conversation(self, mail: ParsedMail) -> bool:
        if await self._received.has_received(self._config.id, mail.message_id):
            return True
        await self._mail_state.database.run_async(
            self._mail_state.record_message,
            self._config.id,
            mail.message_id,
            MailThread(subject=mail.subject, references=mail.references),
            outbound=False,
        )
        facts = ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=mail.sender,
            user_id=mail.sender,
            user_display_name=mail.sender_name or mail.sender,
            kind="direct",
            message_id=mail.message_id,
            thread_id=mail.message_id,
        )
        text = conversation_text(mail)
        if mail.attachments:
            admitted = await self._engine.handle_inbound_media(
                facts,
                tuple({"attachment": attachment} for attachment in mail.attachments),
                companion_text=text,
            )
        else:
            admitted = await self._engine.handle_inbound_text(facts, text)
        if admitted:
            await self._received.record_received(self._config.id, mail.message_id)
        return admitted

    # -- Conversation transport -------------------------------------------------------

    def caption_text(self, raw_message: Any) -> str | None:
        return None

    async def build_quoted_message(self, raw_message: Any) -> QuotedMessageFacts | None:
        return None

    async def delete_message(self, platform_target: str, message_id: str) -> bool:
        return False

    def activity_indicator(
        self, platform_target: str, thread_id: str | None = None
    ) -> contextlib.AbstractAsyncContextManager[None]:
        return contextlib.nullcontext()

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        attachment: ParsedAttachment = raw_message["attachment"]
        if self._attachment_store is None:
            raise ChannelError("Attachment storage is unavailable")
        self._attachment_store.ensure_within_limit(len(attachment.data))
        record = await self._attachment_store.store_async(attachment.filename, attachment.data)
        return content_blocks_for_attachment(record)

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
        buttons: Any = None,
    ) -> None:
        await self.send(
            text, platform_target, thread_id=thread_id or reply_to_message_id, buttons=buttons
        )

    # -- Outbound ---------------------------------------------------------------------

    @override
    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
        thread_id: str | None = None,
        buttons: Any = None,
    ) -> None:
        if buttons is not None:
            raise ChannelError("Buttons are not supported on Email")
        if not message and not files:
            raise ChannelConfigError("Provide a message or files")
        recipient = _recipient(platform_target)
        database = self._mail_state.database
        thread = (
            await database.run_async(self._mail_state.thread, self._config.id, thread_id)
            if thread_id
            else None
        )
        subject = (
            reply_subject(thread.subject) if thread is not None else subject_from_text(message)
        )
        references = thread.references if thread is not None else ()
        mail = build_mail(
            sender=self._settings.address,
            recipient=recipient,
            subject=subject,
            text=message,
            files=files,
            in_reply_to=thread_id or None,
            references=references,
        )
        await _IO_POOL.run(self._send_mail, self._settings, self._password, mail)
        try:
            await database.run_async(
                self._mail_state.record_message,
                self._config.id,
                str(mail["Message-ID"]),
                MailThread(
                    subject=subject,
                    references=(*references, thread_id) if thread_id else (),
                ),
                outbound=True,
            )
        except Exception:
            # The mail went out; failing the send now would only invite a duplicate.
            _LOGGER.warning(
                "Sent email not recorded; a reply to it may not thread (channel=%s)",
                self._config.id,
                exc_info=True,
            )

    @override
    async def ensure_outbound_session(
        self, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts:
        address = _recipient(platform_target)
        return await self._engine.ensure_channel_session(
            ConversationFacts(
                platform=self.platform,
                channel_id=self._config.id,
                chat_id=address,
                user_id=address,
                kind="direct",
                thread_id=thread_id,
            )
        )


def _recipient(platform_target: str) -> str:
    """The address of a target, also written as ``mailto:<address>`` or ``Name <address>``."""
    text = platform_target.strip()
    if text[:7].lower() == "mailto:":
        text = text[7:]
    _name, parsed = parseaddr(text)
    address = normalize_email_address(parsed) if parsed else None
    if address is None:
        raise ChannelConfigError(
            f'"{platform_target}" is not an email address. '
            "Use the recipient's address, such as name@example.com."
        )
    return address
