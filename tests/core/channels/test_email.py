"""Channels: the email Channel's mailbox tracking, mail routing and sending."""

from __future__ import annotations

import asyncio
import imaplib
import logging
import smtplib
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

import core.channels.email as email_module
from core.channels._email_state import MailboxWatermark, MailThread
from core.channels._email_transport import FolderStatus, ImapMailbox, send_smtp
from core.channels.adapter import ConversationFacts, FileData
from core.channels.config import ChannelConfig, ChannelConfigError, ChannelError, EmailSettings
from core.channels.email import EmailChannelAdapter
from core.channels.engine import ChannelConversationEngine
from core.channels.mail import InboundMail, MailAttachment
from core.channels.state import ChannelStateStore
from tests.core.channels.channels_test_support import wait_until
from tests.core.channels.engine_test_support import channel_state

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_CHANNEL = "mail"
_ME = "me@example.com"
_SETTINGS = EmailSettings.from_dict(
    {"address": "bot@example.com", "imap_host": "imap.example.com", "smtp_host": "smtp.example.com"}
)
_PASS = "mx.example.net; dkim=pass header.d=example.com; dmarc=pass header.from=example.com"


def make_mail(
    *,
    sender: str = f"Me <{_ME}>",
    message_id: str = "<m1@example.com>",
    subject: str = "Plan",
    auth: str | None = _PASS,
    headers: dict[str, str] | None = None,
    text: str = "Hello",
    html: str | None = None,
    attachment: tuple[str, bytes] | None = None,
) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    if auth is not None:
        message["Authentication-Results"] = auth
    message["From"] = sender
    message["To"] = "Bot <bot@example.com>"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Tue, 27 Oct 2026 10:00:00 +0100"
    for key, value in (headers or {}).items():
        message[key] = value
    if html is None:
        message.set_content(text)
    else:
        message.set_content(html, subtype="html")
    if attachment is not None:
        message.add_attachment(
            attachment[1], maintype="application", subtype="pdf", filename=attachment[0]
        )
    return message.as_bytes()


@dataclass
class FakeFolder:
    uid_validity: int = 1
    uid_next: int = 1
    mails: dict[int, bytes] = field(default_factory=dict)

    def add(self, raw: bytes) -> int:
        uid = self.uid_next
        self.mails[uid] = raw
        self.uid_next += 1
        return uid


class FakeMailbox:
    def __init__(self, folders: dict[str, FakeFolder], *, supports_idle: bool) -> None:
        self.folders = folders
        self.supports_idle = supports_idle
        self.selected = ""
        self.idling = threading.Event()
        self.interrupted = threading.Event()

    def status(self, folder: str) -> FolderStatus:
        mailbox = self.folders[folder]
        return FolderStatus(uid_validity=mailbox.uid_validity, uid_next=mailbox.uid_next)

    def select(self, folder: str) -> None:
        self.selected = folder

    def uids_after(self, last_uid: int) -> list[int]:
        return sorted(uid for uid in self.folders[self.selected].mails if uid > last_uid)

    def fetch(self, uid: int) -> tuple[bytes, bool]:
        return self.folders[self.selected].mails.get(uid, b""), False

    def idle(self, seconds: float) -> None:
        self.idling.set()
        if self.interrupted.wait(5):
            raise OSError("connection shut down")

    def interrupt(self) -> None:
        self.interrupted.set()

    def close(self) -> None:
        pass


class FakeEngine:
    def __init__(self) -> None:
        self.inbound: list[tuple[ConversationFacts, str, int]] = []
        self.admit: list[bool] = []

    def attach(self, transport: object) -> None:
        pass

    async def handle_inbound_text(self, facts: ConversationFacts, text: str) -> bool:
        self.inbound.append((facts, text, 0))
        return self.admit.pop(0) if self.admit else True

    async def handle_inbound_media(
        self, facts: ConversationFacts, raw_messages: tuple[Any, ...], *, companion_text: str
    ) -> bool:
        self.inbound.append((facts, companion_text, len(raw_messages)))
        return self.admit.pop(0) if self.admit else True


class FakeSink:
    def __init__(self) -> None:
        self.mails: list[InboundMail] = []
        self.failures = 0

    async def accept_mail(self, mail: InboundMail) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("the triggers are busy")
        self.mails.append(mail)


class Clock:
    """Stands in for the adapter's waits; each ``tick`` runs one more mailbox pass."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._wakes: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        await self._wakes.get()

    async def tick(self) -> None:
        expected = len(self.delays) + 1
        self._wakes.put_nowait(None)
        await wait_until(lambda: len(self.delays) >= expected, timeout=2)


@dataclass
class Harness:
    adapter: EmailChannelAdapter
    state: ChannelStateStore
    inbox: FakeFolder
    mailbox: FakeMailbox
    engine: FakeEngine
    sink: FakeSink
    clock: Clock
    sent: list[EmailMessage]

    async def deliver(self, raw: bytes) -> None:
        self.inbox.add(raw)
        await self.clock.tick()

    def watermark(self) -> MailboxWatermark | None:
        return self.state.email.watermark(_CHANNEL, "INBOX")


def make_adapter(
    tmp_path: Path,
    *,
    require_sender_authentication: bool = True,
    mailbox: FakeMailbox | None = None,
) -> tuple[EmailChannelAdapter, ChannelStateStore, FakeMailbox, FakeEngine, FakeSink, list[Any]]:
    state = channel_state(tmp_path, _CHANNEL)
    mailbox = mailbox or FakeMailbox({"INBOX": FakeFolder()}, supports_idle=False)
    engine = FakeEngine()
    sink = FakeSink()
    sent: list[EmailMessage] = []
    config = ChannelConfig(
        id=_CHANNEL,
        platform="email",
        agent_id="assistant",
        allowed_chat_ids=[_ME, "you@mail.example.com"],
        token_env_var="EMAIL_PASSWORD_MAIL",
        email=EmailSettings.from_dict(
            {
                **_SETTINGS.to_dict(),
                "require_sender_authentication": require_sender_authentication,
            }
        ),
    )
    adapter = EmailChannelAdapter(
        config,
        cast(ChannelConversationEngine, engine),
        lambda _name: "secret",
        attachment_store=None,
        received_messages=state,
        mail_state=state.email,
        mail_sink=lambda: sink,
        open_mailbox=lambda _settings, _password: mailbox,
        send_mail=lambda _settings, _password, message: sent.append(message),
    )
    return adapter, state, mailbox, engine, sink, sent


@pytest_asyncio.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    clock = Clock()
    monkeypatch.setattr(email_module, "_sleep", clock.sleep)
    adapter, state, mailbox, engine, sink, sent = make_adapter(tmp_path)
    inbox = mailbox.folders["INBOX"]
    # A mail from before the first connect is not handled.
    inbox.add(make_mail(message_id="<old@example.com>"))
    task = asyncio.create_task(adapter.start())
    await wait_until(lambda: len(clock.delays) == 1, timeout=2)
    try:
        yield Harness(adapter, state, inbox, mailbox, engine, sink, clock, sent)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender", "auth", "headers", "to_conversation", "denied"),
    [
        (_ME, _PASS, {}, True, False),
        # A DKIM signature of a parent domain vouches for a subdomain sender.
        ("you@mail.example.com", "mx; dkim=pass header.d=example.com", {}, True, False),
        (_ME, "mx; dkim=pass header.d=other.example.net", {}, False, True),
        (_ME, "mx; dmarc=fail header.from=example.com", {}, False, True),
        (_ME, None, {}, False, True),
        (_ME, _PASS, {"Auto-Submitted": "auto-replied"}, False, False),
        (_ME, _PASS, {"Precedence": "list"}, False, False),
        ("stranger@example.com", _PASS, {}, False, False),
    ],
    ids=[
        "allowed",
        "parent-dkim",
        "foreign-dkim",
        "dmarc-fail",
        "unauthenticated",
        "auto-reply",
        "list",
        "not-allowed",
    ],
)
async def test_mail_routes_to_the_conversation_only_from_an_allowed_authenticated_person(
    harness: Harness,
    sender: str,
    auth: str | None,
    headers: dict[str, str],
    to_conversation: bool,
    denied: bool,
) -> None:
    await harness.deliver(make_mail(sender=sender, auth=auth, headers=headers))

    if to_conversation:
        assert harness.sink.mails == []
        [(facts, text, _media)] = harness.engine.inbound
        assert (facts.chat_id, facts.user_id, facts.kind) == (sender, sender, "direct")
        assert facts.message_id == facts.thread_id == "<m1@example.com>"
        assert text == "Subject: Plan\n\nHello"
    else:
        assert harness.engine.inbound == []
        assert [mail.message_id for mail in harness.sink.mails] == ["<m1@example.com>"]
    # Only an allowed sender the server did not vouch for shows up as denied.
    assert [entry.chat_id for entry in harness.adapter.denied_chats()] == (
        [sender] if denied else []
    )
    assert harness.watermark() == MailboxWatermark("imap.example.com/bot@example.com", 1, 2)


@pytest.mark.asyncio
async def test_mail_carries_its_facts_to_the_conversation_and_the_triggers(
    harness: Harness,
) -> None:
    report = ("report.pdf", b"%PDF-1.7")
    await harness.deliver(make_mail(attachment=report))
    await harness.deliver(
        make_mail(
            sender="Shop <News@Shop.example.org>",
            message_id="<n1@shop.example.org>",
            subject="Your invoice",
            html="<h1>Invoice</h1><p>Total <b>12 EUR</b></p>",
            attachment=report,
            headers={"Cc": "Other <Other@Example.com>"},
        )
    )
    # A mail without a Message-ID gets a stable stand-in.
    await harness.deliver(make_mail(sender="x@example.org").replace(b"Message-ID", b"X-Old-ID"))

    [(facts, text, media)] = harness.engine.inbound
    assert (facts.chat_id, text, media) == (_ME, "Subject: Plan\n\nHello", 1)
    invoice, anonymous = harness.sink.mails
    assert invoice == InboundMail(
        channel_id=_CHANNEL,
        message_id="<n1@shop.example.org>",
        received_at=invoice.received_at,
        folder="INBOX",
        sender="news@shop.example.org",
        sender_name="Shop",
        sender_authenticated=False,
        recipients=("bot@example.com", "other@example.com"),
        subject="Your invoice",
        sent_at="2026-10-27T09:00:00.000000Z",
        text="# Invoice\n\nTotal **12 EUR**",
        text_cut=False,
        attachments=(MailAttachment("report.pdf", "application/pdf", len(report[1])),),
    )
    assert anonymous.message_id.endswith(f"@{_CHANNEL}.vbot.invalid>")


@pytest.mark.asyncio
async def test_watermark_holds_a_mail_until_its_triggers_take_it(harness: Harness) -> None:
    harness.sink.failures = 1

    await harness.deliver(make_mail(sender="stranger@example.com"))
    assert harness.watermark() == MailboxWatermark("imap.example.com/bot@example.com", 1, 1)
    assert harness.clock.delays[-1] == email_module.RETRY_SECONDS[0]

    await harness.clock.tick()
    assert harness.watermark() == MailboxWatermark("imap.example.com/bot@example.com", 1, 2)
    assert len(harness.sink.mails) == 1
    # Back to the normal wait once the mail is taken.
    assert harness.clock.delays[-1] == email_module.POLL_SECONDS


@pytest.mark.asyncio
async def test_a_conversation_mail_the_queue_rejects_is_not_handed_over_again(
    harness: Harness,
) -> None:
    # The engine answered it with its busy reply; a retry would mail that reply again.
    harness.engine.admit = [False]

    await harness.deliver(make_mail())
    await harness.clock.tick()

    assert len(harness.engine.inbound) == 1
    assert harness.watermark() == MailboxWatermark("imap.example.com/bot@example.com", 1, 2)
    assert harness.clock.delays[-1] == email_module.POLL_SECONDS


@pytest.mark.asyncio
async def test_a_mail_that_starts_with_a_command_reaches_the_conversation_without_its_subject(
    harness: Harness,
) -> None:
    await harness.deliver(make_mail(subject="Re: Plan", text="/new\n\n> quoted"))

    [(_facts, text, _media)] = harness.engine.inbound
    assert text == "/new\n\n> quoted"


@pytest.mark.asyncio
async def test_a_changed_mailbox_restarts_at_its_newest_mail(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.inbox.uid_validity = 9
    harness.inbox.add(make_mail(message_id="<renumbered@example.com>"))

    with caplog.at_level(logging.WARNING):
        await harness.clock.tick()

    assert harness.engine.inbound == []
    assert harness.watermark() == MailboxWatermark("imap.example.com/bot@example.com", 9, 2)
    assert "Email folder restarts at its newest mail" in caplog.text
    await harness.deliver(make_mail())
    assert len(harness.engine.inbound) == 1


@pytest.mark.asyncio
async def test_replies_thread_and_own_mails_are_skipped(harness: Harness) -> None:
    harness.state.email.record_message(
        _CHANNEL, "<m1@example.com>", MailThread("Re: Plan", ("<m0@example.com>",)), outbound=False
    )

    await harness.adapter.send_text(_ME, "Done.", reply_to_message_id="<m1@example.com>")
    await harness.adapter.send(
        "## **Weekly** report\nAll green.",
        "Anna <You@Mail.Example.com>",
        files=[FileData(filename="a.txt", data=b"hi", media_type="text/plain")],
    )

    reply, report = harness.sent
    assert (reply["To"], reply["Subject"], reply["In-Reply-To"]) == (
        _ME,
        "Re: Plan",
        "<m1@example.com>",
    )
    assert reply["References"] == "<m0@example.com> <m1@example.com>"
    assert str(reply["Message-ID"]).endswith("@example.com>")
    assert (report["To"], report["Subject"], report["In-Reply-To"]) == (
        "you@mail.example.com",
        "Weekly report",
        None,
    )
    assert [part.get_filename() for part in report.iter_attachments()] == ["a.txt"]

    # The Channel's own mail, copied back into the folder, is not read as inbound.
    await harness.deliver(reply.as_bytes())
    assert harness.engine.inbound == []
    assert harness.sink.mails == []

    with pytest.raises(ChannelError, match="Buttons"):
        await harness.adapter.send("Pick", _ME, buttons=[[("Yes", "yes")]])
    with pytest.raises(ChannelConfigError, match="email address"):
        await harness.adapter.send("Hi", "12345")


@pytest.mark.asyncio
async def test_stop_ends_a_waiting_idle_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = FakeMailbox({"INBOX": FakeFolder()}, supports_idle=True)
    adapter, *_rest = make_adapter(tmp_path, mailbox=mailbox)
    task = asyncio.create_task(adapter.start())
    await wait_until(mailbox.idling.is_set, timeout=2)

    task.cancel()
    async with asyncio.timeout(2):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert mailbox.interrupted.is_set()


class FakeImapClient:
    capabilities = ("IMAP4REV1", "IDLE")
    size = 10

    def __init__(self, host: str, port: int, **_kwargs: Any) -> None:
        self.commands: list[tuple[Any, ...]] = []
        FakeImapClient.last = self

    def login(self, username: str, password: str) -> None:
        self.commands.append(("LOGIN", username))

    def status(self, folder: str, items: str) -> tuple[str, list[bytes]]:
        self.commands.append(("STATUS", folder))
        return "OK", [folder.encode() + b" (UIDNEXT 8 UIDVALIDITY 3)"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.commands.append(("SELECT", folder, readonly))
        return "OK", [b"7"]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.commands.append(("UID", command, *args))
        if command == "SEARCH":
            # "8:*" names the newest mail even when none is above 7.
            return "OK", [b"7"]
        if args[1] == "(RFC822.SIZE)":
            return "OK", [b"7 (UID 7 RFC822.SIZE %d)" % self.size]
        return "OK", [(b"7 (UID 7 BODY[] {5}", b"mail!"), b")"]

    def logout(self) -> None:
        pass

    last: FakeImapClient


@pytest.fixture
def imap_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImapClient)
    yield


@pytest.mark.usefixtures("imap_client")
def test_imap_mailbox_reads_without_changing_the_mailbox() -> None:
    mailbox = ImapMailbox(_SETTINGS, "secret")

    assert mailbox.status("Entwürfe") == FolderStatus(uid_validity=3, uid_next=8)
    mailbox.select("Entwürfe")
    assert mailbox.uids_after(7) == []
    assert mailbox.fetch(7) == (b"mail!", False)
    FakeImapClient.size = 60 * 1024 * 1024
    try:
        assert mailbox.fetch(7) == (b"mail!", True)
    finally:
        FakeImapClient.size = 10

    commands = FakeImapClient.last.commands
    assert ("SELECT", '"Entw&APw-rfe"', True) in commands
    fetched = [command[3] for command in commands if command[:2] == ("UID", "FETCH")]
    assert fetched == ["(RFC822.SIZE)", "(BODY.PEEK[])", "(RFC822.SIZE)", "(BODY.PEEK[HEADER])"]
    assert not any("STORE" in command for command in commands)


class FakeSmtp:
    fail: tuple[str, Any] = ("", None)
    sent: list[bytes] = []

    def __init__(self, host: str, port: int, **_kwargs: Any) -> None:
        self._step("connect")

    def _step(self, step: str) -> None:
        name, error = self.fail
        if name == step and isinstance(error, Exception):
            raise error

    def _code(self, step: str) -> tuple[int, bytes]:
        name, code = self.fail
        return (code if name == step and isinstance(code, int) else 250), b""

    def starttls(self, **_kwargs: Any) -> None:
        pass

    def login(self, username: str, password: str) -> None:
        self._step("login")

    def mail(self, sender: str) -> tuple[int, bytes]:
        return self._code("mail")

    def rcpt(self, recipient: str) -> tuple[int, bytes]:
        return self._code("rcpt")

    def data(self, message: bytes) -> None:
        self._step("data")
        FakeSmtp.sent.append(message)

    def quit(self) -> None:
        pass


@pytest.mark.parametrize(
    ("fail", "retryable", "possibly_delivered"),
    [
        (("connect", OSError("refused")), True, False),
        (("login", smtplib.SMTPAuthenticationError(535, b"no")), False, False),
        (("mail", 451), True, False),
        (("rcpt", 550), False, False),
        (("data", smtplib.SMTPDataError(554, b"spam")), False, False),
        (("data", smtplib.SMTPServerDisconnected("gone")), False, True),
    ],
    ids=["connect", "login", "sender-4xx", "recipient-5xx", "data-refused", "data-unconfirmed"],
)
def test_send_smtp_classifies_its_failures_for_delivery(
    monkeypatch: pytest.MonkeyPatch,
    fail: tuple[str, Any],
    retryable: bool,
    possibly_delivered: bool,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(FakeSmtp, "fail", fail)
    monkeypatch.setattr(FakeSmtp, "sent", [])
    message = BytesParser(policy=policy.default).parsebytes(make_mail(sender="bot@example.com"))

    with pytest.raises(ChannelError) as raised:
        send_smtp(_SETTINGS, "secret", cast(EmailMessage, message))

    assert (raised.value.retryable, raised.value.possibly_delivered) == (
        retryable,
        possibly_delivered,
    )
    assert FakeSmtp.sent == []
