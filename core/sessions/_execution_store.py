"""Canonical execution storage within the Session database.

Run admission, completion, durable drafts and owner-bound deliveries share this
transactional boundary. History and metadata remain in SessionStore; the public
Session service chooses which operation to admit, and no second database or
transaction is introduced here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from core.sessions import (
    _store_commands,
    _store_extension_results,
    _store_external_notes,
    _store_history,
    _store_owned,
    _store_runs,
)
from core.sessions._archive_types import ArchiveEntryRef
from core.sessions._types import JsonObject, OwnedRunRecord

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import (
        DeliveryReceipt,
        OwnedSessionSummary,
        SessionAddress,
        SessionReadBatch,
        SessionReadCursor,
        SessionRunAdmission,
        SessionRunCompletion,
        TemporarySessionBinding,
        ToolResultFacts,
    )
    from core.sessions.store import SessionStore

# Transcripts may wait for another writer; read acknowledgements are advisory.
TRANSCRIPT_WRITE_PATIENCE_S = 60.0
ACTIVITY_WRITE_PATIENCE_S = 0.5


class SessionExecutionStore:
    """Run state and durable execution delivery, on the owning store's transactions."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def admit_run(self, address: SessionAddress, admission: SessionRunAdmission) -> None:
        """Admit one Run with its Run kind and execution owner in one transaction."""
        self._store._execute_write(
            lambda connection: _store_runs.admit_run(connection, address, admission)
        )

    def finish_run(self, address: SessionAddress, completion: SessionRunCompletion) -> JsonObject:
        return self._store._execute_write(
            lambda connection: _store_runs.finish_run(connection, address, completion),
            patience_s=TRANSCRIPT_WRITE_PATIENCE_S,
        )

    def record_run_changes(
        self, address: SessionAddress, run_id: str, change_stats: JsonObject
    ) -> None:
        """Store a running Run's change statistics so far; its completion replaces them."""
        self._store._execute_write(
            lambda connection: _store_runs.record_run_changes(
                connection, address, run_id, change_stats
            )
        )

    def append_stream_draft(
        self,
        address: SessionAddress,
        run_id: str,
        *,
        model: str,
        reasoning_delta: str,
        content_delta: str,
        tool_calls: Sequence[JsonObject] = (),
    ) -> None:
        """Append streamed output of a running Run's current Model step to its draft."""
        self._store._execute_write(
            lambda connection: _store_runs.append_stream_draft(
                connection,
                address,
                run_id,
                model=model,
                reasoning_delta=reasoning_delta,
                content_delta=content_delta,
                tool_calls=tool_calls,
            )
        )

    def discard_stream_draft(self, address: SessionAddress, run_id: str) -> None:
        """Delete a running Run's stream draft, whose Model step starts over."""
        self._store._execute_write(
            lambda connection: _store_runs.discard_stream_draft(connection, address, run_id)
        )

    def recover_interrupted_runs(self) -> None:
        self._store._execute_write(_store_runs.recover_interrupted_runs)

    def mark_terminal_run_read(
        self, address: SessionAddress, run_id: str
    ) -> tuple[JsonObject, bool]:
        """Mark the latest completion read; return the activity and whether it changed."""
        return self._store._execute_write(
            lambda connection: _store_runs.mark_terminal_run_read(connection, address, run_id),
            patience_s=ACTIVITY_WRITE_PATIENCE_S,
        )

    def create_bound_temporary_session(
        self,
        address: SessionAddress,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str,
        config: JsonObject,
    ) -> str:
        return self._store._execute_write(
            lambda connection: _store_owned.create_bound_temporary_session(
                connection,
                address,
                owner_name=owner_name,
                group_id=group_id,
                participant_id=participant_id,
                config=config,
            )
        )

    def temporary_binding(self, address: SessionAddress) -> TemporarySessionBinding | None:
        return self._store._read_decoded(
            lambda connection: _store_owned.temporary_binding(connection, address)
        )

    def temporary_binding_by_participant(
        self, *, owner_name: str, group_id: str, participant_id: str
    ) -> TemporarySessionBinding | None:
        return self._store._read_decoded(
            lambda connection: _store_owned.temporary_binding_by_participant(
                connection, owner_name=owner_name, group_id=group_id, participant_id=participant_id
            )
        )

    def delete_temporary_group(self, *, owner_name: str, group_id: str) -> int:
        return self._store._execute_write(
            lambda connection: _store_owned.delete_temporary_group(
                connection, owner_name=owner_name, group_id=group_id
            )
        )

    def archive_temporary_group(
        self, *, owner_name: str, group_id: str, reason: str
    ) -> tuple[int, ArchiveEntryRef | None]:
        return self._store._execute_write(
            lambda connection: _store_owned.archive_temporary_group(
                connection, owner_name=owner_name, group_id=group_id, reason=reason
            )
        )

    def temporary_groups(self, *, owner_name: str, after: str = "", limit: int = 100) -> list[str]:
        return self._store._read(
            lambda connection: _store_owned.temporary_groups(
                connection, owner_name=owner_name, after=after, limit=limit
            )
        )

    def temporary_owners(self) -> list[str]:
        return self._store._read(_store_owned.temporary_owners)

    def temporary_bindings(
        self,
        *,
        owner_name: str,
        group_id: str,
        after: str = "",
        limit: int = 100,
    ) -> list[TemporarySessionBinding]:
        return self._store._read_decoded(
            lambda connection: _store_owned.temporary_bindings(
                connection, owner_name=owner_name, group_id=group_id, after=after, limit=limit
            )
        )

    def set_temporary_group_title(self, *, owner_name: str, group_id: str, title: str) -> None:
        self._store._execute_write(
            lambda connection: _store_owned.set_temporary_group_title(
                connection, owner_name=owner_name, group_id=group_id, title=title
            )
        )

    def temporary_group_titles(
        self, *, owner_name: str, group_ids: Sequence[str]
    ) -> dict[str, str]:
        return self._store._read(
            lambda connection: _store_owned.temporary_group_titles(
                connection, owner_name=owner_name, group_ids=group_ids
            )
        )

    def owned_session_summaries(
        self,
        *,
        owner_name: str | None = None,
        group_id: str | None = None,
        metadata_keys: Sequence[str] = (),
    ) -> list[OwnedSessionSummary]:
        return self._store._read_decoded(
            lambda connection: _store_owned.owned_session_summaries(
                connection, owner_name=owner_name, group_id=group_id, metadata_keys=metadata_keys
            )
        )

    def append_messages_with_receipts(
        self,
        address: SessionAddress,
        *,
        generation_id: str,
        owner_name: str,
        messages: Sequence[ChatMessage],
        receipts: Sequence[tuple[int, str, str, str, str]],
        deduplicate_carrier: bool = False,
        run_id: str | None = None,
        assistant_message_id: str | None = None,
        tool_results: Mapping[str, ToolResultFacts] | None = None,
        since: SessionReadCursor | None = None,
    ) -> SessionReadBatch | None:
        """Receipt-carrying variant of :meth:`append_messages` with the same options."""

        def _fn(connection: sqlite3.Connection) -> _store_history.HistoryDelta | None:
            _store_owned.append_messages_with_receipts(
                connection,
                address,
                generation_id=generation_id,
                owner_name=owner_name,
                messages=messages,
                receipts=receipts,
                deduplicate_carrier=deduplicate_carrier,
                run_id=run_id,
                assistant_message_id=assistant_message_id,
                tool_results=tool_results,
            )
            return self._store._select_since(connection, address, since)

        delta = self._store._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)
        return None if delta is None else _store_history.read_batch(delta)

    def delivery_receipt(
        self, address: SessionAddress, *, generation_id: str, owner_name: str, receipt_id: str
    ) -> DeliveryReceipt | None:
        return self._store._read(
            lambda connection: _store_owned.delivery_receipt(
                connection,
                address,
                generation_id=generation_id,
                owner_name=owner_name,
                receipt_id=receipt_id,
            )
        )

    def owned_runs(
        self,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> list[OwnedRunRecord]:
        return self._store._read(
            lambda connection: _store_owned.owned_runs(
                connection,
                owner_name=owner_name,
                group_id=group_id,
                participant_id=participant_id,
                after=after,
                limit=limit,
            )
        )

    def owned_runs_by_id(
        self, *, owner_name: str, group_id: str, run_ids: Sequence[str]
    ) -> dict[str, OwnedRunRecord]:
        return self._store._read(
            lambda connection: _store_owned.owned_runs_by_id(
                connection, owner_name=owner_name, group_id=group_id, run_ids=run_ids
            )
        )

    def owned_run_by_input(self, address: SessionAddress, input_id: str) -> OwnedRunRecord | None:
        return self._store._read(
            lambda connection: _store_owned.owned_run_by_input(connection, address, input_id)
        )

    def append_notes_once(
        self,
        address: SessionAddress,
        delivery_id: str,
        notes: Sequence[str],
        *,
        generation_id: str,
    ) -> None:
        self._store._execute_write(
            lambda connection: _store_external_notes.append_notes_once(
                connection, address, delivery_id, notes, generation_id=generation_id
            ),
            patience_s=TRANSCRIPT_WRITE_PATIENCE_S,
        )

    def background_command_put(
        self,
        address: SessionAddress,
        command_id: str,
        origin_run_id: str,
        checkpoint: JsonObject,
    ) -> None:
        self._store._execute_write(
            lambda connection: _store_commands.put(
                connection, address, command_id, origin_run_id, checkpoint
            )
        )

    def background_command_read(self) -> list[JsonObject]:
        return self._store._read(_store_commands.read)

    def background_command_delete(self, command_id: str) -> None:
        self._store._execute_write(
            lambda connection: _store_commands.delete(connection, command_id)
        )

    def background_command_delivered(self, command_id: str, body: str) -> bool:
        return self._store._read(
            lambda connection: _store_commands.delivered(connection, command_id, body)
        )

    def capture_tool_receipt(
        self,
        address: SessionAddress,
        *,
        owner_name: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        execution_owner: JsonObject | None,
    ) -> str | None:
        return self._store._execute_write(
            lambda connection: _store_extension_results.capture_tool_receipt(
                connection,
                address,
                owner_name=owner_name,
                run_id=run_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                execution_owner=execution_owner,
            )
        )

    def resolve_tool_receipt(self, receipt_id: str, *, owner_name: str) -> JsonObject | None:
        return self._store._read(
            lambda connection: _store_extension_results.resolve_tool_receipt(
                connection, receipt_id, owner_name=owner_name
            )
        )

    def deliver_tool_outcome(self, receipt_id: str, content: str, *, owner_name: str) -> bool:
        return self._store._execute_write(
            lambda connection: _store_extension_results.deliver_tool_outcome(
                connection, receipt_id, content, owner_name=owner_name
            ),
            patience_s=TRANSCRIPT_WRITE_PATIENCE_S,
        )
