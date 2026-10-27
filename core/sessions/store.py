"""The Session store: typed reads and transactional writes on the Session database."""

from __future__ import annotations

import builtins
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.database import Database, DatabaseError, open_database
from core.database.snapshots import read_retained_snapshot_members
from core.sessions import (
    _store_accessor_inputs,
    _store_archive,
    _store_attachments,
    _store_conversation,
    _store_external_notes,
    _store_fts,
    _store_history,
    _store_mutations,
    _store_operations,
    _store_prompts,
    _store_queries,
    _store_search,
    _store_timeline,
    _store_usage,
    _store_values,
)
from core.sessions._archive_types import ArchiveEntryRef
from core.sessions._execution_store import TRANSCRIPT_WRITE_PATIENCE_S, SessionExecutionStore
from core.sessions._search_request import SessionSearchRequest
from core.sessions._store_health import SessionHealth
from core.sessions._store_schema import session_database_spec
from core.sessions._types import (
    JsonObject,
    SessionChatHistorySnapshot,
    SessionConversationPage,
    SessionDescriptorSource,
    SessionHistoryRevision,
    SessionSearchResult,
)
from core.sessions.errors import FtsHealth, SessionNotFoundError

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import (
        PromptEpoch,
        SeenSkillsUpdate,
        SessionAddress,
        SessionIdentityReferenceUpdate,
        SessionListCursor,
        SessionListFilters,
        SessionListPage,
        SessionProjectGrouping,
        SessionReadBatch,
        SessionReadCursor,
        SessionRunRecord,
        SessionWorkingProject,
        ToolResultFacts,
    )

# ruff: noqa: E501


WRITE_PATIENCE_S = 20.0

_FTS_ERROR_MARKERS = ("fts",)


def _is_fts_failure(exc: BaseException) -> bool:
    """Whether a failed statement failed in the derived search index.

    The search indexes are the store's only virtual tables, so a corrupt
    virtual table is always theirs, even though SQLite reports it as a
    malformed database image.
    """
    cause = exc if isinstance(exc, sqlite3.Error) else exc.__cause__
    if not isinstance(cause, sqlite3.Error):
        return False
    if getattr(cause, "sqlite_errorcode", None) == sqlite3.SQLITE_CORRUPT_VTAB:
        return True
    message = str(cause).lower()
    return any(marker in message for marker in _FTS_ERROR_MARKERS)


class SessionStore:
    """The canonical Session database, served by the shared database kernel.

    Opening goes through the kernel's canonical profile: marker authorization,
    identity and format checks, additive reconcile, automatic restore from a
    data snapshot, then the search index readiness hook. Reads use pooled read
    transactions; writes run as whole ``BEGIN IMMEDIATE`` transactions, each
    one complete domain step.
    """

    def __init__(self, path: Path) -> None:
        self.execution = SessionExecutionStore(self)
        self.path = Path(path)
        self._health = SessionHealth()
        self._database = open_database(session_database_spec(self.path, health=self._health))
        self._metadata_revision = 0
        self._metadata_revision_lock = threading.Lock()

    @property
    def database(self) -> Database:
        """The kernel handle, for data snapshots, status and blocking-work offload."""
        return self._database

    @property
    def _writer(self) -> sqlite3.Connection:
        return self._database.writer

    @property
    def metadata_revision(self) -> int:
        """A process-local number that changes after every metadata write.

        It advances once a write transaction that rewrote any Session's
        metadata storage ends, committed or not, so an unchanged value proves
        that no Session's metadata changed in this process meanwhile. Read it
        before the metadata it validates.
        """
        return self._metadata_revision

    async def run_async[Decoded](
        self, function: Callable[..., Decoded], *arguments: Any, **keyword_arguments: Any
    ) -> Decoded:
        """Run blocking Session work on the database's bounded worker pool."""
        return await self._database.run_async(function, *arguments, **keyword_arguments)

    def _read[Decoded](self, select: Callable[[sqlite3.Connection], Decoded]) -> Decoded:
        with self._database.read() as connection:
            return select(connection)

    def _read_decoded[Decoded](
        self, select: Callable[[sqlite3.Connection], Callable[[], Decoded]]
    ) -> Decoded:
        """Select rows in one read transaction, then decode them after it ends.

        A rollback-journal store serves reads under its runtime lock, so Message
        reconstruction outside the transaction never delays a writer.
        """
        with self._database.read() as connection:
            decode = select(connection)
        return decode()

    def _execute_write[WriteResult](
        self,
        func: Callable[[sqlite3.Connection], WriteResult],
        patience_s: float = WRITE_PATIENCE_S,
    ) -> WriteResult:
        fts_retried = False
        try:
            while True:
                try:
                    return self._database.write(func, patience_s=patience_s)
                except (sqlite3.Error, DatabaseError) as exc:
                    if fts_retried or not _is_fts_failure(exc):
                        raise
                    # The derived search index failed, not canonical storage:
                    # detach it and retry the write without it.
                    fts_retried = True
                    with suppress(Exception):
                        self._database.write(_store_fts._detach_fts, patience_s=patience_s)
        finally:
            # After the transaction ended, so a reader that sees the new
            # revision also reads the committed metadata.
            if _store_values.take_metadata_storage_written():
                with self._metadata_revision_lock:
                    self._metadata_revision += 1

    def close(self) -> None:
        self._database.close()

    def verify_read_write(self) -> None:
        """Exercise the opened read/write path without changing canonical rows."""
        self._database.verify_read_write()

    def referenced_attachment_ids(self) -> frozenset[str]:
        return self._read(_store_attachments.referenced)

    def unreferenced_attachment_ids(self, *, additional: Sequence[str] = ()) -> tuple[str, ...]:
        """Unowned known or supplied blobs at a quiescent lifecycle boundary only."""
        candidates = self._read(
            lambda connection: _store_attachments.unreferenced(connection, additional=additional)
        )
        if not candidates:
            return ()
        protected: set[str] = set()
        for references in read_retained_snapshot_members(
            self.path.parent, "sessions", _store_attachments.snapshot_references
        ):
            protected.update(references)
        return tuple(
            attachment_id for attachment_id in candidates if attachment_id not in protected
        )

    def forget_collected_attachment_ids(self, ids: Sequence[str]) -> None:
        self._execute_write(lambda connection: _store_attachments.forget(connection, ids))

    def usage_history(self, after_entry_key: int, *, limit: int) -> tuple[JsonObject, ...]:
        return self._read_decoded(
            lambda connection: _store_usage.usage_history(connection, after_entry_key, limit=limit)
        )

    # -- Session lifecycle -------------------------------------------------------

    def create(
        self,
        address: SessionAddress,
        created_at: str | None = None,
        *,
        generate_id: bool = False,
        run_kind: str | None = None,
        metadata: JsonObject | None = None,
        working_project_id: str | None = None,
    ) -> tuple[SessionAddress, str]:
        def create(connection: sqlite3.Connection) -> tuple[SessionAddress, str]:
            created = _store_mutations.create(
                connection,
                address,
                created_at,
                generate_id=generate_id,
                run_kind=run_kind,
                metadata=metadata,
                working_project_id=working_project_id,
            )
            return created, str(_store_values._require_live(connection, created)["generation_id"])

        return self._execute_write(create)

    def ensure_live(
        self, address: SessionAddress, new_working_project: Callable[[], str | None] | None = None
    ) -> None:
        """Create a missing live Session; an existing one costs only a read.

        *new_working_project* returns the working Project a created Session
        starts in; it runs only when the Session is missing.
        """
        if self._read(lambda connection: _store_values._find_live(connection, address)) is not None:
            return
        working_project_id = None if new_working_project is None else new_working_project()
        self._execute_write(
            lambda connection: _store_mutations.ensure_live(connection, address, working_project_id)
        )

    def exists(self, address: SessionAddress) -> bool:
        return self._read(lambda connection: _store_queries.exists(connection, address))

    def generation_address(self, generation_id: str) -> SessionAddress | None:
        return self._read(
            lambda connection: _store_external_notes.generation_address(connection, generation_id)
        )

    def existing_addresses(self, addresses: Sequence[SessionAddress]) -> set[SessionAddress]:
        return self._read(
            lambda connection: _store_queries.existing_addresses(connection, addresses)
        )

    def subagent_children(self, parent: SessionAddress) -> list[SessionAddress]:
        return self._read(lambda connection: _store_queries.subagent_children(connection, parent))

    def subagent_session(self, subagent_id: str) -> SessionAddress | None:
        return self._read(
            lambda connection: _store_queries.subagent_session(connection, subagent_id)
        )

    def live_addresses_with_metadata_field(
        self, key: str, field: str, value: str
    ) -> list[SessionAddress]:
        return self._read(
            lambda connection: _store_queries.live_addresses_with_metadata_field(
                connection, key, field, value
            )
        )

    def require_live(self, address: SessionAddress) -> str:
        """Return the live generation id, or raise ``SessionNotFoundError``."""
        return self._read(
            lambda connection: str(
                _store_values._require_live(connection, address)["generation_id"]
            )
        )

    def archive(
        self,
        address: SessionAddress,
        *,
        facts: Mapping[str, Any] | None = None,
        cleanup_pending: bool = False,
    ) -> ArchiveEntryRef:
        """Archive one live Session with its review forks as its own ``session`` archive entry."""
        return self._execute_write(
            lambda connection: _store_archive.archive_session(
                connection, address, facts, cleanup_pending=cleanup_pending
            )
        )

    def review_forks(
        self, address: SessionAddress, *, recursive: bool = True
    ) -> tuple[SessionAddress, ...]:
        """The live review forks that follow one live Session; see ``_store_mutations``."""
        return self._read(
            lambda connection: _store_mutations.review_fork_addresses(
                connection, address, recursive=recursive
            )
        )

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

    def delete(self, address: SessionAddress) -> None:
        return self._execute_write(lambda connection: _store_mutations.delete(connection, address))

    def discard_unused(self, address: SessionAddress, generation_id: str) -> bool:
        return self._execute_write(
            lambda connection: _store_mutations.discard_unused(connection, address, generation_id)
        )

    def retarget_identity_agent(self, old_agent_id: str, new_agent_id: str) -> int:
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

    def retarget_metadata_value(
        self,
        agent_id: str,
        key: str,
        old_value: str,
        new_value: str,
        *,
        matching: Mapping[str, str] | None = None,
    ) -> int:
        return self._execute_write(
            lambda connection: _store_mutations.retarget_metadata_value(
                connection, agent_id, key, old_value, new_value, matching=matching
            )
        )

    # -- Metadata facade ---------------------------------------------------------

    def metadata(self, address: SessionAddress) -> JsonObject:
        return self._read(
            lambda connection: _store_values._session_metadata_from_state(
                _store_values._live_metadata_row(connection, address)
            )
        )

    def working_project(self, address: SessionAddress) -> SessionWorkingProject:
        """Read the Working Project and whether its original incarnation was archived."""
        return self._read(lambda connection: _store_queries.working_project(connection, address))

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
        self,
        address: SessionAddress,
        mutation: Callable[[JsonObject], None],
        *,
        expected_generation_id: str | None = None,
        expected_first_user_sequence: int | None = None,
    ) -> tuple[JsonObject, JsonObject]:
        return self._execute_write(
            lambda connection: _store_mutations.mutate_metadata(
                connection,
                address,
                mutation,
                expected_generation_id=expected_generation_id,
                expected_first_user_sequence=expected_first_user_sequence,
            )
        )

    def ensure_metadata(
        self,
        address: SessionAddress,
        mutation: Callable[[JsonObject], None],
        *,
        create_missing: bool,
        new_working_project: Callable[[], str | None] | None = None,
    ) -> tuple[JsonObject, JsonObject]:
        """Try the mutation on a read snapshot; enter the writer only for a real change.

        *new_working_project* returns the working Project a Session this creates
        starts in; it runs only when the Session is missing.
        """
        state = self._read(
            lambda connection: _store_values._find_live_metadata_row(connection, address)
        )
        working_project_id = None
        if state is not None:
            previous, updated, storage = _store_mutations.metadata_change(state, mutation)
            if storage is None:
                return previous, updated
        elif not create_missing:
            raise SessionNotFoundError(f"session does not exist: {address.session_id}")
        elif new_working_project is not None:
            working_project_id = new_working_project()
        return self._execute_write(
            lambda connection: _store_mutations.ensure_metadata(
                connection,
                address,
                mutation,
                create_missing=create_missing,
                working_project_id=working_project_id,
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
        subagent_reply_target: str | None = None,
        since: SessionReadCursor | None = None,
        accessor_input_ids: Sequence[str] = (),
        metadata_update: JsonObject | None = None,
    ) -> SessionReadBatch | None:
        """Append Messages in one transaction.

        *tool_results* reports each appended Tool Result's outcome by Tool call
        id. *seen_skills* records the Skills a persisted announcement named in
        the same transaction. *subagent_reply_target* selects the destination of
        later answers in a linked Session, atomically with this input.
        *metadata_update* merges partial metadata in the same transaction,
        preserving other fields and applying the usual metadata validation.
        With *since*, the transaction also selects every entry after
        that cursor (this append and any concurrent writer's), so the caller
        needs no follow-up read; ``None`` then means the cursor cannot be
        continued.
        """

        def _fn(connection: sqlite3.Connection) -> _store_history.HistoryDelta | None:
            steering = None
            if accessor_input_ids:
                if not messages:
                    raise ChatSessionError("Steered Accessor inputs require transcript messages")
                steering = _store_accessor_inputs.prepare_steering(
                    connection, address, accessor_input_ids, run_id
                )
                if steering is None:
                    return self._select_since(connection, address, since)
            if subagent_reply_target is not None:
                _store_mutations.record_subagent_reply_target(
                    connection, address, subagent_reply_target
                )
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
            if steering is not None:
                _store_accessor_inputs.commit_steering(connection, steering)
            if metadata_update:
                _store_mutations.mutate_metadata(
                    connection, address, lambda metadata: metadata.update(metadata_update)
                )
            return self._select_since(connection, address, since)

        delta = self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)
        return None if delta is None else _store_history.read_batch(delta)

    @staticmethod
    def _select_since(
        connection: sqlite3.Connection,
        address: SessionAddress,
        since: SessionReadCursor | None,
    ) -> _store_history.HistoryDelta | None:
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
    ) -> SessionReadBatch | None:
        """Commit a Compaction checkpoint and its prompt epoch while *since* is current.

        Returns the entries after *since*, or ``None`` (nothing written) when
        another writer advanced first.
        """
        committed = self._execute_write(
            lambda connection: _store_operations.commit_compaction(
                connection, address, checkpoint, since=since, epoch=epoch, run_id=run_id
            ),
            patience_s=TRANSCRIPT_WRITE_PATIENCE_S,
        )
        if committed is None:
            return None
        return _store_history.read_batch(committed)

    def apply_edit(
        self,
        address: SessionAddress,
        *,
        target_message_id: str,
        messages: Sequence[ChatMessage],
        run_id: str | None,
        seen_skills: SeenSkillsUpdate | None = None,
        subagent_reply_target: str | None = None,
    ) -> SessionReadBatch:
        """Replace history from one User message on; see ``_store_operations.apply_edit``.

        *subagent_reply_target* records the answer destination in the same transaction, as in
        :meth:`append_messages`. Returns the Session's complete own audit and
        current view after the edit.
        """

        def _fn(connection: sqlite3.Connection) -> _store_history.HistoryDelta:
            if subagent_reply_target is not None:
                _store_mutations.record_subagent_reply_target(
                    connection, address, subagent_reply_target
                )
            delta = _store_operations.apply_edit(
                connection,
                address,
                target_message_id=target_message_id,
                messages=messages,
                run_id=run_id,
                seen_skills=seen_skills,
            )
            return delta

        delta = self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)
        return _store_history.read_batch(delta)

    # -- Extension-owned Sessions -----------------------------------------------------

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

    def single_active_user_message(self, address: SessionAddress) -> tuple[int, ChatMessage] | None:
        return self._read_decoded(
            lambda connection: _store_history.single_active_user_message(connection, address)
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

    def tool_result_payload_by_id(self, payload_id: str, *, owner_name: str) -> str | None:
        return self._read(
            lambda connection: _store_history.tool_result_payload_by_id(
                connection, payload_id, owner_name
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
        complete_runs: bool = False,
        focus_run_id: str | None = None,
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
                complete_runs=complete_runs,
                focus_run_id=focus_run_id,
                background_tool_names=background_tool_names,
                background_note_marker=background_note_marker,
                after=after,
                skip_unchanged=skip_unchanged,
            )
        )

    def status_snapshot(
        self,
        address: SessionAddress,
    ) -> tuple[str | None, int, JsonObject | None, JsonObject, list[ChatMessage]]:
        return self._read_decoded(
            lambda connection: _store_history.status_snapshot(connection, address)
        )

    def run_records(
        self, address: SessionAddress, expected_generation_id: str | None = None
    ) -> tuple[SessionRunRecord, ...]:
        return self._read(
            lambda connection: _store_history.run_records(
                connection, address, expected_generation_id
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

    def expired_ephemeral_addresses(self, before: str, limit: int) -> list[SessionAddress]:
        return self._read(
            lambda connection: _store_queries.expired_ephemeral_addresses(connection, before, limit)
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

    def list_project_groups(
        self,
        grouping: SessionProjectGrouping,
        *,
        limit: int,
        filters: SessionListFilters,
    ) -> dict[str | None, SessionListPage]:
        return self._read_decoded(
            lambda connection: _store_queries.list_project_groups(
                connection, grouping, limit=limit, filters=filters
            )
        )

    def list_project_page(
        self,
        grouping: SessionProjectGrouping,
        project_id: str | None,
        *,
        limit: int,
        cursor: SessionListCursor | None,
        filters: SessionListFilters,
    ) -> SessionListPage:
        return self._read_decoded(
            lambda connection: _store_queries.list_project_page(
                connection, grouping, project_id, limit=limit, cursor=cursor, filters=filters
            )
        )

    def summary(self, address: SessionAddress) -> JsonObject | None:
        return self._read_decoded(lambda connection: _store_queries.summary(connection, address))

    def change_stats(
        self, address: SessionAddress, merged: Sequence[SessionAddress] = ()
    ) -> JsonObject | None:
        """Return the Session's change statistics with its changed files, or ``None``."""
        return self._read(
            lambda connection: _store_queries.session_change_stats(connection, address, merged)
        )

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

    def list_all_history_revisions(self) -> list[SessionHistoryRevision]:
        return self._read_decoded(_store_queries.list_all_history_revisions)

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]:
        return self._read(
            lambda connection: _store_queries.list_history_versions(connection, addresses)
        )

    # -- Search -------------------------------------------------------------------------

    def conversation_page(
        self,
        address: SessionAddress,
        *,
        expected_generation_id: str,
        limit: int = 8,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
        max_text_chars: int = 2000,
        include_peer_messages: bool = True,
    ) -> SessionConversationPage:
        """Return bounded current User/Assistant text with generation checks."""
        return self._read(
            lambda connection: _store_conversation.conversation_page(
                connection,
                address,
                expected_generation_id=expected_generation_id,
                limit=limit,
                before_sequence=before_sequence,
                after_sequence=after_sequence,
                max_text_chars=max_text_chars,
                include_peer_messages=include_peer_messages,
            )
        )

    def fts_health(self) -> FtsHealth:
        """Return operator-facing FTS state with explicit canonical coverage checks."""
        return self._health.check(
            lambda: self._read(
                lambda connection: _store_fts._fts_health_from_connection(
                    connection, verify_coverage=True
                )
            )
        )

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

    def search_messages(self, request: SessionSearchRequest) -> SessionSearchResult:
        def select(*, use_fts: bool, fallback_reason: str | None = None) -> SessionSearchResult:
            return self._read(
                lambda connection: _store_search.search(
                    connection,
                    request,
                    use_fts=use_fts,
                    fallback_reason=fallback_reason,
                )
            )

        if not request.use_fts:
            return select(use_fts=False)
        try:
            return select(use_fts=True)
        except sqlite3.Error as exc:
            if _is_fts_failure(exc):
                error_message = str(exc)
                with suppress(Exception):
                    self._execute_write(
                        lambda connection: _store_fts._detach_fts(connection, error_message)
                    )
            return select(use_fts=False, fallback_reason="fts_error")
