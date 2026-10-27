"""Session service lifecycle, admission and metadata policy."""

from __future__ import annotations

import builtins
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.runs import RunKind
from core.sessions._archive_types import ArchiveEntryRef
from core.sessions._io import _SessionWriteLock, _SessionWriteLocks
from core.sessions._lifecycle import SessionLifecycle
from core.sessions._metadata import (
    _validate_session_id,
)
from core.sessions._metadata import (
    new_session_id as new_session_id,
)
from core.sessions._run_persistence import SessionRunPersistence
from core.sessions._search_request import SessionSearchRequest
from core.sessions._types import (
    AGENT_DEFAULT_PROJECT,
    AccessorInputRecord,
    DeliveryReceipt,
    JsonObject,
    OwnedRunRecord,
    OwnedSessionSummary,
    SeenSkillsUpdate,
    SessionAddress,
    SessionConversationPage,
    SessionDescriptorSource,
    SessionHistoryRevision,
    SessionIdentityReferenceUpdate,
    SessionListCursor,
    SessionListFilters,
    SessionListPage,
    SessionProjectGrouping,
    SessionReadBatch,
    SessionReadCursor,
    SessionSearchOrder,
    SessionSearchResult,
    SessionWorkingProject,
    TemporarySessionBinding,
    ToolResultFacts,
    WorkingProjectChoice,
)
from core.sessions.archive_ledger import SessionArchiveLedger
from core.sessions.errors import FtsHealth, SessionNotFoundError
from core.sessions.notifications import SessionNotifications
from core.sessions.session import ChatSession
from core.sessions.store import SessionStore
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.database import Database
    from core.runs import Run


_LOGGER = get_logger("sessions")


class ChatSessionManager:
    """One SQLite-only Session service, injected into Runtime consumers."""

    def __init__(
        self, data_dir: Path, store: SessionStore | None = None, *, store_path: Path | None = None
    ) -> None:
        self.data_dir = data_dir
        self._store = store or SessionStore(store_path or data_dir / "sessions.db")
        self._owns_store = store is None
        self._archive_ledger = SessionArchiveLedger(self._store)
        self._completion_read_callbacks: list[Callable[[SessionAddress, str], None]] = []
        self._write_locks = _SessionWriteLocks()
        self._runs = SessionRunPersistence(
            self._store,
            lambda address, run_id: self._lifecycle._notify_callbacks(
                "completion_read", self._completion_read_callbacks, address, run_id
            ),
        )
        self.notifications = SessionNotifications(
            self._store,
            lambda address, run_id: self._lifecycle._notify_callbacks(
                "completion_read", self._completion_read_callbacks, address, run_id
            ),
        )
        self._lifecycle = SessionLifecycle(
            self._store, self.write_lock, self._archive_ledger.notify_changed
        )

    def close(self) -> None:
        self._write_locks.clear()
        if self._owns_store:
            self._store.close()

    @property
    def database(self) -> Database:
        """The Session database handle, for data snapshots and data-store status."""
        return self._store.database

    @property
    def history_id(self) -> str:
        """Opaque continuity token of the Session history for projections that follow it.

        Stable across restarts. A data snapshot restore keeps the database
        identity but brings back older history, after which a Session can reach
        a ``(generation_id, history_revision)`` it had before, with other
        content. The token therefore also names the database's latest restore
        (``Database.restore_id``): a projection that sees another token rereads
        every Session instead of trusting its freshness stamps.
        """
        database = self._store.database
        return f"{database.database_id}:{database.restore_id or ''}"

    @property
    def archive_ledger(self) -> SessionArchiveLedger:
        """The archive entries kept beside the Sessions they hold."""
        return self._archive_ledger

    def usage_history(
        self, after_entry_key: int = 0, *, limit: int = 1000
    ) -> tuple[JsonObject, ...]:
        """Accounting-only own audit across live and archived Session generations."""
        return self._store.usage_history(after_entry_key, limit=limit)

    async def run_async[Result](
        self, function: Callable[..., Result], *arguments: Any, **keyword_arguments: Any
    ) -> Result:
        """Run blocking work that reads or writes Sessions on the Session database's pool.

        For a caller's own unit of Session work, such as several reads and their
        projection, that has no dedicated ``*_async`` method. A closed Session
        database raises :class:`~core.database.DatabaseUnavailableError`.
        """
        return await self._store.run_async(function, *arguments, **keyword_arguments)

    def add_title_changed_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]:
        self._lifecycle.title_changed_callbacks.append(callback)
        return lambda: (
            self._lifecycle.title_changed_callbacks.remove(callback)
            if callback in self._lifecycle.title_changed_callbacks
            else None
        )

    def add_completion_read_callback(
        self, callback: Callable[[SessionAddress, str], None]
    ) -> Callable[[], None]:
        self._completion_read_callbacks.append(callback)
        return lambda: (
            self._completion_read_callbacks.remove(callback)
            if callback in self._completion_read_callbacks
            else None
        )

    def set_agent_default_project(self, provider: Callable[[str], str | None] | None) -> None:
        """Name where a new Session of an Identity Agent works when its creator names nothing.

        *provider* maps an Identity Agent id to its default Project (``None``:
        its Workspace) and must not raise. Without one, such Sessions work in
        the Workspace.
        """
        self._lifecycle.agent_default_project = provider

    def write_lock(self, address: SessionAddress) -> _SessionWriteLock:
        return self._write_locks.get(address)

    def create(
        self,
        agent_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
        *,
        actor: str | None = None,
        run_kind: RunKind | None = None,
        metadata: JsonObject | None = None,
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
    ) -> ChatSession:
        return self._lifecycle.create(
            agent_id,
            session_id,
            project_id,
            actor=actor,
            run_kind=run_kind,
            metadata=metadata,
            working_project_id=working_project_id,
        )

    async def move(self, source: SessionAddress, target: SessionAddress) -> ChatSession:
        return await self._lifecycle.move(source, target)

    async def fork(
        self,
        source: SessionAddress,
        *,
        target_agent_id: str | None = None,
        target_project_id: str | None = None,
        title: str | None = None,
        run_kind: RunKind | None = None,
    ) -> ChatSession:
        return await self._lifecycle.fork(
            source,
            target_agent_id=target_agent_id,
            target_project_id=target_project_id,
            title=title,
            run_kind=run_kind,
        )

    async def archive(
        self,
        address: SessionAddress,
        *,
        facts: Mapping[str, Any] | None = None,
        cleanup_pending: bool = False,
    ) -> ArchiveEntryRef:
        return await self._lifecycle.archive(address, facts=facts, cleanup_pending=cleanup_pending)

    def set_title(self, address: SessionAddress, title: str) -> str | None:
        return self._lifecycle.set_title(address, title)

    def set_auto_title(
        self,
        address: SessionAddress,
        title: str,
        *,
        initialized: bool = True,
        expected_generation_id: str | None = None,
        expected_first_user_sequence: int | None = None,
    ) -> str | None:
        return self._lifecycle.set_auto_title(
            address,
            title,
            initialized=initialized,
            expected_generation_id=expected_generation_id,
            expected_first_user_sequence=expected_first_user_sequence,
        )

    def mark_auto_title_initialized(self, address: SessionAddress) -> None:
        return self._lifecycle.mark_auto_title_initialized(address)

    async def create_async(
        self,
        agent_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
        *,
        actor: str | None = None,
        run_kind: RunKind | None = None,
        metadata: JsonObject | None = None,
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
    ) -> ChatSession:
        return await self._store.run_async(
            lambda: self.create(
                agent_id,
                session_id,
                project_id,
                actor=actor,
                run_kind=run_kind,
                metadata=metadata,
                working_project_id=working_project_id,
            )
        )

    def exists(self, address: SessionAddress) -> bool:
        _validate_session_id(address.session_id)
        return self._store.exists(address)

    def existing_addresses(self, addresses: Sequence[SessionAddress]) -> set[SessionAddress]:
        """Return which *addresses* name live Sessions, in one set-oriented read."""
        return self._store.existing_addresses(addresses)

    def subagent_children(self, parent: SessionAddress) -> list[SessionAddress]:
        """Return the live Sub-Agent Sessions linked to *parent*, oldest first."""
        return self._store.subagent_children(parent)

    def subagent_session(self, subagent_id: str) -> SessionAddress | None:
        """Return the live Sub-Agent Session with this public Sub-Agent id, if any."""
        return self._store.subagent_session(subagent_id)

    def live_addresses_with_metadata_field(
        self, key: str, field: str, value: str
    ) -> builtins.list[SessionAddress]:
        """Return the live Sessions whose metadata object *key* holds *field* = *value*."""
        return self._store.live_addresses_with_metadata_field(key, field, value)

    def get(self, address: SessionAddress) -> ChatSession:
        """Resolve a live Session and capture its generation for later Run admission."""
        return self._lifecycle.get(address)

    async def get_async(self, address: SessionAddress) -> ChatSession:
        return await self._store.run_async(self.get, address)

    def get_or_create(self, address: SessionAddress) -> ChatSession:
        """Return the live Session, creating it when missing (existing ones cost a read).

        A created Session of an Identity Agent works in the Agent's default Project.
        """
        return self._lifecycle.get_or_create(address)

    def get_metadata(self, address: SessionAddress) -> JsonObject:
        return self._store.metadata(address)

    def ensure_metadata(
        self,
        address: SessionAddress,
        mutation: Callable[[JsonObject], None],
        *,
        create_missing: bool = False,
    ) -> tuple[JsonObject, JsonObject]:
        """Re-assert metadata, writing only a real change (or a ``create_missing`` Session).

        The mutation may run twice, so it must be deterministic and side-effect free.
        A created Session of an Identity Agent works in the Agent's default Project.
        """
        return self._lifecycle.ensure_metadata(address, mutation, create_missing=create_missing)

    async def get_metadata_async(self, address: SessionAddress) -> JsonObject:
        return await self._store.run_async(self.get_metadata, address)

    def descriptor_sources(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, SessionDescriptorSource]:
        """Load descriptor inputs, including Recall visibility, for many Sessions at once."""
        return self._store.descriptor_sources(addresses)

    def set_metadata(self, address: SessionAddress, data: JsonObject) -> None:
        self._store.replace_metadata(address, data)

    def mutate_metadata(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> JsonObject:
        """Atomically mutate metadata without overwriting concurrent domain fields."""
        _previous, updated = self.mutate_metadata_with_previous(address, mutation)
        return updated

    def mutate_metadata_with_previous(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        """Atomically mutate metadata and return exact before/after snapshots."""
        return self._store.mutate_metadata(address, mutation)

    def working_project(self, address: SessionAddress) -> SessionWorkingProject:
        """Read the Working Project binding, including an archived original incarnation."""
        return self._store.working_project(address)

    async def working_project_async(self, address: SessionAddress) -> SessionWorkingProject:
        return await self._store.run_async(self.working_project, address)

    def metadata_value(self, address: SessionAddress, key: str) -> Any:
        """Read one metadata value (``None`` when absent) from the Session row alone."""
        return self._store.metadata_value(address, key)

    @property
    def metadata_revision(self) -> int:
        """Changes after every metadata write of this process (``SessionStore``)."""
        return self._store.metadata_revision

    async def metadata_value_async(self, address: SessionAddress, key: str) -> Any:
        return await self._store.run_async(self.metadata_value, address, key)

    def prompt_cache_affinity_id(self, address: SessionAddress) -> str:
        """Return the Session's prompt-cache affinity id (forks in one scope share it)."""
        return self._store.prompt_cache_affinity_id(address)

    def prompt_pin(self, address: SessionAddress, slot: str) -> JsonObject | None:
        """Return the value pinned in one prompt pin *slot*, or ``None``.

        A pin keeps one prompt input fixed for the current prompt epoch; a
        Compaction commit starts the next epoch with fresh pins.
        """
        return self._store.prompt_pin(address, slot)

    def ensure_prompt_pin(
        self,
        address: SessionAddress,
        slot: str,
        value: JsonObject,
        accept: Callable[[JsonObject], bool],
    ) -> JsonObject:
        """Keep an acceptable pin in *slot*, else pin *value*; return the pin in effect."""
        return self._store.ensure_prompt_pin(address, slot, value, accept)

    def seen_skills(self, address: SessionAddress) -> frozenset[str] | None:
        """Return the Skills announced to this Session, or ``None`` before the first record."""
        return self._store.seen_skills(address)

    def record_seen_skills(self, address: SessionAddress, update: SeenSkillsUpdate) -> None:
        self._store.record_seen_skills(address, update)

    def recover_interrupted_runs(self) -> None:
        return self._runs.recover_interrupted_runs()

    async def accept_accessor_input_async(
        self,
        address: SessionAddress,
        input_id: str,
        *,
        request_hash: str,
        payload: JsonObject,
        queue_id: str,
        expected_generation_id: str | None = None,
        attachment_ids: Sequence[str] = (),
        fallback_input: ChatMessage | None = None,
        fallback_notes: Sequence[ChatMessage] = (),
        source: SessionAddress | None = None,
        expected_source_generation_id: str | None = None,
        expected_working_project_id: str | None = None,
    ) -> tuple[AccessorInputRecord, bool]:
        """Commit ordinary input acceptance; exact retries return its original receipt."""
        return await self._store.run_async(
            self._store.execution.accept_accessor_input,
            address,
            input_id,
            request_hash=request_hash,
            payload=payload,
            queue_id=queue_id,
            expected_generation_id=expected_generation_id,
            attachment_ids=attachment_ids,
            fallback_input=fallback_input,
            fallback_notes=fallback_notes,
            source=source,
            expected_source_generation_id=expected_source_generation_id,
            expected_working_project_id=expected_working_project_id,
        )

    async def lookup_accessor_input_async(
        self, input_id: str, *, request_hash: str | None = None
    ) -> AccessorInputRecord | None:
        """Resolve an input before creating a new Session, including archived receipts."""
        return await self._store.run_async(
            self._store.execution.lookup_accessor_input, input_id, request_hash=request_hash
        )

    def queued_accessor_inputs(self) -> tuple[AccessorInputRecord, ...]:
        """Read unclaimed inputs of live generations in acceptance order for restart."""
        return self._store.execution.queued_accessor_inputs()

    async def queued_accessor_inputs_async(self) -> tuple[AccessorInputRecord, ...]:
        return await self._store.run_async(self.queued_accessor_inputs)

    async def update_accessor_input_async(
        self,
        address: SessionAddress,
        input_id: str,
        *,
        payload_patch: JsonObject,
        attachment_ids: Sequence[str] | None = None,
        fallback_input: ChatMessage | None = None,
        fallback_notes: Sequence[ChatMessage] | None = None,
    ) -> AccessorInputRecord:
        """Edit queued execution context, preserving unknown payload and the request hash."""
        return await self._store.run_async(
            self._store.execution.update_accessor_input,
            address,
            input_id,
            payload_patch=payload_patch,
            attachment_ids=attachment_ids,
            fallback_input=fallback_input,
            fallback_notes=fallback_notes,
        )

    async def remove_accessor_input_async(self, address: SessionAddress, input_id: str) -> bool:
        """Retire queued work while retaining its idempotency receipt."""
        return await self._store.run_async(
            self._store.execution.remove_accessor_input, address, input_id
        )

    async def start_run(self, run: Run) -> None:
        return await self._runs.start_run(run)

    async def finish_run(
        self, run: Run, status: str, payload: JsonObject, *, completion_reason: str | None
    ) -> JsonObject:
        return await self._runs.finish_run(
            run, status, payload, completion_reason=completion_reason
        )

    def mark_terminal_run_read(self, address: SessionAddress, run_id: str) -> JsonObject:
        return self._runs.mark_terminal_run_read(address, run_id)

    async def mark_terminal_run_read_async(
        self, address: SessionAddress, run_id: str
    ) -> JsonObject:
        return await self._store.run_async(self.mark_terminal_run_read, address, run_id)

    async def set_title_async(self, address: SessionAddress, title: str) -> str | None:
        return await self._store.run_async(self.set_title, address, title)

    def list(self, agent_id: str, project_id: str | None = None) -> list[ChatSession]:
        return [
            ChatSession(self._store, address)
            for address in self._store.list_addresses(project_id=project_id, agent_id=agent_id)
        ]

    def list_addresses(
        self,
        project_id: str | None = None,
        *,
        agent_id: str | None = None,
        exclude_owner_managed: bool = False,
    ) -> builtins.list[SessionAddress]:
        """List live addresses in one scope or one Agent, optionally without Extension ones."""
        return self._store.list_addresses(
            project_id=project_id,
            agent_id=agent_id,
            exclude_owner_managed=exclude_owner_managed,
        )

    def expired_ephemeral_addresses(
        self, before: str, *, limit: int
    ) -> builtins.list[SessionAddress]:
        """Live Ephemeral Sessions whose last activity is older than *before*.

        *before* is a canonical UTC timestamp; at most *limit* addresses, least
        recently active first.
        """
        return self._store.expired_ephemeral_addresses(before, limit)

    def referenced_attachment_ids(self) -> frozenset[str]:
        """Blob references retained by every live, archived and superseded history entry."""
        return self._store.referenced_attachment_ids()

    def unreferenced_attachment_ids(self, *, additional: Sequence[str] = ()) -> tuple[str, ...]:
        """Unowned known or supplied blobs, excluding retained snapshots.

        Only for quiescent lifecycle boundaries. Attachment collection supplies
        expired uploads through *additional*, including ones never admitted.
        """
        return self._store.unreferenced_attachment_ids(additional=additional)

    def forget_collected_attachment_ids(self, ids: Sequence[str]) -> None:
        """Acknowledge only blobs collection successfully removed."""
        self._store.forget_collected_attachment_ids(ids)

    def background_command_put(
        self,
        address: SessionAddress,
        command_id: str,
        origin_run_id: str,
        checkpoint: JsonObject,
        *,
        generation_id: str,
    ) -> None:
        """Persist an opaque command checkpoint bound to its original Session."""
        self._store.execution.background_command_put(
            address, command_id, origin_run_id, checkpoint, generation_id=generation_id
        )

    def background_command_read(self) -> builtins.list[JsonObject]:
        """Read command checkpoints with their current live Session addresses."""
        return self._store.execution.background_command_read()

    def background_command_delete(self, command_id: str) -> None:
        self._store.execution.background_command_delete(command_id)

    def background_command_delivered(self, command_id: str, body: str) -> bool:
        return self._store.execution.background_command_delivered(command_id, body)

    async def capture_tool_receipt_async(
        self,
        address: SessionAddress,
        *,
        owner_name: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        execution_owner: JsonObject | None,
    ) -> str | None:
        """Bind deferred Extension work to its original admitted invocation."""
        return await self.run_async(
            self._store.execution.capture_tool_receipt,
            address,
            owner_name=owner_name,
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            execution_owner=execution_owner,
        )

    async def resolve_tool_receipt_async(
        self,
        receipt_id: str,
        *,
        owner_name: str,
    ) -> JsonObject | None:
        return await self.run_async(
            self._store.execution.resolve_tool_receipt,
            receipt_id,
            owner_name=owner_name,
        )

    async def deliver_tool_outcome_async(
        self,
        receipt_id: str,
        content: str,
        *,
        owner_name: str,
    ) -> bool:
        """Persist a late Extension outcome once, without rerunning the invocation."""
        return await self.run_async(
            self._store.execution.deliver_tool_outcome,
            receipt_id,
            content,
            owner_name=owner_name,
        )

    def generation_address(self, generation_id: str) -> SessionAddress | None:
        """Find the live address of the same generation after a move or Agent rename."""
        return self._store.generation_address(generation_id)

    def append_notes_once(
        self,
        address: SessionAddress,
        delivery_id: str,
        notes: Sequence[str],
        *,
        generation_id: str,
    ) -> None:
        """Persist a complete external Note delivery once for its original generation."""
        self._store.execution.append_notes_once(
            address, delivery_id, notes, generation_id=generation_id
        )

    def list_agent_ids(
        self, project_id: str | None = None, *, exclude_owner_managed: bool = False
    ) -> builtins.list[str]:
        """Return each Agent id owning a live Session in one scope, in one indexed read."""
        return self._store.list_agent_ids(project_id, exclude_owner_managed=exclude_owner_managed)

    def newest_session_id(self, agent_id: str, project_id: str | None = None) -> str | None:
        """Return the most recently active listed Session (any run kind, no Extension one)."""
        page = self.list_summaries_page([(project_id, agent_id)], limit=1)
        return str(page.sessions[0]["id"]) if page.sessions else None

    def list_summaries(
        self,
        agent_id: str,
        project_id: str | None = None,
        *,
        metadata_keys: Sequence[str] = (),
    ) -> builtins.list[JsonObject]:
        """Return normalized Session-list fields without open-ended metadata."""
        return self._store.list_summaries(project_id, agent_id, metadata_keys=metadata_keys)

    def list_summaries_page(
        self,
        scopes: Sequence[tuple[str | None, str]],
        *,
        limit: int,
        cursor: SessionListCursor | None = None,
        filters: SessionListFilters | None = None,
        required_address: SessionAddress | None = None,
    ) -> SessionListPage:
        """Return a bounded Session-list read model without open-ended metadata.

        A live ``required_address`` within ``scopes`` joins the page even when
        the filters hide it.
        """
        return self._store.list_summaries_page(
            scopes,
            limit=limit,
            cursor=cursor,
            filters=filters or SessionListFilters(),
            required_address=required_address,
        )

    def list_project_groups(
        self,
        grouping: SessionProjectGrouping,
        *,
        limit: int,
        filters: SessionListFilters | None = None,
    ) -> dict[str | None, SessionListPage]:
        """Return the newest ``limit`` Sessions of every Agent per Working Project group.

        Every Project of ``grouping`` and the ``None`` group (Sessions working
        in no registered Project) get a page with its total count and cursor,
        also when empty; ``list_project_page`` continues a group.
        """
        return self._store.list_project_groups(
            grouping, limit=limit, filters=filters or SessionListFilters()
        )

    def list_project_page(
        self,
        grouping: SessionProjectGrouping,
        project_id: str | None,
        *,
        limit: int,
        cursor: SessionListCursor | None = None,
        filters: SessionListFilters | None = None,
    ) -> SessionListPage:
        """Return one keyset page of one Working Project group of ``grouping``."""
        return self._store.list_project_page(
            grouping,
            project_id,
            limit=limit,
            cursor=cursor,
            filters=filters or SessionListFilters(),
        )

    def summary(self, address: SessionAddress) -> JsonObject | None:
        """Read one live Session's list row by exact address; ``None`` if absent."""
        return self._store.summary(address)

    def change_stats(
        self, address: SessionAddress, *, merged: Sequence[SessionAddress] = ()
    ) -> JsonObject | None:
        """Return the lines the Session's own Runs changed, with its changed files.

        ``{files, added, removed, file_stats}`` or ``None`` when the Session
        changed no file; see ``sessions.md`` -> Interfaces. The own Runs of the
        live Sessions in *merged* (such as its Sub-Agents') count as its own.
        """
        return self._store.change_stats(address, merged)

    def list_completion_activity(
        self, scopes: Sequence[tuple[str | None, str]]
    ) -> dict[tuple[str | None, str], builtins.list[JsonObject]]:
        """Map every ``(project_id, agent_id)`` scope to its live Sessions with a completion."""
        return self._store.list_completion_activity(scopes)

    def list_history_revisions(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[SessionHistoryRevision]:
        """Return every live Session version of one scope with its Recall visibility."""
        return self._store.list_history_revisions(project_id, agent_id)

    def list_all_history_revisions(self) -> builtins.list[SessionHistoryRevision]:
        """Return every live Session version of every scope with its Recall visibility.

        One read, ordered by scope and Session id: a projection that follows
        every scope compares its freshness stamps without a read per scope.
        """
        return self._store.list_all_history_revisions()

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]:
        """Batched generation/revision lookup for derived-projection freshness."""
        return self._store.list_history_versions(addresses)

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
        """Read bounded current conversation text without hydrating Tool history."""
        return self._store.conversation_page(
            address,
            expected_generation_id=expected_generation_id,
            limit=limit,
            before_sequence=before_sequence,
            after_sequence=after_sequence,
            max_text_chars=max_text_chars,
            include_peer_messages=include_peer_messages,
        )

    def create_bound_temporary_session(
        self,
        address: SessionAddress,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str,
        config: JsonObject,
    ) -> TemporarySessionBinding:
        return self._lifecycle.create_bound_temporary_session(
            address,
            owner_name=owner_name,
            group_id=group_id,
            participant_id=participant_id,
            config=config,
        )

    async def delete_temporary_group(self, *, owner_name: str, group_id: str) -> int:
        return await self._lifecycle.delete_temporary_group(
            owner_name=owner_name, group_id=group_id
        )

    async def archive_temporary_group(self, *, owner_name: str, group_id: str, reason: str) -> int:
        return await self._lifecycle.archive_temporary_group(
            owner_name=owner_name, group_id=group_id, reason=reason
        )

    def temporary_binding(self, address: SessionAddress) -> TemporarySessionBinding | None:
        return self._store.execution.temporary_binding(address)

    def temporary_agent_binding(
        self, project_id: str | None, agent_id: str
    ) -> TemporarySessionBinding | None:
        """Return the live binding of the temporary Agent *agent_id*, or ``None``.

        Every Session under a temporary Agent's id runs as that Agent: its bound
        Session and the child Sessions it delegated to itself, which have no
        binding of their own. Bindings that share an id name no single Agent.
        """
        return self._store.execution.temporary_agent_binding(project_id, agent_id)

    def temporary_binding_by_participant(
        self, *, owner_name: str, group_id: str, participant_id: str
    ) -> TemporarySessionBinding | None:
        return self._store.execution.temporary_binding_by_participant(
            owner_name=owner_name, group_id=group_id, participant_id=participant_id
        )

    async def temporary_groups_async(
        self, *, owner_name: str, after: str = "", limit: int = 100
    ) -> builtins.list[str]:
        """Page the ids of one owner's groups that still have live bound Sessions."""
        return await self._store.run_async(
            lambda: self._store.execution.temporary_groups(
                owner_name=owner_name, after=after, limit=limit
            )
        )

    async def temporary_owners_async(self) -> builtins.list[str]:
        """Return the names of every owner that still has live bound Sessions."""
        return await self._store.run_async(self._store.execution.temporary_owners)

    async def temporary_bindings_async(
        self,
        *,
        owner_name: str,
        group_id: str,
        after: str = "",
        limit: int = 100,
    ) -> builtins.list[TemporarySessionBinding]:
        return await self._store.run_async(
            lambda: self._store.execution.temporary_bindings(
                owner_name=owner_name, group_id=group_id, after=after, limit=limit
            )
        )

    def set_temporary_group_title(self, *, owner_name: str, group_id: str, title: str) -> None:
        """Replace the display title of one owner's temporary execution group."""
        self._store.execution.set_temporary_group_title(
            owner_name=owner_name, group_id=group_id, title=title
        )

    async def set_temporary_group_title_async(
        self, *, owner_name: str, group_id: str, title: str
    ) -> None:
        await self._store.run_async(
            lambda: self.set_temporary_group_title(
                owner_name=owner_name, group_id=group_id, title=title
            )
        )

    def temporary_group_titles(
        self, *, owner_name: str, group_ids: Sequence[str]
    ) -> dict[str, str]:
        """Return stored display titles for this owner's groups, keyed by group id."""
        return self._store.execution.temporary_group_titles(
            owner_name=owner_name, group_ids=group_ids
        )

    async def temporary_group_titles_async(
        self, *, owner_name: str, group_ids: Sequence[str]
    ) -> dict[str, str]:
        return await self._store.run_async(
            lambda: self.temporary_group_titles(owner_name=owner_name, group_ids=group_ids)
        )

    def list_owned_session_summaries(
        self,
        *,
        owner_name: str | None = None,
        group_id: str | None = None,
        metadata_keys: Sequence[str] = (),
    ) -> builtins.list[OwnedSessionSummary]:
        """Return live owner-managed Sessions in creation order with binding labels.

        Derived projections use this set-oriented read instead of enumerating
        synthetic participant Agent ids. It exposes only display labels and the
        configured Model from the protected binding, never its complete
        configuration. ``group_id`` narrows one owner's group.
        """
        return self._store.execution.owned_session_summaries(
            owner_name=owner_name, group_id=group_id, metadata_keys=metadata_keys
        )

    async def list_owned_session_summaries_async(
        self,
        *,
        owner_name: str | None = None,
        group_id: str | None = None,
        metadata_keys: Sequence[str] = (),
    ) -> builtins.list[OwnedSessionSummary]:
        return await self._store.run_async(
            lambda: self.list_owned_session_summaries(
                owner_name=owner_name, group_id=group_id, metadata_keys=metadata_keys
            )
        )

    async def append_messages_with_receipts_async(
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
        return await self._store.run_async(
            lambda: self.append_messages_with_receipts(
                address,
                generation_id=generation_id,
                owner_name=owner_name,
                messages=messages,
                receipts=receipts,
                deduplicate_carrier=deduplicate_carrier,
                run_id=run_id,
                assistant_message_id=assistant_message_id,
                tool_results=tool_results,
                since=since,
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
        """Append owned deliveries; see ``SessionStore.append_messages`` for the options."""
        return self._store.execution.append_messages_with_receipts(
            address,
            generation_id=generation_id,
            owner_name=owner_name,
            messages=messages,
            receipts=receipts,
            deduplicate_carrier=deduplicate_carrier,
            run_id=run_id,
            assistant_message_id=assistant_message_id,
            tool_results=tool_results,
            since=since,
        )

    async def lookup_delivery_receipt(
        self,
        address: SessionAddress,
        generation_id: str,
        owner_name: str,
        receipt_id: str,
    ) -> DeliveryReceipt | None:
        return await self._store.run_async(
            lambda: self._store.execution.delivery_receipt(
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
    ) -> builtins.list[OwnedRunRecord]:
        return self._store.execution.owned_runs(
            owner_name=owner_name,
            group_id=group_id,
            participant_id=participant_id,
            after=after,
            limit=limit,
        )

    async def owned_runs_by_id_async(
        self, *, owner_name: str, group_id: str, run_ids: Sequence[str]
    ) -> dict[str, OwnedRunRecord]:
        """Read exact Run ids' execution records in one owner group (absent ids omitted)."""
        return await self._store.run_async(
            lambda: self._store.execution.owned_runs_by_id(
                owner_name=owner_name, group_id=group_id, run_ids=run_ids
            )
        )

    async def owned_run_by_input_async(
        self, address: SessionAddress, input_id: str
    ) -> OwnedRunRecord | None:
        """Read the execution record admitted for one input of a live Session."""
        return await self._store.run_async(
            self._store.execution.owned_run_by_input, address, input_id
        )

    async def tool_result_persisted_async(self, address: SessionAddress, tool_call_id: str) -> bool:
        """Report in one indexed probe whether a live Session holds a Tool call's result."""
        return await self._store.run_async(self._store.tool_result_persisted, address, tool_call_id)

    async def tool_result_payload_async(
        self, address: SessionAddress, payload_id: str, *, owner_name: str
    ) -> Any | None:
        """Load one payload *owner_name* attached to a Tool Result the Session's view shows.

        Returns ``None`` when the Session does not exist, the payload belongs to
        another owner, or its Tool Result is not in the current view.
        """
        try:
            payload_json = await self._store.run_async(
                lambda: self._store.tool_result_payload(address, payload_id, owner_name=owner_name)
            )
        except SessionNotFoundError:
            return None
        return None if payload_json is None else json.loads(payload_json)

    async def tool_result_payload_by_id_async(
        self, payload_id: str, *, owner_name: str
    ) -> Any | None:
        """Load payload *payload_id* of *owner_name* from whichever Session keeps it.

        For a holder of a capability over the payload id, such as a widget
        address the server issued; ``None`` once no Session keeps it.
        """
        payload_json = await self._store.run_async(
            lambda: self._store.tool_result_payload_by_id(payload_id, owner_name=owner_name)
        )
        return None if payload_json is None else json.loads(payload_json)

    def retarget_identity_agent_references(
        self, old_agent_id: str, new_agent_id: str
    ) -> tuple[SessionIdentityReferenceUpdate, ...]:
        """Point Sub-Agent parent links, live and archived, at an Identity Agent's moved Sessions.

        Run after :meth:`retarget_identity_agent_sessions`: a link naming
        ``old_agent_id`` changes only when its parent Session, live or archived,
        moved to ``new_agent_id`` (and is the generation the link recorded). A
        link to a parent that stayed behind or no longer exists keeps its id, so
        reversing the ids moves back only links to moved Sessions, and a repeated
        call after an interruption finishes the retarget.
        """
        return self._store.retarget_identity_agent_references(old_agent_id, new_agent_id)

    def retarget_metadata_value(
        self,
        agent_id: str,
        key: str,
        old_value: str,
        new_value: str,
        *,
        matching: Mapping[str, str] | None = None,
    ) -> int:
        """Point one open metadata value of an Identity Agent's global Sessions elsewhere.

        Every live or archived global Session of ``agent_id`` whose metadata
        ``key`` holds ``old_value``, and whose ``matching`` keys hold the given
        values, gets ``new_value`` in one transaction, such as the Librarian
        Sessions bound to a renamed Agent; an archived one keeps it through a
        restore. Returns how many changed; only values still naming ``old_value``
        change, so a repeated call after an interruption finishes it.
        """
        return self._store.retarget_metadata_value(
            agent_id, key, old_value, new_value, matching=matching
        )

    def delete(self, address: SessionAddress) -> None:
        """Delete one live Session; its live review forks go first.

        A review fork with a running Run stays, whole.
        """
        self._store.delete(address)

    def discard_unused(self, address: SessionAddress, generation_id: str) -> bool:
        """Discard only this newly created generation if no history or Run has used it.

        The caller must also hold its Run admission guard to exclude pending admission.
        """
        return self._store.discard_unused(address, generation_id)

    def review_forks(
        self, address: SessionAddress, *, recursive: bool = True
    ) -> tuple[SessionAddress, ...]:
        """The live review forks that follow one live Session, oldest first.

        Review ownership is captured in the original scope and survives source
        moves. Nested review forks follow too unless ``recursive=False`` asks
        only for direct reviews. Archive and delete take the whole tree along,
        so a caller guarding the Session's Runs must guard theirs as well.
        """
        _validate_session_id(address.session_id)
        return self._store.review_forks(address, recursive=recursive)

    def retarget_identity_agent_sessions(self, old_agent_id: str, new_agent_id: str) -> None:
        """Move every global Session of an Identity Agent to its new id, live and archived.

        The archived ones and the scope of the Agent's ``session`` archive entries
        follow in the same transaction, so a restore brings them back to the
        renamed Agent. Archived Sessions that an archived Agent of the old id
        holds in its own entry, or that an Extension manages, stay. Only
        Sessions still owned by ``old_agent_id`` move, so a repeated call after
        an interruption finishes the move.
        """
        if self._store.retarget_identity_agent(old_agent_id, new_agent_id):
            self._archive_ledger.notify_changed()

    def is_fts_available(self) -> bool:
        return self._store.is_fts_available()

    def fts_health(self) -> FtsHealth:
        return self._store.fts_health()

    def recall_context(self, address: SessionAddress, message_id: str) -> builtins.list[JsonObject]:
        """Return the bounded question/final-answer context for a past Message."""
        return self._store.recall_context(address, message_id)

    def search_messages(
        self,
        query: str,
        *,
        project_id: str | None,
        agent_id: str | None,
        session_id: str | None = None,
        match_mode: str = "all_terms",
        order: SessionSearchOrder = "relevance",
        limit: int = 1_000,
        roles: Sequence[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        excluded_session_ids: Sequence[str] = (),
        include_subagents: bool = False,
        use_fts: bool = True,
    ) -> SessionSearchResult:
        """Return up to ``limit`` active Messages whose conversation text matches.

        Only live Sessions whose Recall visibility the request admits are
        searched. Each candidate is checked literally (case-insensitive
        substring per ``match_mode``) before it counts, so results are exact
        and full when enough matches exist; a candidate budget marks longer
        searches incomplete. FTS supplies candidates by relevance or time;
        ``use_fts=False`` or an unavailable index scans by Message time.
        """
        return self._store.search_messages(
            SessionSearchRequest(
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
            )
        )
