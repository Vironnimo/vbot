"""Session service lifecycle, admission and metadata policy."""

from __future__ import annotations

import builtins
import json
import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.runs import RunExecutionOwner, RunKind
from core.sessions._io import (
    _run_session_io,
    _SessionWriteLock,
)
from core.sessions._metadata import (
    _completion_activity_from_state,
    _completion_activity_payload,
    _decode_state_object,
    _default_prompt_cache_affinity_id,
    _format_timestamp,
    _is_prompt_cache_affinity_id,
    _new_prompt_cache_affinity_id,
    _normalize_session_title,
    _session_list_summary_from_state,
    _valid_latest_completion,
    _validate_agent_id,
    _validate_session_id,
)
from core.sessions._types import (
    FORK_SOURCE_META_KEY,
    PROMPT_CACHE_AFFINITY_META_KEY,
    SESSION_AUTO_TITLE_INITIALIZED_KEY,
    SESSION_AUTO_TITLE_KEY,
    SESSION_RUN_KINDS_META_KEY,
    SESSION_TERMINAL_RUN_STATUSES,
    SESSION_TITLE_KEY,
    DeliveryReceipt,
    JsonObject,
    OwnedRunRecord,
    RunStartBoundary,
    SessionAddress,
    SessionDescriptorSource,
    SessionIdentityReferenceUpdate,
    SessionListCursor,
    SessionListFilters,
    SessionListPage,
    TemporarySessionBinding,
)
from core.sessions.errors import FtsHealth
from core.sessions.session import ChatSession
from core.sessions.store import SessionStore
from core.settings import is_valid_project_id

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


class ChatSessionManager:
    """One SQLite-only Session service, injected into Runtime consumers."""

    def __init__(
        self, data_dir: Path, store: SessionStore | None = None, *, store_path: Path | None = None
    ) -> None:
        self.data_dir = data_dir
        self._store = store or SessionStore(store_path or data_dir / "sessions.db")
        self._owns_store = store is None
        self._title_changed_callbacks: list[Callable[[SessionAddress], None]] = []
        self._completion_read_callbacks: list[Callable[[SessionAddress], None]] = []
        self._write_locks: dict[SessionAddress, _SessionWriteLock] = {}
        self._write_locks_guard = threading.Lock()

    def close(self) -> None:
        with self._write_locks_guard:
            self._write_locks.clear()
        if self._owns_store:
            self._store.close()

    def backup_snapshot(
        self,
        destination: Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Write one consistent database copy for the snapshot engine."""
        return self._store.backup(destination, cancel_event=cancel_event)

    def create_snapshot(self, *, reason: str) -> Path | None:
        """Create one explicit verified snapshot."""
        from core.sessions.format import read_session_store_marker
        from core.sessions.snapshots import create_snapshot

        marker = read_session_store_marker(self.data_dir)
        database_id = None if marker is None else str(marker["database_id"])
        return create_snapshot(
            self.data_dir,
            self._store.path,
            self.backup_snapshot,
            database_id=database_id,
            reason=reason,
        )

    def status_projection(self) -> JsonObject:
        return self._store.status_projection()

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
        self, callback: Callable[[SessionAddress], None]
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
        return await _run_session_io(self.create, agent_id, session_id, project_id)

    def exists(self, address: SessionAddress) -> bool:
        _validate_session_id(address.session_id)
        return self._store.exists(address)

    async def exists_async(self, address: SessionAddress) -> bool:
        return await _run_session_io(self.exists, address)

    def get(self, address: SessionAddress) -> ChatSession:
        _validate_session_id(address.session_id)
        self._store.state(address)
        return ChatSession(self._store, address)

    async def get_async(self, address: SessionAddress) -> ChatSession:
        return await _run_session_io(self.get, address)

    def get_or_create(self, address: SessionAddress) -> ChatSession:
        _validate_agent_id(address.agent_id)
        _validate_session_id(address.session_id)
        if address.project_id is not None and not is_valid_project_id(address.project_id):
            raise ChatSessionError("invalid project id")
        self._store.ensure_live(address)
        return ChatSession(self._store, address)

    async def get_or_create_async(self, address: SessionAddress) -> ChatSession:
        return await _run_session_io(self.get_or_create, address)

    def get_metadata(self, address: SessionAddress) -> JsonObject:
        return self._store.metadata(address)

    async def get_metadata_async(self, address: SessionAddress) -> JsonObject:
        return await _run_session_io(self.get_metadata, address)

    def descriptor_source(self, address: SessionAddress) -> SessionDescriptorSource:
        metadata, message_count, first_user_message = self._store.descriptor_source(address)
        return SessionDescriptorSource(metadata, message_count, first_user_message)

    def descriptor_sources(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, SessionDescriptorSource]:
        return {
            address: SessionDescriptorSource(metadata, message_count, first_user_message)
            for address, (
                metadata,
                message_count,
                first_user_message,
            ) in self._store.descriptor_sources(addresses).items()
        }

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

    async def set_metadata_async(self, address: SessionAddress, data: JsonObject) -> None:
        await _run_session_io(self.set_metadata, address, data)

    def prompt_cache_affinity_id(self, address: SessionAddress) -> str:
        value = self.get_metadata(address).get(PROMPT_CACHE_AFFINITY_META_KEY)
        if value is None:
            return _default_prompt_cache_affinity_id(address)
        if not _is_prompt_cache_affinity_id(value):
            raise ChatSessionError(
                f"invalid prompt cache affinity id for session: {address.session_id}"
            )
        return cast(str, value)

    def rotate_prompt_cache_affinity_id(self, address: SessionAddress) -> str:
        value = _new_prompt_cache_affinity_id()
        self._store.mutate_metadata(
            address, lambda metadata: metadata.__setitem__(PROMPT_CACHE_AFFINITY_META_KEY, value)
        )
        return value

    def record_run_kind(self, address: SessionAddress, run_kind: RunKind) -> None:
        if not isinstance(run_kind, RunKind):
            raise ChatSessionError("run kind must be a RunKind")

        def update(metadata: JsonObject) -> None:
            values = metadata.get(SESSION_RUN_KINDS_META_KEY, [])
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value in {kind.value for kind in RunKind}
                for value in values
            ):
                raise ChatSessionError("session run_kinds metadata is invalid")
            if run_kind.value not in values:
                metadata[SESSION_RUN_KINDS_META_KEY] = [*values, run_kind.value]

        self._store.mutate_metadata(address, update)

    def record_terminal_run(
        self, address: SessionAddress, run_id: str, status: str, timestamp: str
    ) -> None:
        if not run_id or status not in SESSION_TERMINAL_RUN_STATUSES or not timestamp:
            raise ChatSessionError("invalid terminal Run completion")

        def update(activity: JsonObject) -> None:
            activity["latest_completion"] = {
                "run_id": run_id,
                "status": status,
                "timestamp": timestamp,
            }

        self._store.mutate_activity(address, update)

    def mark_terminal_run_read(self, address: SessionAddress, run_id: str) -> JsonObject:
        marked = False

        def update(activity: JsonObject) -> None:
            nonlocal marked
            latest = _valid_latest_completion(activity)
            marked = bool(
                latest and latest["run_id"] == run_id and activity.get("read_run_id") != run_id
            )
            if marked:
                activity["read_run_id"] = run_id

        _previous, activity = self._store.mutate_activity(address, update)
        result = _completion_activity_payload(activity)
        result["marked_read"] = marked
        if marked:
            self._notify_callbacks(self._completion_read_callbacks, address)
        return result

    async def mark_terminal_run_read_async(
        self, address: SessionAddress, run_id: str
    ) -> JsonObject:
        return await _run_session_io(self.mark_terminal_run_read, address, run_id)

    def set_title(self, address: SessionAddress, title: str) -> str | None:
        normalized = _normalize_session_title(title)

        def update(metadata: JsonObject) -> None:
            if normalized is None:
                metadata.pop(SESSION_TITLE_KEY, None)
            else:
                metadata[SESSION_TITLE_KEY] = normalized

        previous_metadata, _updated = self._store.mutate_metadata(address, update)
        previous = previous_metadata.get(SESSION_TITLE_KEY)
        if previous != normalized:
            self._notify_callbacks(self._title_changed_callbacks, address)
        return normalized

    async def set_title_async(self, address: SessionAddress, title: str) -> str | None:
        return await _run_session_io(self.set_title, address, title)

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

    def reset_auto_title(self, address: SessionAddress) -> None:
        def update(metadata: JsonObject) -> None:
            metadata.pop(SESSION_AUTO_TITLE_KEY, None)
            metadata.pop(SESSION_AUTO_TITLE_INITIALIZED_KEY, None)

        self._store.mutate_metadata(address, update)

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

    async def list_async(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[ChatSession]:
        return await _run_session_io(self.list, agent_id, project_id)

    def list_addresses(self, project_id: str | None = None) -> builtins.list[SessionAddress]:
        return self._store.list_addresses(project_id=project_id, agent_id=None)

    def list_with_metadata(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[JsonObject]:
        result: builtins.list[JsonObject] = []
        for state in self._store.list_state_rows(project_id, agent_id):
            summary = self._store.metadata_from_state(state)
            summary.update(_completion_activity_from_state(state))
            summary.update(
                id=state["session_id"],
                created_at=state["created_at"],
                last_active_at=state["last_message_at"] or state["created_at"],
            )
            result.append(summary)
        return result

    def list_summaries(
        self,
        agent_id: str,
        project_id: str | None = None,
        *,
        metadata_keys: Sequence[str] = (),
    ) -> builtins.list[JsonObject]:
        """Return normalized Session-list fields without open-ended metadata."""
        summaries: builtins.list[JsonObject] = []
        for state in self._store.list_summary_rows_for_scope(
            project_id,
            agent_id,
            metadata_keys=metadata_keys,
        ):
            summary = _session_list_summary_from_state(state)
            for key in metadata_keys:
                payload = state[f"metadata_{key}_json"]
                if payload is not None:
                    summary[key] = json.loads(str(payload))
            summaries.append(summary)
        return summaries

    def list_recall_summaries(
        self,
        agent_id: str,
        project_id: str | None = None,
        *,
        include_subagents: bool,
        excluded_session_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> builtins.list[JsonObject]:
        """Return the exact Recall-visible Session set from normalized list fields."""
        rows = self._store.list_recall_summary_rows(
            project_id,
            agent_id,
            include_subagents=include_subagents,
            excluded_session_id=excluded_session_id,
            since=since,
            until=until,
            limit=limit,
        )
        return [_session_list_summary_from_state(row) for row in rows]

    async def list_with_metadata_async(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[JsonObject]:
        return await _run_session_io(self.list_with_metadata, agent_id, project_id)

    def list_summaries_page(
        self,
        scopes: Sequence[tuple[str | None, str]],
        *,
        limit: int,
        cursor: SessionListCursor | None = None,
        filters: SessionListFilters | None = None,
        required_address: SessionAddress | None = None,
    ) -> SessionListPage:
        """Return a bounded Session-list read model without open-ended metadata."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ChatSessionError("Session list limit must be a positive integer")
        selected_filters = filters or SessionListFilters()
        cursor_value = (
            None
            if cursor is None
            else (
                cursor.active_sort,
                cursor.project_id or "",
                cursor.agent_id,
                cursor.session_id,
            )
        )
        rows, required_row, total_count, has_more = self._store.list_summary_rows(
            scopes,
            limit=limit,
            cursor=cursor_value,
            include_subagents=selected_filters.include_subagents,
            include_memory_reflections=selected_filters.include_memory_reflections,
            include_skill_reflections=selected_filters.include_skill_reflections,
            include_cron=selected_filters.include_cron,
            required_address=required_address,
        )
        summaries = [_session_list_summary_from_state(row) for row in rows]
        seen = {
            (summary.get("project_id"), summary["agent_id"], summary["id"]) for summary in summaries
        }
        if required_row is not None:
            required_summary = _session_list_summary_from_state(required_row)
            required_key = (
                required_summary.get("project_id"),
                required_summary["agent_id"],
                required_summary["id"],
            )
            if required_key not in seen:
                summaries.append(required_summary)
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = SessionListCursor(
                active_sort=float(last["active_sort"]),
                project_id=str(last["project_id"]) or None,
                agent_id=str(last["agent_id"]),
                session_id=str(last["session_id"]),
            )
        return SessionListPage(
            sessions=tuple(summaries),
            next_cursor=next_cursor,
            total_count=total_count,
        )

    def session_ids_with_messages(
        self,
        agent_id: str,
        project_id: str | None,
        roles: Sequence[str],
        since: datetime | None,
        until: datetime | None,
    ) -> set[str]:
        return self._store.session_ids_with_messages(
            project_id,
            agent_id,
            roles,
            since,
            until,
        )

    def list_completion_activity(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[JsonObject]:
        result: builtins.list[JsonObject] = []
        for state in self._store.list_activity_rows(project_id, agent_id):
            result.append({"id": state["session_id"], **_completion_activity_from_state(state)})
        return result

    async def list_completion_activity_async(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[JsonObject]:
        return await _run_session_io(self.list_completion_activity, agent_id, project_id)

    def list_history_revisions(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[tuple[SessionAddress, str, int]]:
        return self._store.list_history_revisions(project_id, agent_id)

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]:
        """Batched generation/revision lookup for derived-projection freshness."""
        return self._store.list_history_versions(addresses)

    def history_version(self, address: SessionAddress) -> tuple[str, int]:
        state = self._store.state(address)
        return str(state["generation_id"]), int(state["history_revision"])

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

    async def create_bound_temporary_session_async(
        self, *args: Any, **kwargs: Any
    ) -> TemporarySessionBinding:
        return await _run_session_io(self.create_bound_temporary_session, *args, **kwargs)

    def temporary_binding(self, address: SessionAddress) -> TemporarySessionBinding | None:
        row = self._store.temporary_binding(address)
        if row is None:
            return None
        return TemporarySessionBinding(
            address,
            str(row["generation_id"]),
            str(row["owner_name"]),
            str(row["group_id"]),
            str(row["participant_id"]),
            _decode_state_object(str(row["config_json"]), "temporary Session config"),
        )

    def temporary_binding_by_participant(
        self, *, owner_name: str, group_id: str, participant_id: str
    ) -> TemporarySessionBinding | None:
        result = self._store.temporary_binding_by_participant(
            owner_name=owner_name, group_id=group_id, participant_id=participant_id
        )
        if result is None:
            return None
        address, row = result
        return TemporarySessionBinding(
            address,
            str(row["generation_id"]),
            str(row["owner_name"]),
            str(row["group_id"]),
            str(row["participant_id"]),
            _decode_state_object(str(row["config_json"]), "temporary Session config"),
        )

    async def delete_temporary_group(self, *, owner_name: str, group_id: str) -> int:
        """Delete bound participant Sessions after their owner has drained execution."""
        return await _run_session_io(
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
        rows = await _run_session_io(
            lambda: self._store.temporary_bindings(
                owner_name=owner_name,
                group_id=group_id,
                after=after,
                limit=limit,
            )
        )
        return [
            TemporarySessionBinding(
                address,
                str(row["generation_id"]),
                str(row["owner_name"]),
                str(row["group_id"]),
                str(row["participant_id"]),
                _decode_state_object(str(row["config_json"]), "temporary Session config"),
            )
            for address, row in rows
        ]

    async def append_messages_with_receipts_async(
        self,
        address: SessionAddress,
        *,
        generation_id: str,
        owner_name: str,
        messages: Sequence[ChatMessage],
        receipts: Sequence[tuple[int, str, str, str, str]],
        deduplicate_carrier: bool = False,
    ) -> None:
        await _run_session_io(
            lambda: self.append_messages_with_receipts(
                address,
                generation_id=generation_id,
                owner_name=owner_name,
                messages=messages,
                receipts=receipts,
                deduplicate_carrier=deduplicate_carrier,
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
    ) -> None:
        self._store.append_messages_with_receipts(
            address,
            generation_id=generation_id,
            owner_name=owner_name,
            messages=messages,
            receipts=receipts,
            deduplicate_carrier=deduplicate_carrier,
        )

    async def lookup_delivery_receipt(
        self,
        address: SessionAddress,
        generation_id: str,
        owner_name: str,
        receipt_id: str,
    ) -> DeliveryReceipt | None:
        row = await _run_session_io(
            lambda: self._store.delivery_receipt(
                address,
                generation_id=generation_id,
                owner_name=owner_name,
                receipt_id=receipt_id,
            )
        )
        if row is None:
            return None
        return DeliveryReceipt(
            str(row["receipt_id"]),
            str(row["content_hash"]),
            str(row["effect_kind"]),
            {"kind": str(row["carrier_kind"]), "sequence": int(row["carrier_sequence"])},
        )

    async def record_run_owner_async(
        self,
        address: SessionAddress,
        *,
        run_id: str,
        owner: RunExecutionOwner,
        input_id: str | None = None,
    ) -> None:
        await _run_session_io(
            lambda: self._store.record_run_owner(
                address, run_id=run_id, owner=owner, input_id=input_id
            )
        )

    async def record_run_start_async(self, address: SessionAddress, *, run_id: str) -> None:
        """Persist one normal Run admission boundary before it appends output."""
        await _run_session_io(lambda: self._store.record_run_start(address, run_id=run_id))

    def owned_runs(
        self,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> builtins.list[OwnedRunRecord]:
        rows = self._store.owned_runs(
            owner_name=owner_name,
            group_id=group_id,
            participant_id=participant_id,
            after=after,
            limit=limit,
        )
        return [
            OwnedRunRecord(
                record_key=int(row["record_key"]),
                address=SessionAddress(
                    row["project_id"] or None, row["agent_id"], row["session_id"]
                ),
                generation_id=str(row["generation_id"]),
                run_id=str(row["run_id"]),
                owner=RunExecutionOwner(
                    str(row["owner_name"]),
                    str(row["group_id"]),
                    str(row["participant_id"]),
                    str(row["participant_generation_id"]),
                    str(row["epoch"]),
                ),
                start_sequence=int(row["start_sequence"]),
                terminal_status=row["terminal_status"],
                terminal_sequence=row["terminal_sequence"],
                input_id=row["input_id"],
            )
            for row in rows
        ]

    async def owned_runs_async(
        self,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> builtins.list[OwnedRunRecord]:
        return await _run_session_io(
            lambda: self.owned_runs(
                owner_name=owner_name,
                group_id=group_id,
                participant_id=participant_id,
                after=after,
                limit=limit,
            )
        )

    def run_start_boundaries(
        self, addresses: Sequence[SessionAddress]
    ) -> builtins.list[RunStartBoundary]:
        rows = self._store.run_start_boundaries(addresses)
        return [
            RunStartBoundary(
                SessionAddress(row["project_id"] or None, row["agent_id"], row["session_id"]),
                str(row["generation_id"]),
                str(row["run_id"]),
                int(row["start_sequence"]),
            )
            for row in rows
        ]

    def history_revision(self, address: SessionAddress) -> int:
        return int(self._store.state(address)["history_revision"])

    def retarget_identity_agent_references(
        self, old_agent_id: str, new_agent_id: str
    ) -> tuple[SessionIdentityReferenceUpdate, ...]:
        updates: builtins.list[SessionIdentityReferenceUpdate] = []
        for address in self._store.list_addresses(include_all_scopes=True):
            changed = False

            def retarget(metadata: JsonObject) -> None:
                nonlocal changed
                parent = metadata.get("subagent_parent")
                if (
                    not isinstance(parent, dict)
                    or parent.get("project_id") is not None
                    or parent.get("agent_id") != old_agent_id
                ):
                    return
                parent = dict(parent)
                parent["agent_id"] = new_agent_id
                metadata["subagent_parent"] = parent
                changed = True

            previous, _updated = self.mutate_metadata_with_previous(address, retarget)
            if changed:
                updates.append(SessionIdentityReferenceUpdate(address, previous))
        return tuple(updates)

    def restore_identity_agent_references(
        self, updates: tuple[SessionIdentityReferenceUpdate, ...]
    ) -> None:
        for update in reversed(updates):
            self.set_metadata(update.address, update.previous_metadata)

    async def move(
        self,
        source: SessionAddress,
        target: SessionAddress,
        *,
        strip_meta_keys: frozenset[str] = frozenset(),
    ) -> ChatSession:
        _validate_session_id(source.session_id)
        _validate_session_id(target.session_id)
        async with self.write_lock(source):
            return await _run_session_io(self._move, source, target, strip_meta_keys)

    def _move(
        self, source: SessionAddress, target: SessionAddress, strip_meta_keys: frozenset[str]
    ) -> ChatSession:
        def prepare_metadata(metadata: JsonObject, _message_count: int) -> None:
            for key in strip_meta_keys:
                metadata.pop(key, None)

        self._store.move(source, target, prepare_metadata)
        return ChatSession(self._store, target)

    async def fork(
        self,
        source: SessionAddress,
        *,
        target_agent_id: str | None = None,
        target_project_id: str | None = None,
        strip_meta_keys: frozenset[str] = frozenset(),
    ) -> ChatSession:
        _validate_session_id(source.session_id)
        async with self.write_lock(source):
            return await _run_session_io(
                self._fork,
                source,
                target_agent_id or source.agent_id,
                target_project_id,
                strip_meta_keys,
                target_agent_id is not None,
            )

    def _fork(
        self,
        source: SessionAddress,
        target_agent_id: str,
        target_project_id: str | None,
        strip_meta_keys: frozenset[str],
        target_explicit: bool,
    ) -> ChatSession:
        _validate_agent_id(target_agent_id)
        same_scope = target_agent_id == source.agent_id and target_project_id == source.project_id
        target = SessionAddress(target_project_id, target_agent_id, "")
        cross_scope_affinity_id = None if same_scope else _new_prompt_cache_affinity_id()
        forked_at = _format_timestamp(datetime.now(UTC))

        def prepare_metadata(metadata: JsonObject, message_count: int) -> None:
            affinity_id = metadata.get(PROMPT_CACHE_AFFINITY_META_KEY)
            for key in strip_meta_keys:
                metadata.pop(key, None)
            if same_scope:
                if affinity_id is None:
                    affinity_id = _default_prompt_cache_affinity_id(source)
                elif not _is_prompt_cache_affinity_id(affinity_id):
                    raise ChatSessionError(
                        f"invalid prompt cache affinity id for session: {source.session_id}"
                    )
            else:
                affinity_id = cross_scope_affinity_id
            metadata[PROMPT_CACHE_AFFINITY_META_KEY] = affinity_id
            metadata[FORK_SOURCE_META_KEY] = {
                "agent_id": source.agent_id,
                "session_id": source.session_id,
                "project_id": source.project_id,
                "forked_at": forked_at,
                "message_count": message_count,
            }

        target = self._store.fork(
            source,
            target,
            prepare_metadata,
            generate_id=True,
            allow_owner_managed_source=target_explicit and not same_scope,
        )
        return ChatSession(self._store, target)

    def delete(self, address: SessionAddress) -> None:
        self._store.delete(address)

    async def archive(self, address: SessionAddress) -> None:
        async with self.write_lock(address):
            await _run_session_io(self._store.archive, address)

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

    def fts_search(
        self,
        query: str,
        *,
        project_id: str | None,
        agent_id: str | None,
        session_id: str | None = None,
        match_mode: str = "all_terms",
        limit: int = 1_000,
        roles: Sequence[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        excluded_session_ids: Sequence[str] = (),
    ) -> builtins.list[tuple[SessionAddress, str, str, str, float]]:
        return self._store.fts_search(
            query,
            project_id=project_id,
            agent_id=agent_id,
            session_id=session_id,
            match_mode=match_mode,
            limit=limit,
            roles=roles,
            since=since,
            until=until,
            excluded_session_ids=excluded_session_ids,
        )

    @staticmethod
    def _notify_callbacks(
        callbacks: Sequence[Callable[[SessionAddress], None]], address: SessionAddress
    ) -> None:
        for callback in list(callbacks):
            try:
                callback(address)
            except Exception:
                logging.getLogger(__name__).exception("Session callback failed")
