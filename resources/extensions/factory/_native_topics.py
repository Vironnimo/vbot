"""Native shared discussions and their canonical delivery markers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.utils.timestamps import utc_now_timestamp

from ._native_records import (
    Json,
    NativeRecordStore,
    _dump,
    _error,
    _get,
    _ids,
    _insert,
    _member,
    _merge,
    _new_id,
    _revision,
    _save,
    _text,
)


class NativeTopicStore(NativeRecordStore):
    async def create_topic(
        self,
        factory_id: str,
        title: str,
        *,
        task_id: str = "",
        participants: Any = (),
        actor: str = "user",
        **fields: Any,
    ) -> Json:
        title = _text(title, "title", 200, required=True)
        participants = _ids(participants, "participants")
        initial_text = _text(fields.pop("initial_text", ""), "initial_text")

        def write(connection: sqlite3.Connection) -> Json:
            factory = _get(connection, "factory", factory_id)
            if factory["state"] == "archived":
                raise _error("factory_archived", "This Factory is archived.")
            if task_id and _get(connection, "task", task_id)["factory_id"] != factory_id:
                raise _error("factory_mismatch", "The linked task must belong to this Factory.")
            for member_id in participants:
                _member(connection, factory_id, member_id)
            topic = _insert(
                connection,
                "topic",
                {
                    **fields,
                    "id": _new_id(connection, "ftp"),
                    "factory_id": factory_id,
                    "title": title,
                    "task_id": task_id,
                    "state": "open",
                },
                actor,
            )
            connection.executemany(
                "INSERT INTO native_topic_participants (topic_id, member_id, joined_at) "
                "VALUES (?, ?, ?)",
                [(topic["id"], member_id, utc_now_timestamp()) for member_id in participants],
            )
            if initial_text:
                self._add_post(
                    connection, topic["id"], initial_text, actor, participants, "", actor
                )
            return _get(connection, "topic", topic["id"])

        return await self.database.write_async(write)

    async def get_topic(self, topic_id: str) -> Json:
        return await self._get("topic", topic_id)

    async def list_topics(
        self,
        factory_id: str | None = None,
        *,
        task_id: str | None = None,
        state: str | None = None,
        after: str = "",
        limit: int = 100,
    ) -> list[Json]:
        return await self._list(
            "topic", {"factory_id": factory_id, "task_id": task_id, "state": state}, after, limit
        )

    async def invite_topic(
        self,
        topic_id: str,
        member_id: str | list[str],
        *,
        actor: str = "user",
        expected_revision: int | None = None,
        text: str = "",
    ) -> Json:
        member_ids = _ids([member_id] if isinstance(member_id, str) else member_id, "member_ids")
        text = _text(text, "text")

        def write(connection: sqlite3.Connection) -> Json:
            topic = _get(connection, "topic", topic_id)
            _revision(topic, expected_revision)
            if topic["state"] != "open":
                raise _error("topic_closed", "Reopen this topic before inviting a member.")
            for invited_id in member_ids:
                _member(connection, topic["factory_id"], invited_id)
            if len(set(topic["participants"]) | set(member_ids)) > 100:
                raise _error("too_many_participants", "A topic supports at most 100 members.")
            changed = False
            for invited_id in member_ids:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO native_topic_participants "
                    "(topic_id, member_id, joined_at) VALUES (?, ?, ?)",
                    (topic_id, invited_id, utc_now_timestamp()),
                )
                changed = changed or bool(cursor.rowcount)
            if changed:
                _save(connection, "topic", topic, {}, actor, "invited")
            if text:
                self._add_post(connection, topic_id, text, actor, member_ids, "", actor)
            return _get(connection, "topic", topic_id)

        return await self.database.write_async(write)

    async def close_topic(
        self, topic_id: str, *, actor: str = "user", expected_revision: int | None = None
    ) -> Json:
        return await self.update_topic(
            topic_id, {"state": "closed"}, actor=actor, expected_revision=expected_revision
        )

    async def update_topic(
        self,
        topic_id: str,
        patch: Json,
        *,
        actor: str = "user",
        expected_revision: int | None = None,
    ) -> Json:
        def write(connection: sqlite3.Connection) -> Json:
            topic = _get(connection, "topic", topic_id)
            _revision(topic, expected_revision)
            proposed = _merge(topic, patch)
            if proposed["state"] not in {"open", "closed"}:
                raise _error("invalid_state", "Topic state must be open or closed.")
            _text(proposed["title"], "title", 200, required=True)
            if (
                proposed["task_id"]
                and _get(connection, "task", proposed["task_id"])["factory_id"]
                != topic["factory_id"]
            ):
                raise _error("factory_mismatch", "The linked task must belong to this Factory.")
            return _save(connection, "topic", topic, patch, actor, "updated")

        return await self.database.write_async(write)

    async def add_post(
        self,
        topic_id: str,
        text: str,
        *,
        author: str = "user",
        recipients: Any = (),
        receipt: str = "",
        actor: str | None = None,
    ) -> Json:
        text = _text(text, "text", 20000, required=True)
        recipients = _ids(recipients, "recipients")
        receipt = _text(receipt, "receipt", 500)

        return await self.database.write_async(
            lambda connection: self._add_post(
                connection, topic_id, text, author, recipients, receipt, actor
            )
        )

    def _add_post(
        self,
        connection: sqlite3.Connection,
        topic_id: str,
        text: str,
        author: str,
        recipients: list[str],
        receipt: str,
        actor: str | None,
    ) -> Json:
        topic = _get(connection, "topic", topic_id)
        if receipt:
            prior = connection.execute(
                "SELECT id, topic_id, sequence, author, text, recipients_json, receipt, "
                "created_at FROM native_posts WHERE topic_id = ? AND receipt = ?",
                (topic_id, receipt),
            ).fetchone()
            if prior is not None:
                post = self._post(prior)
                if (
                    post["text"] != text
                    or post["author"] != author
                    or post["recipients"] != recipients
                ):
                    raise _error(
                        "receipt_conflict",
                        "This post receipt already identifies different content.",
                    )
                return post
        if topic["state"] != "open":
            raise _error("topic_closed", "This topic is closed.")
        for member_id in recipients:
            _member(connection, topic["factory_id"], member_id)
            if member_id not in topic["participants"]:
                raise _error(
                    "participant_required",
                    "Invite the recipient before addressing a post to them.",
                )
        if author != "user":
            author_id = author.removeprefix("member:")
            _member(connection, topic["factory_id"], author_id)
            if author_id not in topic["participants"]:
                raise _error("participant_required", "Join the topic before posting as a member.")
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM native_posts WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()[0]
        post = {
            "id": _new_id(connection, "fpt"),
            "topic_id": topic_id,
            "sequence": sequence,
            "author": author,
            "text": text,
            "recipients": recipients,
            "receipt": receipt,
            "created_at": utc_now_timestamp(),
        }
        connection.execute(
            "INSERT INTO native_posts (id, topic_id, sequence, author, text, "
            "recipients_json, receipt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                post["id"],
                topic_id,
                sequence,
                author,
                text,
                _dump(recipients),
                receipt,
                post["created_at"],
            ),
        )
        _save(connection, "topic", topic, {}, actor or author, "posted")
        return post

    @staticmethod
    def _post(row: sqlite3.Row) -> Json:
        record = dict(row)
        record["recipients"] = json.loads(record.pop("recipients_json"))
        return record

    async def list_posts(self, topic_id: str, *, after: int = 0, limit: int = 100) -> list[Json]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(after) is not int or after < 0:
            raise _error(
                "invalid_limit", "Choose a nonnegative sequence and page size between 1 and 1000."
            )

        def read(connection: sqlite3.Connection) -> list[Json]:
            _get(connection, "topic", topic_id)
            return [
                self._post(row)
                for row in connection.execute(
                    "SELECT id, topic_id, sequence, author, text, recipients_json, receipt, "
                    "created_at FROM native_posts WHERE topic_id = ? AND sequence > ? ORDER BY "
                    "sequence LIMIT ?",
                    (topic_id, after, limit),
                )
            ]

        return await self.database.read_async(read)

    async def recent_posts(self, topic_id: str, *, limit: int = 100) -> list[Json]:
        """Read the bounded discussion tail in chronological order for an invitation."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise _error("invalid_limit", "Choose a page size between 1 and 1000.")

        def read(connection: sqlite3.Connection) -> list[Json]:
            _get(connection, "topic", topic_id)
            rows = connection.execute(
                "SELECT id, topic_id, sequence, author, text, recipients_json, receipt, "
                "created_at FROM native_posts WHERE topic_id = ? ORDER BY sequence DESC LIMIT ?",
                (topic_id, limit),
            ).fetchall()
            return [self._post(row) for row in reversed(rows)]

        return await self.database.read_async(read)

    async def topic_participants(self, topic_id: str) -> list[Json]:
        return await self.database.read_async(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    "SELECT topic_id, member_id, read_sequence, joined_at FROM "
                    "native_topic_participants WHERE topic_id = ? ORDER BY member_id",
                    (topic_id,),
                )
            ]
        )

    async def mark_topic_read(self, topic_id: str, member_id: str, sequence: int) -> None:
        if type(sequence) is not int or sequence < 0:
            raise _error("invalid_sequence", "Read sequence must be nonnegative.")

        def write(connection: sqlite3.Connection) -> None:
            maximum = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM native_posts WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()[0]
            if sequence > maximum:
                raise _error(
                    "invalid_sequence", "A read marker cannot pass the topic's latest post."
                )
            cursor = connection.execute(
                "UPDATE native_topic_participants SET read_sequence = MAX(read_sequence, "
                "?) WHERE topic_id = ? AND member_id = ?",
                (sequence, topic_id, member_id),
            )
            if not cursor.rowcount:
                raise _error(
                    "participant_required", "This member does not participate in the topic."
                )

        await self.database.write_async(write)
