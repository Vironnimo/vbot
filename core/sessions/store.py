"""The Session store: typed reads and transactional writes on the Session database."""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from core.database import Database, DatabaseError, open_database, open_offline_database
from core.sessions import (
    _store_continuation,
    _store_fts,
    _store_history,
    _store_mutations,
    _store_operations,
    _store_owned,
    _store_prompts,
    _store_queries,
    _store_runs,
    _store_search,
    _store_timeline,
    _store_usage,
    _store_values,
)
from core.sessions._store_schema import session_database_spec
from core.sessions._types import (
    JsonObject,
    OwnedRunRecord,
    SessionChatHistorySnapshot,
    SessionDescriptorSource,
    SessionEditResult,
    SessionHistoryRevision,
    SessionSearchOrder,
    SessionSearchResult,
)
from core.sessions.errors import FtsHealth, SessionNotFoundError

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import (
        DeliveryReceipt,
        OwnedSessionSummary,
        PromptEpoch,
        SeenSkillsUpdate,
        SessionAddress,
        SessionContinuationState,
        SessionIdentityReferenceUpdate,
        SessionListCursor,
        SessionListFilters,
        SessionListPage,
        SessionReadBatch,
        SessionReadCursor,
        SessionRunAdmission,
        SessionRunCompletion,
        TemporarySessionBinding,
        ToolResultFacts,
    )


WRITE_PATIENCE_S = 20.0
# Transcript appends are the user's conversation: wait longer before failing.
TRANSCRIPT_WRITE_PATIENCE_S = 60.0
# Marking the latest completion read is advisory: give up quickly under
# contention.
ACTIVITY_WRITE_PATIENCE_S = 0.5

_WriteResult = TypeVar("_WriteResult")
_Decoded = TypeVar("_Decoded")
_FTS_ERROR_MARKERS = ("fts",)


class SessionStore:
    """The canonical Session database, served by the shared database kernel.

    Opening goes through the kernel's canonical profile: marker authorization,
    identity and format checks, additive reconcile, automatic restore from a
    data snapshot, then the search index readiness hook. Reads use pooled read
    transactions; writes run as whole ``BEGIN IMMEDIATE`` transactions, each
    one complete domain step.
    """

    def __init__(self, path: Path, *, _offline: bool = False) -> None:
        self.path = Path(path)
        spec = session_database_spec(self.path)
        self._database = open_offline_database(spec) if _offline else open_database(spec)

    @property
    def database(self) -> Database:
        """The kernel handle, for data snapshots, status and blocking-work offload."""
        return self._database

    @property
    def _writer(self) -> sqlite3.Connection:
        return self._database.writer

    async def run_async(
        self, function: Callable[..., _Decoded], *arguments: Any, **keyword_arguments: Any
    ) -> _Decoded:
        """Run blocking Session work on the database's bounded worker pool."""
        return await self._database.run_async(function, *arguments, **keyword_arguments)

    def _read(self, select: Callable[[sqlite3.Connection], _Decoded]) -> _Decoded:
        with self._database.read() as connection:
            return select(connection)

    def _read_decoded(
        self, select: Callable[[sqlite3.Connection], Callable[[], _Decoded]]
    ) -> _Decoded:
        """Select rows in one read transaction, then decode them after it ends.

        A rollback-journal store serves reads under its runtime lock, so Message
        reconstruction outside the transaction never delays a writer.
        """
        with self._database.read() as connection:
            decode = select(connection)
        return decode()

    def _execute_write(
        self,
        func: Callable[[sqlite3.Connection], _WriteResult],
        patience_s: float = WRITE_PATIENCE_S,
    ) -> _WriteResult:
        fts_retried = False
        while True:
            try:
                return self._database.write(func, patience_s=patience_s)
            except (sqlite3.Error, DatabaseError) as exc:
                cause = exc if isinstance(exc, sqlite3.Error) else exc.__cause__
                message = str(cause).lower() if isinstance(cause, sqlite3.Error) else ""
                if not fts_retried and any(marker in message for marker in _FTS_ERROR_MARKERS):
                    # The derived search index failed, not canonical storage:
                    # detach it and retry the write without it.
                    fts_retried = True
                    with suppress(Exception):
                        self._database.write(_store_fts._detach_fts, patience_s=patience_s)
                    continue
                raise

    def close(self) -> None:
        self._database.close()

    def verify_read_write(self) -> None:
        """Exercise the opened read/write path without changing canonical rows."""
        self._database.verify_read_write()

    def usage_history(self, after_entry_key: int, *, limit: int) -> tuple[JsonObject, ...]:
        return self._read_decoded(
            lambda connection: _store_usage.usage_history(connection, after_entry_key, limit=limit)
        )

    # -- Session lifecycle -------------------------------------------------------

    def create(
        self, address: SessionAddress, created_at: str | None = None, *, generate_id: bool = False
    ) -> SessionAddress:
        return self._execute_write(
            lambda connection: _store_mutations.create(
                connection, address, created_at, generate_id=generate_id
            )
        )

    def ensure_live(self, address: SessionAddress) -> None:
        """Create a missing live Session; an existing one costs only a read."""
        if self._read(lambda connection: _store_values._find_live(connection, address)) is not None:
            return
        self._execute_write(lambda connection: _store_mutations.ensure_live(connection, address))

    def exists(self, address: SessionAddress) -> bool:
        return self._read(lambda connection: _store_queries.exists(connection, address))

    def existing_addresses(self, addresses: Sequence[SessionAddress]) -> set[SessionAddress]:
        return self._read(
            lambda connection: _store_queries.existing_addresses(connection, addresses)
        )

    def require_live(self, address: SessionAddress) -> None:
        """Raise ``SessionNotFoundError`` unless *address* names a live Session."""
        self._read(lambda connection: _store_values._require_live(connection, address))

    def archive(self, address: SessionAddress) -> None:
        return self._execute_write(lambda connection: _store_mutations.archive(connection, address))

    def move(self, source: SessionAddress, target: SessionAddress) -> None:
        return self._execute_write(
            lambda connection: _store_mutations.move(connection, source, target)
        )

    def fork(
        self,
        source: SessionAddress,
        target_scope: SessionAddress,
        *,
        title: str | None = None,
        run_kind: str | None = None,
        allow_owner_managed_source: bool = False,
    ) -> SessionAddress:
        """Fork *source* into a fresh id in *target_scope*; see ``_store_mutations.fork``."""
        return self._execute_write(
            lambda connection: _store_mutations.fork(
                connection,
                source,
                target_scope,
                title=title,
                run_kind=run_kind,
                allow_owner_managed_source=allow_owner_managed_source,
            )
        )

    def restore(self, address: SessionAddress) -> None:
        return self._execute_write(lambda connection: _store_mutations.restore(connection, address))

    def delete(self, address: SessionAddress) -> None:
        return self._execute_write(lambda connection: _store_mutations.delete(connection, address))

    def retarget_identity_agent(self, old_agent_id: str, new_agent_id: str) -> None:
        return self._execute_write(
            lambda connection: _store_mutations.retarget_identity_agent(
                connection, old_agent_id, new_agent_id
            )
        )

    def retarget_identity_agent_references(
        self, old_agent_id: str, new_agent_id: str
    ) -> tuple[SessionIdentityReferenceUpdate, ...]:
        return self._execute_write(
            lambda connection: _store_mutations.retarget_identity_agent_references(
                connection, old_agent_id, new_agent_id
            )
        )

    def restore_identity_agent_references(
        self, updates: tuple[SessionIdentityReferenceUpdate, ...]
    ) -> None:
        self._execute_write(
            lambda connection: _store_mutations.restore_identity_agent_references(
                connection, updates
            )
        )

    def archive_identity_agent_sessions(self, agent_id: str) -> None:
        return self._execute_write(
            lambda connection: _store_mutations.archive_identity_agent_sessions(
                connection, agent_id
            )
        )

    def archive_project_sessions(self, project_id: str) -> None:
        return self._execute_write(
            lambda connection: _store_mutations.archive_project_sessions(connection, project_id)
        )

    # -- Metadata facade ---------------------------------------------------------

    def metadata(self, address: SessionAddress) -> JsonObject:
        return self._read(
            lambda connection: _store_values._session_metadata_from_state(
                _store_values._live_metadata_row(connection, address)
            )
        )

    def metadata_value(self, address: SessionAddress, key: str) -> Any:
        """Return one metadata value, or ``None`` when the Session has none."""
        return self._read(
            lambda connection: _store_queries.metadata_value(connection, address, key)
        )

    def descriptor_sources(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, SessionDescriptorSource]:
        sources = self._read_decoded(
            lambda connection: _store_queries.descriptor_sources(connection, addresses)
        )
        return {
            address: SessionDescriptorSource(metadata, visibility)
            for address, (metadata, visibility) in sources.items()
        }

    def replace_metadata(self, address: SessionAddress, metadata: JsonObject) -> None:
        return self._execute_write(
            lambda connection: _store_mutations.replace_metadata(connection, address, metadata)
        )

    def mutate_metadata(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        return self._execute_write(
            lambda connection: _store_mutations.mutate_metadata(connection, address, mutation)
        )

    def ensure_metadata(
        self,
        address: SessionAddress,
        mutation: Callable[[JsonObject], None],
        *,
        create_missing: bool,
    ) -> tuple[JsonObject, JsonObject]:
        """Try the mutation on a read snapshot; enter the writer only for a real change."""
        state = self._read(
            lambda connection: _store_values._find_live_metadata_row(connection, address)
        )
        if state is not None:
            previous, updated, storage = _store_mutations.metadata_change(state, mutation)
            if storage is None:
                return previous, updated
        elif not create_missing:
            raise SessionNotFoundError(f"session does not exist: {address.session_id}")
        return self._execute_write(
            lambda connection: _store_mutations.ensure_metadata(
                connection, address, mutation, create_missing=create_missing
            )
        )

    # -- Prompt state ------------------------------------------------------------

    def prompt_pin(self, address: SessionAddress, slot: str) -> JsonObject | None:
        return self._read(lambda connection: _store_prompts.prompt_pin(connection, address, slot))

    def ensure_prompt_pin(
        self,
        address: SessionAddress,
        slot: str,
        value: JsonObject,
        accept: Callable[[JsonObject], bool],
    ) -> JsonObject:
        """Return the pin in *slot* when *accept* keeps it, else pin and return *value*.

        An accepted pin costs only a read; the write re-checks in its transaction.
        """
        current = self.prompt_pin(address, slot)
        if current is not None and accept(current):
            return current
        return self._execute_write(
            lambda connection: _store_prompts.ensure_prompt_pin(
                connection, address, slot, value, accept
            )
        )

    def seen_skills(self, address: SessionAddress) -> frozenset[str] | None:
        return self._read(lambda connection: _store_prompts.seen_skills(connection, address))

    def record_seen_skills(self, address: SessionAddress, update: SeenSkillsUpdate) -> None:
        def _fn(connection: sqlite3.Connection) -> None:
            state = _store_values._require_live(connection, address)
            _store_prompts.record_seen_skills(connection, int(state["session_key"]), update)

        self._execute_write(_fn)

    def prompt_cache_affinity_id(self, address: SessionAddress) -> str:
        return self._read(
            lambda connection: _store_prompts.prompt_cache_affinity_id(connection, address)
        )

    # -- Runs ---------------------------------------------------------------------

    def admit_run(self, address: SessionAddress, admission: SessionRunAdmission) -> None:
        """Admit one Run with its Run kind and execution owner in one transaction."""
        self._execute_write(
            lambda connection: _store_runs.admit_run(connection, address, admission)
        )

    def finish_run(self, address: SessionAddress, completion: SessionRunCompletion) -> JsonObject:
        return self._execute_write(
            lambda connection: _store_runs.finish_run(connection, address, completion),
            patience_s=TRANSCRIPT_WRITE_PATIENCE_S,
        )

    def recover_interrupted_runs(self) -> None:
        self._execute_write(_store_runs.recover_interrupted_runs)

    def mark_terminal_run_read(
        self, address: SessionAddress, run_id: str
    ) -> tuple[JsonObject, bool]:
        """Mark the latest completion read; return the activity and whether it changed."""
        return self._execute_write(
            lambda connection: _store_runs.mark_terminal_run_read(connection, address, run_id),
            patience_s=ACTIVITY_WRITE_PATIENCE_S,
        )

    # -- History writes -------------------------------------------------------------

    def append_messages(
        self,
        address: SessionAddress,
        messages: Sequence[ChatMessage],
        *,
        run_id: str | None = None,
        assistant_message_id: str | None = None,
        tool_results: Mapping[str, ToolResultFacts] | None = None,
        seen_skills: SeenSkillsUpdate | None = None,
        continuation_records: Sequence[JsonObject] = (),
        since: SessionReadCursor | None = None,
    ) -> SessionReadBatch | None:
        """Append Messages plus any Continuation records in one transaction.

        *tool_results* reports each appended Tool Result's outcome by Tool call
        id. *seen_skills* records the Skills a persisted announcement named in
        the same transaction. With *since*, the transaction also selects every
        entry after that cursor (this append and any concurrent writer's), so
        the caller needs no follow-up read; ``None`` then means the cursor
        cannot be continued.
        """

        def _fn(connection: sqlite3.Connection) -> _store_history.HistoryDelta | None:
            _store_mutations.append_messages(
                connection,
                address,
                messages,
                run_id=run_id,
                assistant_message_id=assistant_message_id,
                tool_results=tool_results,
            )
            if seen_skills is not None:
                state = _store_values._require_live(connection, address)
                _store_prompts.record_seen_skills(
                    connection, int(state["session_key"]), seen_skills
                )
            return self._journal_and_select(connection, address, continuation_records, since)

        delta = self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)
        return None if delta is None else _store_history.read_batch(delta)

    @staticmethod
    def _journal_and_select(
        connection: sqlite3.Connection,
        address: SessionAddress,
        continuation_records: Sequence[JsonObject],
        since: SessionReadCursor | None,
    ) -> _store_history.HistoryDelta | None:
        _store_continuation.append_continuation(connection, address, continuation_records)
        if since is None:
            return None
        return _store_history.message_rows_since(connection, address, since)

    def commit_compaction(
        self,
        address: SessionAddress,
        checkpoint: ChatMessage,
        *,
        since: SessionReadCursor,
        epoch: PromptEpoch,
        run_id: str | None,
    ) -> tuple[SessionReadBatch, str] | None:
        """Commit a Compaction checkpoint and its prompt epoch while *since* is current.

        Returns the entries after *since* with the new prompt-cache affinity
        id, or ``None`` (nothing written) when another writer advanced first.
        """
        committed = self._execute_write(
            lambda connection: _store_operations.commit_compaction(
                connection, address, checkpoint, since=since, epoch=epoch, run_id=run_id
            ),
            patience_s=TRANSCRIPT_WRITE_PATIENCE_S,
        )
        if committed is None:
            return None
        delta, affinity_id = committed
        return _store_history.read_batch(delta), affinity_id

    def apply_edit(
        self,
        address: SessionAddress,
        *,
        target_message_id: str,
        messages: Sequence[ChatMessage],
        run_id: str | None,
        seen_skills: SeenSkillsUpdate | None = None,
        continuation_records: Sequence[JsonObject] = (),
    ) -> SessionEditResult:
        """Replace history from one User message on; see ``_store_operations.apply_edit``."""
        outcome = self._execute_write(
            lambda connection: _store_operations.apply_edit(
                connection,
                address,
                target_message_id=target_message_id,
                messages=messages,
                run_id=run_id,
                seen_skills=seen_skills,
                continuation_records=continuation_records,
            ),
            patience_s=TRANSCRIPT_WRITE_PATIENCE_S,
        )
        delta, affinity_id = outcome
        return SessionEditResult(_store_history.read_batch(delta), affinity_id)

    def continuation(self, address: SessionAddress) -> SessionContinuationState | None:
        return self._read(lambda connection: _store_continuation.continuation(connection, address))

    def append_continuation(self, address: SessionAddress, records: Sequence[JsonObject]) -> None:
        return self._execute_write(
            lambda connection: _store_continuation.append_continuation(connection, address, records)
        )

    def clear_continuation(self, address: SessionAddress) -> None:
        return self._execute_write(
            lambda connection: _store_continuation.clear_continuation(connection, address)
        )

    # -- Extension-owned Sessions -----------------------------------------------------

    def create_bound_temporary_session(
        self,
        address: SessionAddress,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str,
        config: JsonObject,
    ) -> str:
        return self._execute_write(
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
        return self._read_decoded(
            lambda connection: _store_owned.temporary_binding(connection, address)
        )

    def temporary_binding_by_participant(
        self, *, owner_name: str, group_id: str, participant_id: str
    ) -> TemporarySessionBinding | None:
        return self._read_decoded(
            lambda connection: _store_owned.temporary_binding_by_participant(
                connection, owner_name=owner_name, group_id=group_id, participant_id=participant_id
            )
        )

    def delete_temporary_group(self, *, owner_name: str, group_id: str) -> int:
        return self._execute_write(
            lambda connection: _store_owned.delete_temporary_group(
                connection, owner_name=owner_name, group_id=group_id
            )
        )

    def temporary_bindings(
        self,
        *,
        owner_name: str,
        group_id: str,
        after: str = "",
        limit: int = 100,
    ) -> list[TemporarySessionBinding]:
        return self._read_decoded(
            lambda connection: _store_owned.temporary_bindings(
                connection, owner_name=owner_name, group_id=group_id, after=after, limit=limit
            )
        )

    def set_temporary_group_title(self, *, owner_name: str, group_id: str, title: str) -> None:
        self._execute_write(
            lambda connection: _store_owned.set_temporary_group_title(
                connection, owner_name=owner_name, group_id=group_id, title=title
            )
        )

    def temporary_group_titles(
        self, *, owner_name: str, group_ids: Sequence[str]
    ) -> dict[str, str]:
        return self._read(
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
        return self._read_decoded(
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
        continuation_records: Sequence[JsonObject] = (),
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
            return self._journal_and_select(connection, address, continuation_records, since)

        delta = self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)
        return None if delta is None else _store_history.read_batch(delta)

    def delivery_receipt(
        self, address: SessionAddress, *, generation_id: str, owner_name: str, receipt_id: str
    ) -> DeliveryReceipt | None:
        return self._read(
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
        return self._read(
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
        return self._read(
            lambda connection: _store_owned.owned_runs_by_id(
                connection, owner_name=owner_name, group_id=group_id, run_ids=run_ids
            )
        )

    def owned_run_by_input(self, address: SessionAddress, input_id: str) -> OwnedRunRecord | None:
        return self._read(
            lambda connection: _store_owned.owned_run_by_input(connection, address, input_id)
        )

    # -- History reads ---------------------------------------------------------------

    def messages(self, address: SessionAddress) -> list[ChatMessage]:
        """The Session's own audit: every entry it wrote, superseded ones included."""
        return self._read_decoded(lambda connection: _store_history.messages(connection, address))

    def active_messages(self, address: SessionAddress) -> list[ChatMessage]:
        """The Session's current view, inherited history included."""
        return self._read_decoded(
            lambda connection: _store_history.active_messages(connection, address)
        )

    def active_user_message_count(self, address: SessionAddress, *, limit: int) -> int:
        return self._read(
            lambda connection: _store_history.active_user_message_count(
                connection, address, limit=limit
            )
        )

    def tool_result_persisted(self, address: SessionAddress, tool_call_id: str) -> bool:
        return self._read(
            lambda connection: _store_history.tool_result_persisted(
                connection, address, tool_call_id
            )
        )

    def tool_result_payload(
        self, address: SessionAddress, payload_id: str, *, owner_name: str
    ) -> str | None:
        return self._read(
            lambda connection: _store_history.tool_result_payload(
                connection, address, payload_id, owner_name
            )
        )

    def latest_note(self, address: SessionAddress, *, content_prefix: str) -> ChatMessage | None:
        return self._read_decoded(
            lambda connection: _store_history.latest_note(
                connection, address, content_prefix=content_prefix
            )
        )

    def current_skill_activation_messages(self, address: SessionAddress) -> list[ChatMessage]:
        return self._read_decoded(
            lambda connection: _store_history.current_skill_activation_messages(connection, address)
        )

    def messages_since(
        self, address: SessionAddress, cursor: SessionReadCursor | None
    ) -> SessionReadBatch | None:
        delta = self._read(
            lambda connection: _store_history.message_rows_since(connection, address, cursor)
        )
        return None if delta is None else _store_history.read_batch(delta)

    def chat_history_snapshot(
        self,
        address: SessionAddress,
        *,
        limit: int | None,
        before_message_id: str | None,
        before_sequence: int | None,
        expected_generation_id: str | None,
        excluded_roles: Sequence[str],
        complete_run_segment: bool,
        background_tool_names: Sequence[str],
        background_note_marker: str | None,
        after: tuple[str, int] | None = None,
        skip_unchanged: bool = False,
    ) -> SessionChatHistorySnapshot:
        return self._read_decoded(
            lambda connection: _store_timeline.chat_history_snapshot(
                connection,
                address,
                limit=limit,
                before_message_id=before_message_id,
                before_sequence=before_sequence,
                expected_generation_id=expected_generation_id,
                excluded_roles=excluded_roles,
                complete_run_segment=complete_run_segment,
                background_tool_names=background_tool_names,
                background_note_marker=background_note_marker,
                after=after,
                skip_unchanged=skip_unchanged,
            )
        )

    def status_snapshot(
        self,
        address: SessionAddress,
    ) -> tuple[str | None, int, JsonObject | None, JsonObject, int]:
        return self._read_decoded(
            lambda connection: _store_history.status_snapshot(connection, address)
        )

    def history_snapshot(
        self,
        address: SessionAddress,
        *,
        snapshot_sequence: int | None = None,
    ) -> tuple[str, list[tuple[int, str, str, str]]] | None:
        return self._read(
            lambda connection: _store_history.history_snapshot(
                connection, address, snapshot_sequence=snapshot_sequence
            )
        )

    def history_records(
        self,
        address: SessionAddress,
        *,
        expected_generation_id: str,
        snapshot_sequence: int,
        lower_sequence: int,
        upper_sequence: int,
        roles: Sequence[str],
        direction: str,
        cursor_sequence: int | None,
        limit: int,
        excluded_tool_name: str,
    ) -> list[tuple[int, ChatMessage]] | None:
        return self._read_decoded(
            lambda connection: _store_history.history_records(
                connection,
                address,
                expected_generation_id=expected_generation_id,
                snapshot_sequence=snapshot_sequence,
                lower_sequence=lower_sequence,
                upper_sequence=upper_sequence,
                roles=roles,
                direction=direction,
                cursor_sequence=cursor_sequence,
                limit=limit,
                excluded_tool_name=excluded_tool_name,
            )
        )

    def history_section_stats(
        self,
        address: SessionAddress,
        *,
        expected_generation_id: str,
        snapshot_sequence: int,
        sections: Sequence[tuple[int, int]],
        excluded_tool_name: str,
    ) -> dict[int, tuple[int, str | None, str | None]] | None:
        return self._read(
            lambda connection: _store_history.history_section_stats(
                connection,
                address,
                expected_generation_id=expected_generation_id,
                snapshot_sequence=snapshot_sequence,
                sections=sections,
                excluded_tool_name=excluded_tool_name,
            )
        )

    def history_around(
        self,
        address: SessionAddress,
        *,
        expected_generation_id: str,
        snapshot_sequence: int,
        lower_sequence: int,
        upper_sequence: int,
        roles: Sequence[str],
        message_id: str,
        before: int,
        after: int,
        excluded_tool_name: str,
    ) -> tuple[bool, list[tuple[int, ChatMessage]]] | None:
        return self._read_decoded(
            lambda connection: _store_history.history_around(
                connection,
                address,
                expected_generation_id=expected_generation_id,
                snapshot_sequence=snapshot_sequence,
                lower_sequence=lower_sequence,
                upper_sequence=upper_sequence,
                roles=roles,
                message_id=message_id,
                before=before,
                after=after,
                excluded_tool_name=excluded_tool_name,
            )
        )

    def reflection_runs(self, address: SessionAddress) -> list[JsonObject]:
        return self._read(lambda connection: _store_history.reflection_runs(connection, address))

    def run_messages(self, address: SessionAddress, run_id: str) -> list[ChatMessage]:
        return self._read_decoded(
            lambda connection: _store_history.run_messages(connection, address, run_id)
        )

    def run_summary(
        self,
        address: SessionAddress,
        *,
        run_id: str | None = None,
        work_id: str | None = None,
    ) -> ChatMessage | None:
        return self._read_decoded(
            lambda connection: _store_history.run_summary(
                connection, address, run_id=run_id, work_id=work_id
            )
        )

    def run_result(
        self,
        address: SessionAddress,
        *,
        run_id: str | None = None,
        work_id: str | None = None,
        require_latest: bool = False,
    ) -> tuple[ChatMessage | None, ChatMessage, str | None] | None:
        return self._read_decoded(
            lambda connection: _store_history.run_result(
                connection, address, run_id=run_id, work_id=work_id, require_latest=require_latest
            )
        )

    def recall_context(self, address: SessionAddress, message_id: str) -> builtins.list[JsonObject]:
        """Return bounded conversation text beside a search anchor."""
        return self._read(
            lambda connection: _store_history.recall_context(connection, address, message_id)
        )

    # -- Session lists ------------------------------------------------------------------

    def list_addresses(
        self,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        include_all_scopes: bool = False,
        exclude_owner_managed: bool = False,
    ) -> list[SessionAddress]:
        return self._read(
            lambda connection: _store_queries.list_addresses(
                connection,
                project_id=project_id,
                agent_id=agent_id,
                include_all_scopes=include_all_scopes,
                exclude_owner_managed=exclude_owner_managed,
            )
        )

    def list_agent_ids(
        self, project_id: str | None, *, exclude_owner_managed: bool = False
    ) -> list[str]:
        return self._read(
            lambda connection: _store_queries.list_agent_ids(
                connection, project_id, exclude_owner_managed=exclude_owner_managed
            )
        )

    def list_summaries(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        metadata_keys: Sequence[str] = (),
    ) -> list[JsonObject]:
        return self._read_decoded(
            lambda connection: _store_queries.list_summaries(
                connection, project_id, agent_id, metadata_keys=metadata_keys
            )
        )

    def list_summaries_page(
        self,
        scopes: Sequence[tuple[str | None, str]],
        *,
        limit: int,
        cursor: SessionListCursor | None,
        filters: SessionListFilters,
        required_address: SessionAddress | None,
    ) -> SessionListPage:
        return self._read_decoded(
            lambda connection: _store_queries.list_summaries_page(
                connection,
                scopes,
                limit=limit,
                cursor=cursor,
                filters=filters,
                required_address=required_address,
            )
        )

    def summary(self, address: SessionAddress) -> JsonObject | None:
        return self._read_decoded(lambda connection: _store_queries.summary(connection, address))

    def list_completion_activity(
        self, scopes: Sequence[tuple[str | None, str]]
    ) -> dict[tuple[str | None, str], list[JsonObject]]:
        return self._read(
            lambda connection: _store_queries.list_completion_activity(connection, scopes)
        )

    def list_history_revisions(
        self, project_id: str | None, agent_id: str
    ) -> list[SessionHistoryRevision]:
        return self._read(
            lambda connection: _store_queries.list_history_revisions(
                connection, project_id, agent_id
            )
        )

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]:
        return self._read(
            lambda connection: _store_queries.list_history_versions(connection, addresses)
        )

    # -- Search -------------------------------------------------------------------------

    def fts_health(self) -> FtsHealth:
        """Return operator-facing FTS state with explicit canonical coverage checks."""
        try:
            return self._read(
                lambda connection: _store_fts._fts_health_from_connection(
                    connection, verify_coverage=True
                )
            )
        except Exception as exc:
            return FtsHealth(state="unavailable", reason=f"FTS health check failed: {exc}")

    def is_fts_available(self) -> bool:
        """Return cheap marker-backed availability; rebuild verification owns coverage scans."""
        try:
            return self._read(
                lambda connection: (
                    _store_fts._fts_health_from_connection(
                        connection, verify_coverage=False
                    ).available
                )
            )
        except Exception:
            return False

    def search_messages(
        self,
        query: str,
        *,
        project_id: str | None,
        agent_id: str | None,
        session_id: str | None = None,
        match_mode: str = "all_terms",
        order: SessionSearchOrder = "relevance",
        limit: int = _store_values._SEARCH_RESULT_LIMIT,
        roles: Sequence[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        excluded_session_ids: Sequence[str] = (),
        include_subagents: bool = False,
        use_fts: bool = True,
    ) -> SessionSearchResult:
        def select(*, use_fts: bool, fallback_reason: str | None = None) -> SessionSearchResult:
            return self._read(
                lambda connection: _store_search.search(
                    connection,
                    query,
                    project_id=project_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    match_mode=match_mode,
                    order=order,
                    limit=limit,
                    roles=roles,
                    since=since,
                    until=until,
                    excluded_session_ids=excluded_session_ids,
                    include_subagents=include_subagents,
                    use_fts=use_fts,
                    fallback_reason=fallback_reason,
                )
            )

        if not use_fts:
            return select(use_fts=False)
        try:
            return select(use_fts=True)
        except sqlite3.Error as exc:
            if any(marker in str(exc).lower() for marker in _FTS_ERROR_MARKERS):
                error_message = str(exc)
                with suppress(Exception):
                    self._execute_write(
                        lambda connection: _store_fts._detach_fts(connection, error_message)
                    )
            return select(use_fts=False, fallback_reason="fts_error")
