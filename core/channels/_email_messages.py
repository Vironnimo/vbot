"""Reading inbound mails and building outbound ones for the email Channel.

Pure functions over the standard library's ``email`` package; the adapter owns
every mailbox and server connection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, make_msgid, parsedate_to_datetime

from core.channels.adapter import FileData
from core.channels.config import normalize_email_address
from core.channels.mail import MAIL_TEXT_MAX_CHARS, InboundMail, MailAttachment
from core.utils.timestamps import format_canonical_timestamp

_SUBJECT_MAX_CHARS = 78
_DEFAULT_SUBJECT = "Message"
_AUTOMATIC_PRECEDENCE = frozenset(("bulk", "junk", "list"))
_BOUNCE_LOCAL_PARTS = frozenset(("mailer-daemon", "postmaster"))
_REPLY_PREFIX = re.compile(r"^\s*(re|aw|sv|antw)\s*:\s*", re.IGNORECASE)
_COMMENT = re.compile(r"\([^()]*\)")
_PROPERTY = re.compile(r"\b(header\.[a-z]+)\s*=\s*\"?([^\s;\"]+)\"?", re.IGNORECASE)
_RESULT = re.compile(r"^\s*([a-z0-9-]+)\s*=\s*([a-z]+)", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_LEAD = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)+")
_MARKDOWN_MARKS = re.compile(r"[*_`~]+")
_TEXT_CUT_MARK = "\n\n[Cut: the mail is longer.]"


@dataclass(frozen=True, slots=True)
class ParsedAttachment:
    filename: str
    media_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ParsedMail:
    """One inbound mail as the email Channel routes it."""

    message_id: str
    sender: str
    sender_name: str
    sender_authenticated: bool
    automatic: bool
    recipients: tuple[str, ...]
    subject: str
    sent_at: str | None
    text: str
    text_cut: bool
    attachments: tuple[ParsedAttachment, ...]
    references: tuple[str, ...]

    def inbound_mail(self, *, channel_id: str, folder: str, received_at: str) -> InboundMail:
        return InboundMail(
            channel_id=channel_id,
            message_id=self.message_id,
            received_at=received_at,
            folder=folder,
            sender=self.sender,
            sender_name=self.sender_name,
            sender_authenticated=self.sender_authenticated,
            recipients=self.recipients,
            subject=self.subject,
            sent_at=self.sent_at,
            text=self.text,
            text_cut=self.text_cut,
            attachments=tuple(
                MailAttachment(
                    filename=attachment.filename,
                    media_type=attachment.media_type,
                    size=len(attachment.data),
                )
                for attachment in self.attachments
            ),
        )


def parse_mail(raw: bytes, *, fallback_message_id: str) -> ParsedMail:
    """Read one RFC 5322 mail; ``fallback_message_id`` stands in for a missing Message-ID."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    name, sender = _first_address(message, "From")
    message_id = _message_id(message.get("Message-ID")) or fallback_message_id
    text, cut = _body_text(message)
    return ParsedMail(
        message_id=message_id,
        sender=sender,
        sender_name=name,
        sender_authenticated=sender_authenticated(message, sender),
        automatic=is_automatic(message, sender),
        recipients=tuple(
            address
            for _name, value in getaddresses(
                [str(header) for key in ("To", "Cc") for header in message.get_all(key, [])]
            )
            if (address := normalize_email_address(value)) is not None
        ),
        subject=_header_text(message.get("Subject")),
        sent_at=_sent_at(message.get("Date")),
        text=text,
        text_cut=cut,
        attachments=tuple(_attachments(message)),
        references=_references(message),
    )


def sender_authenticated(message: Message, sender: str) -> bool:
    """Whether the receiving server's topmost Authentication-Results vouches for the From.

    Authenticated means ``dmarc=pass`` for the From domain, or ``dkim=pass`` for
    a signing domain that equals the From domain or is a parent of it.
    """
    domain = sender.rpartition("@")[2]
    headers = message.get_all("Authentication-Results") or []
    if not domain or not headers:
        return False
    # The authserv-id leads; result clauses follow, separated by semicolons.
    clauses = _COMMENT.sub(" ", str(headers[0])).split(";")[1:]
    for clause in clauses:
        result = _RESULT.match(clause)
        if result is None or result.group(2).lower() != "pass":
            continue
        method = result.group(1).lower()
        properties = {key.lower(): value.lower() for key, value in _PROPERTY.findall(clause)}
        if method == "dmarc":
            header_from = properties.get("header.from")
            if header_from is None or header_from == domain:
                return True
        elif method == "dkim":
            signer = properties.get("header.d") or properties.get("header.i", "")
            signer = signer.rpartition("@")[2]
            if signer and (domain == signer or domain.endswith(f".{signer}")):
                return True
    return False


def is_automatic(message: Message, sender: str) -> bool:
    """Whether a mail was sent by a machine: auto-replies, bulk and list mail, bounces."""
    auto_submitted = str(message.get("Auto-Submitted", "")).strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if str(message.get("Precedence", "")).strip().lower() in _AUTOMATIC_PRECEDENCE:
        return True
    return not sender or sender.partition("@")[0] in _BOUNCE_LOCAL_PARTS


def conversation_text(mail: ParsedMail) -> str:
    """The text a conversation receives: the subject line, a blank line, the body.

    A body that starts with a Command (``/new``) stays alone, so the Command runs.
    """
    if mail.text.lstrip().startswith("/"):
        return mail.text.strip()
    subject = mail.subject or "(no subject)"
    return f"Subject: {subject}\n\n{mail.text}".rstrip()


def reply_subject(subject: str) -> str:
    """``Re: <subject>`` without stacking another reply prefix."""
    base = subject.strip()
    while (match := _REPLY_PREFIX.match(base)) is not None:
        base = base[match.end() :]
    return f"Re: {base}" if base else f"Re: {_DEFAULT_SUBJECT}"


def subject_from_text(text: str | None) -> str:
    """A subject from the first non-empty line, without Markdown markers."""
    for line in (text or "").splitlines():
        plain = _MARKDOWN_LINK.sub(r"\1", line)
        plain = _MARKDOWN_MARKS.sub("", _MARKDOWN_LEAD.sub("", plain)).strip()
        if not plain:
            continue
        if len(plain) <= _SUBJECT_MAX_CHARS:
            return plain
        cut = plain[: _SUBJECT_MAX_CHARS - 1].rsplit(" ", 1)[0].rstrip()
        return f"{cut or plain[: _SUBJECT_MAX_CHARS - 1]}…"
    return _DEFAULT_SUBJECT


def build_mail(
    *,
    sender: str,
    recipient: str,
    subject: str,
    text: str | None,
    files: list[FileData] | None,
    in_reply_to: str | None = None,
    references: tuple[str, ...] = (),
    now: datetime | None = None,
) -> EmailMessage:
    """Build one outbound plain-text mail with a fresh Message-ID on the sender's domain."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(now or datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join((*references, in_reply_to))
    message.set_content(text or "", subtype="plain", charset="utf-8")
    for file in files or []:
        maintype, _, subtype = file.media_type.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            file.data, maintype=maintype, subtype=subtype, filename=file.filename
        )
    return message


def _first_address(message: Message, header: str) -> tuple[str, str]:
    for name, value in getaddresses([str(item) for item in message.get_all(header, [])]):
        address = normalize_email_address(value)
        if address is not None:
            return name.strip(), address
    return "", ""


def _message_id(value: object) -> str | None:
    text = str(value or "").strip()
    match = re.search(r"<[^<>\s]+>", text)
    return match.group(0) if match else None


def _references(message: Message) -> tuple[str, ...]:
    text = " ".join(str(message.get(key, "")) for key in ("References",))
    return tuple(re.findall(r"<[^<>\s]+>", text))


def _header_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _sent_at(value: object) -> str | None:
    if value is None:
        return None
    try:
        sent = parsedate_to_datetime(str(value))
    except TypeError, ValueError, IndexError:
        return None
    if sent.tzinfo is None:
        return None
    return format_canonical_timestamp(sent.astimezone(UTC))


def _body_text(message: EmailMessage | Message) -> tuple[str, bool]:
    body = (
        message.get_body(preferencelist=("plain", "html"))
        if isinstance(message, EmailMessage)
        else None
    )
    if body is None:
        return "", False
    text = _part_text(body)
    if body.get_content_subtype() == "html":
        text = _html_text(text)
    text = text.replace("\r\n", "\n").strip()
    if len(text) <= MAIL_TEXT_MAX_CHARS:
        return text, False
    limit = MAIL_TEXT_MAX_CHARS - len(_TEXT_CUT_MARK)
    cut = text[:limit]
    newline = cut.rfind("\n", limit // 2)
    return (cut[:newline] if newline > 0 else cut).rstrip() + _TEXT_CUT_MARK, True


def _part_text(part: Message) -> str:
    try:
        content = part.get_content() if isinstance(part, EmailMessage) else part.get_payload()
    except LookupError, ValueError:
        content = None
    if isinstance(content, str):
        return content
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def _html_text(html: str) -> str:
    from markdownify import markdownify

    try:
        return str(markdownify(html, strip=["img", "script", "style"], heading_style="ATX"))
    except Exception:
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser").get_text("\n")


def _attachments(message: Message) -> list[ParsedAttachment]:
    if not isinstance(message, EmailMessage):
        return []
    attachments: list[ParsedAttachment] = []
    for part in message.iter_attachments():
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        attachments.append(
            ParsedAttachment(
                filename=part.get_filename() or "attachment",
                media_type=part.get_content_type(),
                data=payload,
            )
        )
    return attachments
