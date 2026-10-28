"""Email Channel state in ``channels.db``: mailbox watermarks and recent mails.

Both tables belong to a registered Channel and leave with it, and with a
platform change (``ChannelStateStore.bind_platform``), through the registry's
cascading delete. The methods block; the email adapter runs them on the
database's worker pool (``Database.run_async``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core.channels._access_state import _registered_self_user_id
from core.channels.config import _normalize_channel_id
from core.database import Database
from core.utils.timestamps import utc_now_timestamp

# Recent mails kept per Channel for reply threading and own-mail recognition.
EMAIL_MESSAGE_WINDOW = 4096


@dataclass(frozen=True, slots=True)
class MailboxWatermark:
    """The last handled UID of one folder, valid only for its account and UIDVALIDITY."""

    account: str
    uid_validity: int
    last_uid: int


@dataclass(frozen=True, slots=True)
class MailThread:
    """What a reply to one recorded mail needs: its subject and References chain."""

    subject: str
    references: tuple[str, ...]


class EmailMailState:
    """Mailbox watermarks and recent mails of the email Channels."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    def watermark(self, channel_id: str, folder: str) -> MailboxWatermark | None:
        normalized_id = _normalize_channel_id(channel_id)
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT account, uid_validity, last_uid FROM channel_email_mailboxes "
                "WHERE channel_id = ? AND folder = ?",
                (normalized_id, folder),
            ).fetchone()
        if row is None:
            return None
        return MailboxWatermark(account=str(row[0]), uid_validity=int(row[1]), last_uid=int(row[2]))

    def save_watermark(self, channel_id: str, folder: str, watermark: MailboxWatermark) -> None:
        """Record the folder's watermark; refused for an unregistered Channel."""
        normalized_id = _normalize_channel_id(channel_id)
        updated_at = utc_now_timestamp()

        def save(connection: sqlite3.Connection) -> None:
            _registered_self_user_id(connection, normalized_id)
            connection.execute(
                "INSERT INTO channel_email_mailboxes "
                "(channel_id, folder, account, uid_validity, last_uid, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (channel_id, folder) DO UPDATE SET account = excluded.account, "
                "uid_validity = excluded.uid_validity, last_uid = excluded.last_uid, "
                "updated_at = excluded.updated_at",
                (
                    normalized_id,
                    folder,
                    watermark.account,
                    watermark.uid_validity,
                    watermark.last_uid,
                    updated_at,
                ),
            )

        self._database.write(save)

    def record_message(
        self,
        channel_id: str,
        message_id: str,
        thread: MailThread,
        *,
        outbound: bool,
    ) -> None:
        """Remember one mail, keeping the newest ``EMAIL_MESSAGE_WINDOW`` per Channel."""
        normalized_id = _normalize_channel_id(channel_id)
        recorded_at = utc_now_timestamp()

        def record(connection: sqlite3.Connection) -> None:
            _registered_self_user_id(connection, normalized_id)
            connection.execute(
                "INSERT INTO channel_email_messages "
                "(channel_id, message_id, subject, reference_ids, outbound, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (channel_id, message_id) DO UPDATE SET "
                "subject = excluded.subject, reference_ids = excluded.reference_ids, "
                "outbound = max(channel_email_messages.outbound, excluded.outbound), "
                "recorded_at = excluded.recorded_at",
                (
                    normalized_id,
                    message_id,
                    thread.subject,
                    " ".join(thread.references),
                    int(outbound),
                    recorded_at,
                ),
            )
            connection.execute(
                "DELETE FROM channel_email_messages WHERE channel_id = ? AND message_id IN ("
                "SELECT message_id FROM channel_email_messages WHERE channel_id = ? "
                "ORDER BY recorded_at DESC, message_id DESC LIMIT -1 OFFSET ?)",
                (normalized_id, normalized_id, EMAIL_MESSAGE_WINDOW),
            )

        self._database.write(record)

    def thread(self, channel_id: str, message_id: str) -> MailThread | None:
        normalized_id = _normalize_channel_id(channel_id)
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT subject, reference_ids FROM channel_email_messages "
                "WHERE channel_id = ? AND message_id = ?",
                (normalized_id, message_id),
            ).fetchone()
        if row is None:
            return None
        return MailThread(subject=str(row[0]), references=tuple(str(row[1]).split()))

    def is_outbound(self, channel_id: str, message_id: str) -> bool:
        """Return whether this Channel sent the mail itself."""
        normalized_id = _normalize_channel_id(channel_id)
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT outbound FROM channel_email_messages "
                "WHERE channel_id = ? AND message_id = ?",
                (normalized_id, message_id),
            ).fetchone()
        return row is not None and bool(row[0])
