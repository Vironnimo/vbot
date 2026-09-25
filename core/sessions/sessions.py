"""Session service lifecycle, admission and metadata policy."""

from __future__ import annotations

import builtins
import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from core.chat.errors import ChatSessionError
from core.runs import RunKind
from core.sessions._io import _SessionWriteLock
from core.sessions._metadata import (
    _normalize_session_title,
    _validate_agent_id,
    _validate_session_id,
)
from core.sessions._types import (
    SESSION_AUTO_TITLE_INITIALIZED_KEY,
    SESSION_AUTO_TITLE_KEY,
    SESSION_TITLE_KEY,
    DeliveryReceipt,
    JsonObject,
    OwnedRunRecord,
    OwnedSessionSummary,
    SeenSkillsUpdate,
    SessionAddress,
    SessionDescriptorSource,
    SessionHistoryRevision,
    SessionIdentityReferenceUpdate,
    SessionListCursor,
    SessionListFilters,
    SessionListPage,
    SessionReadBatch,
    SessionReadCursor,
    SessionRunAdmission,
    SessionRunCompletion,
    SessionSearchOrder,
    SessionSearchResult,
    TemporarySessionBinding,
    ToolResultFacts,
)
from core.sessions.errors import FtsHealth, SessionNotFoundError
from core.sessions.session import ChatSession
from core.sessions.store import SessionStore
from core.settings import is_valid_project_id

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.database import Database
    from core.runs import Run

_Result = TypeVar("_Result")


class ChatSessionManager:
    """One SQLite-only Session service, injected into Runtime consumers."""

    def __init__(
        self, data_dir: Path, store: SessionStore | None = None, *, store_path: Path | None = None
    ) -> None:
        self.data_dir = data_dir
        self._store = store or SessionStore(store_path or data_dir / "sessions.db")
        self._owns_store = store is None
        self._title_changed_callbacks: list[Callable[[SessionAddress], None]] = []
        self._completion_read_callbacks: list[Callable[[SessionAddress, str], None]] = []
        self._write_locks: dict[SessionAddress, _SessionWriteLock] = {}
        self._write_locks_guard = threading.Lock()

    def close(self) -> None:
        with self._write_locks_guard:
            self._write_locks.clear()
        if self._owns_store:
            self._store.close()

    @property
    def database(self) -> Database:
        """The Session database handle, for data snapshots and data-store status."""
        return self._store.database

    def usage_history(
        self, after_entry_key: int = 0, *, limit: int = 1000
    ) -> tuple[JsonObject, ...]:
        """Accounting-only own audit across live and archived Session generations."""
        return self._store.usage_history(after_entry_key, limit=limit)

    async def run_async(
        self, function: Callable[..., _Result], *arguments: Any, **keyword_arguments: Any
    ) -> _Result:
        """Run blocking work that reads or writes Sessions on the Session database's pool.

        For a caller's own unit of Session work, such as several reads and their
        projection, that has no dedicated ``*_async`` method. A closed Session
        database raises :class:`~core.database.DatabaseUnavailableError`.
        """
        return await self._store.run_async(function, *arguments, **keyword_arguments)

    def add_title_changed_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]:
        self._title_changed_callbacks.append(callback)
        return lambda: (
            self._title_changed_callbacks.remove(callback)
            if callback in self._title_changed_callbacks
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

    def write_lock(self, address: SessionAddress) -> _SessionWriteLock:
        _validate_session_id(address.session_id)
        with self._write_locks_guard:
            if address not in self._write_locks:
                self._write_locks[address] = _SessionWriteLock()
            return self._write_locks[address]

    def create(
        self, agent_id: str, session_id: str | None = None, project_id: str | None = None
    ) -> ChatSession:
        _validate_agent_id(agent_id)
        if project_id is not None and not is_valid_project_id(project_id):
            raise ChatSessionError("invalid project id")
        if session_id is not None:
            _validate_session_id(session_id)
        address = self._store.create(
            SessionAddress(project_id, agent_id, session_id or ""), generate_id=session_id is None
        )
        return ChatSession(self._store, address)

    async def create_async(
        self, agent_id: str, session_id: str | None = None, project_id: str | None = None
    ) -> ChatSession:
        return await self._store.run_async(self.create, agent_id, session_id, project_id)

    def exists(self, address: SessionAddress) -> bool:
        _validate_session_id(address.session_id)
        return self._store.exists(address)

    def existing_addresses(self, addresses: Sequence[SessionAddress]) -> set[SessionAddress]:
        """Return which *addresses* name live Sessions, in one set-oriented read."""
        return self._store.existing_addresses(addresses)

    def get(self, address: SessionAddress) -> ChatSession:
        _validate_session_id(address.session_id)
        self._store.require_live(address)
        return ChatSession(self._store, address)

    async def get_async(self, address: SessionAddress) -> ChatSession:
        return await self._store.run_async(self.get, address)

    def get_or_create(self, address: SessionAddress) -> ChatSession:
        """Return the live Session, creating it when missing (existing ones cost a read)."""
        _validate_creatable_address(address)
        self._store.ensure_live(address)
        return ChatSession(self._store, address)

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
        """
        if create_missing:
            _validate_creatable_address(address)
        return self._store.ensure_metadata(address, mutation, create_missing=create_missing)

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

    def metadata_value(self, address: SessionAddress, key: str) -> Any:
        """Read one metadata value (``None`` when absent) from the Session row alone."""
        return self._store.metadata_value(address, key)

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
        self._store.recover_interrupted_runs()

    async def start_run(self, run: Run) -> None:
        """Admit *run* with its Run kind and execution owner in one transaction."""
        address = SessionAddress(
            project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        admission = SessionRunAdmission(
            run_id=run.id,
            run_kind=run.run_kind.value,
            started_at=run.created_at,
            work_id=run.work_id,
            contributes_to_activity=run.contributes_to_agent_activity,
            owner=run.execution_owner,
            input_id=run.execution_input_id,
        )
        await self._store.run_async(self._store.admit_run, address, admission)

    async def finish_run(self, run: Run, status: str, payload: JsonObject) -> JsonObject:
        address = SessionAddress(
            project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        completion = SessionRunCompletion(
            run_id=run.id,
            work_id=run.work_id,
            status=status,
            timing=payload["timing"],
            iteration_count=run.iteration_count,
            change_stats=payload.get("change_stats"),
            completion_reason=run.cancel_reason,
            contributes_to_activity=run.contributes_to_agent_activity,
        )
        return await self._store.run_async(self._store.finish_run, address, completion)

    def mark_terminal_run_read(self, address: SessionAddress, run_id: str) -> JsonObject:
        activity, marked = self._store.mark_terminal_run_read(address, run_id)
        result = dict(activity)
        result["marked_read"] = marked
        if marked:
            self._notify_callbacks(self._completion_read_callbacks, address, run_id)
        return result

    async def mark_terminal_run_read_async(
        self, address: SessionAddress, run_id: str
    ) -> JsonObject:
        return await self._store.run_async(self.mark_terminal_run_read, address, run_id)

    def set_title(self, address: SessionAddress, title: str) -> str | None:
        normalized = _normalize_session_title(title)
        previous_metadata, _updated = self._store.mutate_metadata(
            address, lambda metadata: _set_title(metadata, normalized)
        )
        previous = previous_metadata.get(SESSION_TITLE_KEY)
        if previous != normalized:
            self._notify_callbacks(self._title_changed_callbacks, address)
        return normalized

    async def set_title_async(self, address: SessionAddress, title: str) -> str | None:
        return await self._store.run_async(self.set_title, address, title)

    def set_auto_title(
        self, address: SessionAddress, title: str, *, initialized: bool = True
    ) -> str | None:
        normalized = _normalize_session_title(title)

        def update(metadata: JsonObject) -> None:
            if normalized is None:
                metadata.pop(SESSION_AUTO_TITLE_KEY, None)
            else:
                metadata[SESSION_AUTO_TITLE_KEY] = normalized
            if initialized:
                metadata[SESSION_AUTO_TITLE_INITIALIZED_KEY] = True

        previous_metadata, _updated = self._store.mutate_metadata(address, update)
        previous = previous_metadata.get(SESSION_AUTO_TITLE_KEY)
        if previous != normalized:
            self._notify_callbacks(self._title_changed_callbacks, address)
        return normalized

    def mark_auto_title_initialized(self, address: SessionAddress) -> None:
        self._store.mutate_metadata(
            address,
            lambda metadata: metadata.__setitem__(SESSION_AUTO_TITLE_INITIALIZED_KEY, True),
        )

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

    def summary(self, address: SessionAddress) -> JsonObject | None:
        """Read one live Session's list row by exact address; ``None`` if absent."""
        return self._store.summary(address)

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

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]:
        """Batched generation/revision lookup for derived-projection freshness."""
        return self._store.list_history_versions(addresses)

    def create_bound_temporary_session(
        self,
        address: SessionAddress,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str,
        config: JsonObject,
    ) -> TemporarySessionBinding:
        generation_id = self._store.create_bound_temporary_session(
            address,
            owner_name=owner_name,
            group_id=group_id,
            participant_id=participant_id,
            config=config,
        )
        binding = self.temporary_binding_by_participant(
            owner_name=owner_name, group_id=group_id, participant_id=participant_id
        )
        if binding is None or binding.generation_id != generation_id:
            raise ChatSessionError("temporary Session binding was not retained")
        return binding

    def temporary_binding(self, address: SessionAddress) -> TemporarySessionBinding | None:
        return self._store.temporary_binding(address)

    def temporary_binding_by_participant(
        self, *, owner_name: str, group_id: str, participant_id: str
    ) -> TemporarySessionBinding | None:
        return self._store.temporary_binding_by_participant(
            owner_name=owner_name, group_id=group_id, participant_id=participant_id
        )

    async def delete_temporary_group(self, *, owner_name: str, group_id: str) -> int:
        """Delete bound participant Sessions after their owner has drained execution."""
        return await self._store.run_async(
            lambda: self._store.delete_temporary_group(owner_name=owner_name, group_id=group_id)
        )

    async def temporary_bindings_async(
        self,
        *,
        owner_name: str,
        group_id: str,
        after: str = "",
        limit: int = 100,
    ) -> builtins.list[TemporarySessionBinding]:
        return await self._store.run_async(
            lambda: self._store.temporary_bindings(
                owner_name=owner_name, group_id=group_id, after=after, limit=limit
            )
        )

    def set_temporary_group_title(self, *, owner_name: str, group_id: str, title: str) -> None:
        """Replace the display title of one owner's temporary execution group."""
        self._store.set_temporary_group_title(owner_name=owner_name, group_id=group_id, title=title)

    async def set_temporary_group_title_async(
        self, *, owner_name: str, group_id: str, title: str
    ) -> None:
        await self._store.run_async(
            lambda: self.set_temporary_group_title(
                owner_name=owner_name, group_id=group_id, title=title
            )
        )

    async def temporary_group_titles_async(
        self, *, owner_name: str, group_ids: Sequence[str]
    ) -> dict[str, str]:
        """Return stored display titles for this owner's groups, keyed by group id."""
        return await self._store.run_async(
            lambda: self._store.temporary_group_titles(owner_name=owner_name, group_ids=group_ids)
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
        return self._store.owned_session_summaries(
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
        continuation_records: Sequence[JsonObject] = (),
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
                continuation_records=continuation_records,
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
        continuation_records: Sequence[JsonObject] = (),
        since: SessionReadCursor | None = None,
    ) -> SessionReadBatch | None:
        """Append owned deliveries; see ``SessionStore.append_messages`` for the options."""
        return self._store.append_messages_with_receipts(
            address,
            generation_id=generation_id,
            owner_name=owner_name,
            messages=messages,
            receipts=receipts,
            deduplicate_carrier=deduplicate_carrier,
            run_id=run_id,
            assistant_message_id=assistant_message_id,
            tool_results=tool_results,
            continuation_records=continuation_records,
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
            lambda: self._store.delivery_receipt(
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
        return self._store.owned_runs(
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
            lambda: self._store.owned_runs_by_id(
                owner_name=owner_name, group_id=group_id, run_ids=run_ids
            )
        )

    async def owned_run_by_input_async(
        self, address: SessionAddress, input_id: str
    ) -> OwnedRunRecord | None:
        """Read the execution record admitted for one input of a live Session."""
        return await self._store.run_async(self._store.owned_run_by_input, address, input_id)

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

    def retarget_identity_agent_references(
        self, old_agent_id: str, new_agent_id: str
    ) -> tuple[SessionIdentityReferenceUpdate, ...]:
        return self._store.retarget_identity_agent_references(old_agent_id, new_agent_id)

    def restore_identity_agent_references(
        self, updates: tuple[SessionIdentityReferenceUpdate, ...]
    ) -> None:
        self._store.restore_identity_agent_references(updates)

    async def move(self, source: SessionAddress, target: SessionAddress) -> ChatSession:
        """Give a Session a new address; history, forks and relations stay attached.

        A move into another scope leaves the Agent-bound prompt state behind and
        starts a new prompt-cache affinity.
        """
        _validate_session_id(source.session_id)
        _validate_creatable_address(target)
        async with self.write_lock(source):
            await self._store.run_async(self._store.move, source, target)
        return ChatSession(self._store, target)

    async def fork(
        self,
        source: SessionAddress,
        *,
        target_agent_id: str | None = None,
        target_project_id: str | None = None,
        title: str | None = None,
        run_kind: RunKind | None = None,
    ) -> ChatSession:
        """Fork a Session under a fresh id in one write.

        The fork shares the source's current history up to its settled end
        without copying it. It belongs to ``target_agent_id`` (default: the
        source's Agent) in ``target_project_id`` (``None``: outside any
        Project). Channel, Sub-Agent and reflection bindings stay behind;
        ``title`` and ``run_kind`` label the fork instead.
        """
        _validate_session_id(source.session_id)
        agent_id = target_agent_id or source.agent_id
        _validate_agent_id(agent_id)
        if target_project_id is not None and not is_valid_project_id(target_project_id):
            raise ChatSessionError("invalid project id")
        if run_kind is not None and not isinstance(run_kind, RunKind):
            raise ChatSessionError("run kind must be a RunKind")
        normalized_title = None if title is None else _normalize_session_title(title) or ""
        same_scope = agent_id == source.agent_id and target_project_id == source.project_id
        async with self.write_lock(source):
            target = await self._store.run_async(
                lambda: self._store.fork(
                    source,
                    SessionAddress(target_project_id, agent_id, ""),
                    title=normalized_title,
                    run_kind=None if run_kind is None else run_kind.value,
                    allow_owner_managed_source=target_agent_id is not None and not same_scope,
                )
            )
        if title is not None:
            self._notify_callbacks(self._title_changed_callbacks, target)
        return ChatSession(self._store, target)

    def delete(self, address: SessionAddress) -> None:
        self._store.delete(address)

    async def archive(self, address: SessionAddress) -> None:
        async with self.write_lock(address):
            await self._store.run_async(self._store.archive, address)

    def restore(self, address: SessionAddress) -> None:
        self._store.restore(address)

    def retarget_identity_agent_sessions(self, old_agent_id: str, new_agent_id: str) -> None:
        self._store.retarget_identity_agent(old_agent_id, new_agent_id)

    def archive_identity_agent_sessions(self, agent_id: str) -> None:
        self._store.archive_identity_agent_sessions(agent_id)

    def archive_project_sessions(self, project_id: str) -> None:
        self._store.archive_project_sessions(project_id)

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

    @staticmethod
    def _notify_callbacks(callbacks: Sequence[Callable[..., None]], *args: Any) -> None:
        for callback in list(callbacks):
            try:
                callback(*args)
            except Exception:
                logging.getLogger(__name__).exception("Session callback failed")


def _validate_creatable_address(address: SessionAddress) -> None:
    _validate_agent_id(address.agent_id)
    _validate_session_id(address.session_id)
    if address.project_id is not None and not is_valid_project_id(address.project_id):
        raise ChatSessionError("invalid project id")


def _set_title(metadata: JsonObject, normalized: str | None) -> None:
    if normalized is None:
        metadata.pop(SESSION_TITLE_KEY, None)
    else:
        metadata[SESSION_TITLE_KEY] = normalized
