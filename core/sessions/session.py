"""Path-free Session handle and buffered note lifecycle."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.chat.errors import ChatSessionError
from core.sessions._io import (
    _encode_chat_history_cursor,
    _run_session_io,
)
from core.sessions._metadata import _decode_chat_history_cursor
from core.sessions._types import (
    JsonObject,
    SessionAddress,
    SessionChatHistorySnapshot,
    SessionHistoryCheckpoint,
    SessionHistoryRecord,
    SessionHistorySectionStats,
    SessionHistorySnapshot,
    SessionMessagePage,
    SessionReadBatch,
    SessionReadCursor,
    SessionRunResult,
    SessionStatusSnapshot,
)
from core.sessions.history import (
    _skill_context_note_content,
    current_skill_activation_contents,
)
from core.sessions.store import SessionStore

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


class ChatSession:
    """Path-free Session handle backed by the canonical SQLite store."""

    def __init__(self, store: SessionStore, address: SessionAddress) -> None:
        self._store = store
        self.address = address
        self._pending_notes: deque[ChatMessage] = deque()
        self._defer_notes = False
        self._deferred_note_messages: list[ChatMessage] = []
        self._activated_skill_contents: dict[str, str] = {}
        self._activated_skill_cache_loaded = False
        self._state_lock = threading.RLock()

    @property
    def id(self) -> str:
        return self.address.session_id

    def append(self, message: ChatMessage) -> None:
        self.append_many([message])

    def append_many(self, messages: list[ChatMessage]) -> None:
        self._store.append_messages(self.address, messages)
        if any(message.role == "compaction_checkpoint" for message in messages):
            with self._state_lock:
                self._activated_skill_contents = current_skill_activation_contents(self.load())
                self._activated_skill_cache_loaded = True

    async def append_async(self, message: ChatMessage) -> None:
        await _run_session_io(self.append, message)

    async def append_many_async(self, messages: list[ChatMessage]) -> None:
        if messages:
            await _run_session_io(self.append_many, list(messages))

    def append_continuation_record(self, record: JsonObject) -> None:
        self.append_continuation_records([record])

    def append_continuation_records(self, records: list[JsonObject]) -> None:
        self._store.append_continuation(self.address, records)

    async def append_continuation_records_async(self, records: list[JsonObject]) -> None:
        if records:
            await _run_session_io(self.append_continuation_records, list(records))

    def load_continuation_records(self) -> list[JsonObject]:
        return self._store.continuation(self.address)

    async def load_continuation_records_async(self) -> list[JsonObject]:
        return await _run_session_io(self.load_continuation_records)

    def clear_continuation(self) -> None:
        self._store.clear_continuation(self.address)

    async def clear_continuation_async(self) -> None:
        await _run_session_io(self.clear_continuation)

    def begin_defer_notes(self) -> None:
        with self._state_lock:
            self._defer_notes = True

    def _take_deferred_notes(self) -> list[ChatMessage]:
        with self._state_lock:
            notes = list(self._deferred_note_messages)
            self._deferred_note_messages.clear()
            self._defer_notes = False
            return notes

    def take_deferred_notes(self) -> list[ChatMessage]:
        return self._take_deferred_notes()

    def flush_deferred_notes(self) -> None:
        self.append_many(self._take_deferred_notes())

    async def flush_deferred_notes_async(self) -> None:
        await self.append_many_async(self._take_deferred_notes())

    def add_note(self, content: str) -> None:
        from core.chat.messages import ChatMessage

        note = ChatMessage.note(content)
        with self._state_lock:
            deferred = self._defer_notes
            if deferred:
                self._deferred_note_messages.append(note)
            self._pending_notes.append(note)
        if not deferred:
            self.append(note)

    async def add_note_async(self, content: str) -> None:
        await _run_session_io(self.add_note, content)

    def drain_pending_notes(self) -> list[ChatMessage]:
        with self._state_lock:
            notes = list(self._pending_notes)
            self._pending_notes.clear()
            return notes

    def _load_activated_skill_contents(self) -> dict[str, str]:
        with self._state_lock:
            if not self._activated_skill_cache_loaded:
                self._activated_skill_contents = current_skill_activation_contents(self.load())
                self._activated_skill_cache_loaded = True
            return dict(self._activated_skill_contents)

    def register_skill_activation(self, name: str, content: str) -> bool:
        active = self._load_activated_skill_contents()
        with self._state_lock:
            if active.get(name) == content:
                return False
            self._activated_skill_contents[name] = content
            self._activated_skill_cache_loaded = True
            return True

    def activate_skill_context(self, name: str, data: JsonObject) -> bool:
        from core.chat.messages import ChatMessage

        content = data.get("activation_content")
        if not isinstance(content, str) or not content:
            raise ChatSessionError("skill activation context must be a non-empty string")
        if not self.register_skill_activation(name, content):
            return False
        self.append(ChatMessage.note(_skill_context_note_content(name, content)))
        return True

    def activated_skill_contents(self, messages: list[ChatMessage] | None = None) -> dict[str, str]:
        if messages is None:
            return self._load_activated_skill_contents()
        current = current_skill_activation_contents(messages)
        with self._state_lock:
            self._activated_skill_contents = dict(current)
            self._activated_skill_cache_loaded = True
        return dict(current)

    def bookend_timestamps(self) -> tuple[str, str] | None:
        return self._store.bookend_timestamps(self.address)

    def load(self) -> list[ChatMessage]:
        return self._store.messages(self.address)

    async def load_async(self) -> list[ChatMessage]:
        return await _run_session_io(self.load)

    def load_active(self) -> list[ChatMessage]:
        return self._store.active_messages(self.address)

    async def load_active_async(self) -> list[ChatMessage]:
        return await _run_session_io(self.load_active)

    def active_user_message_count(self, *, limit: int = 2) -> int:
        return self._store.active_user_message_count(self.address, limit=limit)

    def latest_note(self, content_prefix: str) -> ChatMessage | None:
        return self._store.latest_note(self.address, content_prefix=content_prefix)

    async def latest_note_async(self, content_prefix: str) -> ChatMessage | None:
        return await _run_session_io(self.latest_note, content_prefix)

    def read_chat_history_snapshot(
        self,
        *,
        limit: int | None,
        before: str | None = None,
        excluded_roles: Sequence[str] = (),
        complete_run_segment: bool = False,
        background_roles: Sequence[str] = (),
        background_tool_names: Sequence[str] = (),
    ) -> SessionChatHistorySnapshot:
        decoded_cursor = None if before is None else _decode_chat_history_cursor(before)
        before_message_id = before if decoded_cursor is None else None
        expected_generation_id = None if decoded_cursor is None else decoded_cursor[0]
        before_sequence = None if decoded_cursor is None else decoded_cursor[1]
        (
            messages,
            has_more,
            editable_ids,
            usage,
            context_messages,
            background_messages,
            generation_id,
            page_floor,
        ) = self._store.chat_history_snapshot(
            self.address,
            limit=limit,
            before_message_id=before_message_id,
            before_sequence=before_sequence,
            expected_generation_id=expected_generation_id,
            excluded_roles=excluded_roles,
            complete_run_segment=complete_run_segment,
            background_roles=background_roles,
            background_tool_names=background_tool_names,
        )
        return SessionChatHistorySnapshot(
            page=SessionMessagePage(
                tuple(messages),
                has_more,
                editable_ids,
                (
                    _encode_chat_history_cursor(generation_id, page_floor)
                    if has_more and page_floor is not None
                    else None
                ),
            ),
            session_usage=usage,
            context_messages=tuple(context_messages),
            background_messages=tuple(background_messages),
        )

    def status_snapshot(self) -> SessionStatusSnapshot:
        first_message_at, user_count, latest_usage, usage, cache_input_tokens = (
            self._store.status_snapshot(self.address)
        )
        return SessionStatusSnapshot(
            first_message_at=first_message_at,
            user_message_count=user_count,
            latest_assistant_usage=latest_usage,
            session_usage=usage,
            cache_input_tokens=cache_input_tokens,
        )

    async def status_snapshot_async(self) -> SessionStatusSnapshot:
        return await _run_session_io(self.status_snapshot)

    def resolve_history_snapshot(
        self,
        *,
        snapshot_sequence: int | None = None,
    ) -> SessionHistorySnapshot | None:
        resolved = self._store.history_snapshot(
            self.address,
            snapshot_sequence=snapshot_sequence,
        )
        if resolved is None:
            return None
        generation_id, checkpoints = resolved
        return SessionHistorySnapshot(
            generation_id=generation_id,
            checkpoints=tuple(
                SessionHistoryCheckpoint(
                    ordinal=ordinal,
                    sequence=sequence,
                    message_id=message_id,
                    timestamp=timestamp,
                    summary=summary,
                )
                for ordinal, (sequence, message_id, timestamp, summary) in enumerate(
                    checkpoints, start=1
                )
            ),
        )

    def load_history_records(
        self,
        snapshot: SessionHistorySnapshot,
        *,
        lower_sequence: int,
        upper_sequence: int,
        roles: Sequence[str],
        direction: str,
        cursor_sequence: int | None,
        limit: int,
        excluded_tool_name: str,
    ) -> tuple[SessionHistoryRecord, ...] | None:
        records = self._store.history_records(
            self.address,
            expected_generation_id=snapshot.generation_id,
            snapshot_sequence=snapshot.latest.sequence,
            lower_sequence=lower_sequence,
            upper_sequence=upper_sequence,
            roles=roles,
            direction=direction,
            cursor_sequence=cursor_sequence,
            limit=limit,
            excluded_tool_name=excluded_tool_name,
        )
        if records is None:
            return None
        return tuple(
            SessionHistoryRecord(sequence=sequence, message=message)
            for sequence, message in records
        )

    def history_section_stats(
        self,
        snapshot: SessionHistorySnapshot,
        *,
        sections: Sequence[tuple[int, int]],
        excluded_tool_name: str,
    ) -> dict[int, SessionHistorySectionStats] | None:
        stats = self._store.history_section_stats(
            self.address,
            expected_generation_id=snapshot.generation_id,
            snapshot_sequence=snapshot.latest.sequence,
            sections=sections,
            excluded_tool_name=excluded_tool_name,
        )
        if stats is None:
            return None
        return {
            sequence: SessionHistorySectionStats(
                eligible_count=count,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
            for sequence, (count, start_timestamp, end_timestamp) in stats.items()
        }

    def load_history_around(
        self,
        snapshot: SessionHistorySnapshot,
        *,
        lower_sequence: int,
        upper_sequence: int,
        roles: Sequence[str],
        message_id: str,
        before: int,
        after: int,
        excluded_tool_name: str,
    ) -> tuple[bool, tuple[SessionHistoryRecord, ...]] | None:
        result = self._store.history_around(
            self.address,
            expected_generation_id=snapshot.generation_id,
            snapshot_sequence=snapshot.latest.sequence,
            lower_sequence=lower_sequence,
            upper_sequence=upper_sequence,
            roles=roles,
            message_id=message_id,
            before=before,
            after=after,
            excluded_tool_name=excluded_tool_name,
        )
        if result is None:
            return None
        exists, records = result
        return exists, tuple(
            SessionHistoryRecord(sequence=sequence, message=message)
            for sequence, message in records
        )

    def reflection_runs(self) -> list[JsonObject]:
        return self._store.reflection_runs(self.address)

    def find_run_summary(
        self,
        *,
        run_id: str | None = None,
        work_id: str | None = None,
    ) -> ChatMessage | None:
        return self._store.run_summary(self.address, run_id=run_id, work_id=work_id)

    def load_run_result(
        self,
        *,
        run_id: str | None = None,
        work_id: str | None = None,
        require_latest: bool = False,
    ) -> SessionRunResult | None:
        result = self._store.run_result(
            self.address,
            run_id=run_id,
            work_id=work_id,
            require_latest=require_latest,
        )
        if result is None:
            return None
        assistant, summary, latest_tool_name = result
        return SessionRunResult(assistant, summary, latest_tool_name)

    def load_since(self, cursor: SessionReadCursor | None = None) -> SessionReadBatch | None:
        result = self._store.messages_since(self.address, cursor)
        return None if result is None else SessionReadBatch(tuple(result[0]), result[1])

    async def load_since_async(
        self, cursor: SessionReadCursor | None = None
    ) -> SessionReadBatch | None:
        return await _run_session_io(self.load_since, cursor)

    def delete(self) -> None:
        self._store.delete(self.address)
