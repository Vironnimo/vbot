"""Canonical SQLite lifecycle, transaction admission and recovery policy."""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from core.sessions import (
    _store_continuation,
    _store_fts,
    _store_history,
    _store_mutations,
    _store_owned,
    _store_queries,
    _store_schema,
    _store_search,
    _store_values,
)
from core.sessions._types import JsonObject, SessionChatHistorySnapshot
from core.sessions.errors import (
    FtsHealth,
    SessionNotFoundError,
    SessionStorageFormatError,
    SessionStoreCorruptError,
    SessionStoreHealth,
    SessionStoreSchemaMismatchError,
    SessionStoreUnavailableError,
)
from core.sessions.format import (
    MARKER_STATE_BOOTSTRAP,
    publish_ready_marker,
    read_session_store_marker,
    validate_session_store_paths,
)
from core.sessions.schema import (
    SCHEMA_VERSION,
)
from core.sessions.sqlite_runtime import (
    ACTIVITY_WRITE_PATIENCE_S,
    TRANSCRIPT_WRITE_PATIENCE_S,
    WRITE_PATIENCE_S,
    SQLiteRuntime,
    classify_write_error,
)

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.runs import RunExecutionOwner
    from core.sessions._types import (
        SessionAddress,
        SessionContinuationState,
        SessionIdentityReferenceUpdate,
        SessionReadBatch,
        SessionReadCursor,
        SessionRunCompletion,
    )


_WriteResult = TypeVar("_WriteResult")
_Decoded = TypeVar("_Decoded")
# Records selected inside a write transaction and decoded after commit.
_HistoryDelta = tuple[list[sqlite3.Row], "SessionReadCursor"] | None


class SessionStore:
    """One canonical SQLite database with explicit read/write snapshots."""

    def __init__(self, path: Path, *, _offline: bool = False) -> None:
        self.path = Path(path)
        self._runtime = SQLiteRuntime(self.path)
        self._offline = _offline
        try:
            self._writer = self._open_runtime(offline=_offline)
        except BaseException:
            self._runtime.close()
            raise

    def _open_runtime(self, *, offline: bool) -> sqlite3.Connection:
        if offline:
            database_id = uuid.uuid4().hex if not self.path.exists() else None
            writer = self._runtime.open_writer(
                create=not self.path.exists(), database_id=database_id
            )
            self._reconcile_open_database(writer, expected_database_id=None)
            return writer

        validate_session_store_paths(self.path.parent, self.path)
        marker = read_session_store_marker(self.path.parent)
        if marker is None:
            raise SessionStorageFormatError(
                f"the data directory does not authorize a current-format Session store: "
                f"{self.path.parent}; initialize the data directory or install a converted "
                "Session database first"
            )
        if int(marker["schema_version"]) != SCHEMA_VERSION:
            raise SessionStoreSchemaMismatchError(
                "Session store marker schema does not match the Runtime: "
                f"schema version {marker['schema_version']}"
            )
        database_id = str(marker["database_id"])
        if marker["state"] == MARKER_STATE_BOOTSTRAP:
            writer = self._runtime.open_writer(create=True, database_id=database_id)
            self._reconcile_open_database(writer, expected_database_id=database_id)
            publish_ready_marker(self.path.parent, database_id)
            return writer
        from core.sessions.recovery import auto_restore_if_needed, read_recovery_incident

        pending_incident = read_recovery_incident(self.path.parent)
        if pending_incident and pending_incident.get("verification") == "pending":
            auto_restore_if_needed(self.path.parent, self.path)
        if not self.path.exists() and not auto_restore_if_needed(self.path.parent, self.path):
            raise SessionStoreUnavailableError(
                f"the Session database is missing although the store is ready: {self.path}"
            )
        try:
            writer = self._runtime.open_writer(expected_database_id=database_id)
            self._reconcile_open_database(writer, expected_database_id=database_id)
            return writer
        except (SessionStoreCorruptError, sqlite3.DatabaseError, OSError):
            self._runtime.close()
            if auto_restore_if_needed(self.path.parent, self.path):
                self._runtime = SQLiteRuntime(self.path)
                writer = self._runtime.open_writer(expected_database_id=database_id)
                self._reconcile_open_database(writer, expected_database_id=database_id)
                return writer
            raise

    def _reconcile_open_database(
        self, connection: sqlite3.Connection, *, expected_database_id: str | None
    ) -> None:
        _store_schema._reconcile_open_database(
            connection, self.path, expected_database_id=expected_database_id
        )

    def _read_decoded(
        self, select: Callable[[sqlite3.Connection], Callable[[], _Decoded]]
    ) -> _Decoded:
        """Select rows in one read transaction, then decode them after it ends.

        A rollback-journal store serves reads under its runtime lock, so Message
        reconstruction outside the transaction never delays a writer.
        """
        with self._runtime.read_ctx() as connection:
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
                return cast(_WriteResult, self._runtime.execute_write(func, patience_s=patience_s))
            except sqlite3.Error as exc:
                message = str(exc).lower()
                if not fts_retried and any(
                    marker in message for marker in ("fts", "messages_fts", "message_search")
                ):
                    fts_retried = True
                    with suppress(Exception):
                        self._runtime.execute_write(_store_fts._detach_fts, patience_s=patience_s)
                    continue
                classification = classify_write_error(exc)
                if classification == "corrupt":
                    raise SessionStoreCorruptError(
                        f"Session database write found corruption: {self.path}"
                    ) from exc
                if classification == "unavailable":
                    raise SessionStoreUnavailableError(
                        f"Session database write failed: {self.path}"
                    ) from exc
                raise

    def close(self) -> None:
        self._runtime.close()

    def checkpoint(self) -> None:
        self._runtime.checkpoint()

    def backup(
        self,
        destination: Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        return self._runtime.backup(destination, cancel_event=cancel_event)

    def verify_read_write(self) -> None:
        """Exercise the opened Runtime's read/write path without changing canonical rows."""

        def verify(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TEMP TABLE session_store_verify(value INTEGER NOT NULL)")
            try:
                connection.execute("INSERT INTO session_store_verify(value) VALUES (1)")
                row = connection.execute("SELECT value FROM session_store_verify").fetchone()
                if row is None or int(row[0]) != 1:
                    raise SessionStoreUnavailableError(
                        "Session database read/write verification failed"
                    )
            finally:
                connection.execute("DROP TABLE IF EXISTS session_store_verify")

        self._execute_write(verify)

    def status_projection(self) -> JsonObject:
        """Return operator-safe health, snapshot, and incident state."""
        from core.sessions.recovery import read_recovery_incident
        from core.sessions.snapshots import read_snapshot_health, snapshot_inventory

        marker = read_session_store_marker(self.path.parent)
        if marker is None:
            raise SessionStorageFormatError("current-format Session marker is missing")
        fts = self.fts_health()
        incident = read_recovery_incident(self.path.parent)
        snapshots = snapshot_inventory(
            self.path.parent, expected_database_id=str(marker["database_id"])
        )
        snapshot_health = read_snapshot_health(self.path.parent)
        active_incident = incident if incident and not incident.get("acknowledged", False) else None
        if active_incident:
            health = SessionStoreHealth("recovered_with_incident")
        elif not fts.available:
            health = SessionStoreHealth("search_degraded", fts.reason)
        elif not snapshots or snapshot_health.get("state") != "healthy":
            health = SessionStoreHealth(
                "snapshot_degraded",
                str(snapshot_health.get("reason") or "no verified Session snapshot is available"),
            )
        else:
            health = SessionStoreHealth("healthy")
        return {
            "state": health.state,
            "reason": health.reason,
            "database_id": str(marker["database_id"]),
            "marker_state": marker["state"],
            "schema_version": int(marker["schema_version"]),
            "fts": {
                "state": fts.state,
                "reason": fts.reason,
                "generation": fts.generation,
                "target_high_water": fts.target_high_water,
                "completed_high_water": fts.completed_high_water,
            },
            "snapshots": snapshots,
            "snapshot_health": snapshot_health,
            "incident": active_incident,
        }

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
        with self._runtime.read_ctx() as connection:
            if _store_values._find_live(connection, address) is not None:
                return
        self._execute_write(lambda connection: _store_mutations.ensure_live(connection, address))

    def exists(self, address: SessionAddress, *, include_archived: bool = False) -> bool:
        with self._runtime.read_ctx() as connection:
            return _store_queries.exists(connection, address, include_archived=include_archived)

    def state(self, address: SessionAddress, *, include_archived: bool = False) -> sqlite3.Row:
        with self._runtime.read_ctx() as connection:
            return _store_queries.state(connection, address, include_archived=include_archived)

    def metadata(self, address: SessionAddress) -> JsonObject:
        return _store_values._session_metadata_from_state(self.state(address))

    def metadata_value(self, address: SessionAddress, key: str) -> Any:
        """Return one metadata value, or ``None`` when the Session has none."""
        with self._runtime.read_ctx() as connection:
            return _store_queries.metadata_value(connection, address, key)

    def descriptor_source(
        self, address: SessionAddress
    ) -> tuple[JsonObject, int, ChatMessage | None]:
        """Load compact descriptor inputs without reconstructing Session history."""
        source = self.descriptor_sources((address,)).get(address)
        if source is None:
            raise SessionNotFoundError(f"session does not exist: {address.session_id}")
        return source

    def descriptor_sources(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[JsonObject, int, ChatMessage | None]]:
        return self._read_decoded(
            lambda connection: _store_queries.descriptor_sources(connection, addresses)
        )

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
        """Apply a metadata mutation, entering the writer only for a real change.

        The mutation first runs against a read snapshot. A live Session whose
        persisted metadata it leaves unchanged returns without a write
        transaction; otherwise the writer creates the Session when allowed and
        reapplies the mutation to the latest row. The mutation may therefore
        run twice and must be deterministic and free of side effects.
        """
        with self._runtime.read_ctx() as connection:
            state = _store_values._find_live(connection, address)
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

    def activity(self, address: SessionAddress) -> JsonObject:
        payload = self.state(address)["activity_json"]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SessionStoreCorruptError(
                f"invalid Session activity: {address.session_id}"
            ) from exc
        return data if isinstance(data, dict) else {}

    def replace_activity(self, address: SessionAddress, activity: JsonObject) -> None:
        return self._execute_write(
            lambda connection: _store_mutations.replace_activity(connection, address, activity),
            patience_s=ACTIVITY_WRITE_PATIENCE_S,
        )

    def mutate_activity(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        return self._execute_write(
            lambda connection: _store_mutations.mutate_activity(connection, address, mutation),
            patience_s=ACTIVITY_WRITE_PATIENCE_S,
        )

    def append_messages(
        self,
        address: SessionAddress,
        messages: Sequence[ChatMessage],
        *,
        run_id: str | None = None,
        assistant_message_id: str | None = None,
        continuation_records: Sequence[JsonObject] = (),
        since: SessionReadCursor | None = None,
        metadata_mutation: Callable[[JsonObject], None] | None = None,
        require_current: bool = False,
    ) -> SessionReadBatch | None:
        """Append Messages plus any Continuation records in one transaction.

        With *since*, the same transaction also selects every record after that
        cursor (this append and any concurrent writer's), so the caller needs no
        follow-up read. ``None`` then means the cursor cannot be continued.

        *metadata_mutation* changes Session metadata in the same transaction, so
        the Messages and the metadata they depend on commit or roll back together.
        With *require_current*, the transaction writes nothing and returns
        ``None`` unless *since* still names the Session's newest record.
        """
        if require_current and since is None:
            raise ValueError("require_current needs the cursor to verify")

        def _fn(connection: sqlite3.Connection) -> _HistoryDelta:
            if (
                require_current
                and since is not None
                and not _store_history.cursor_is_current(connection, address, since)
            ):
                return None
            _store_mutations.append_messages(
                connection,
                address,
                messages,
                run_id=run_id,
                assistant_message_id=assistant_message_id,
            )
            if metadata_mutation is not None:
                _store_mutations.mutate_metadata(connection, address, metadata_mutation)
            return self._journal_and_select(connection, address, continuation_records, since)

        delta = self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)
        return None if delta is None else _store_history.read_batch(delta)

    @staticmethod
    def _journal_and_select(
        connection: sqlite3.Connection,
        address: SessionAddress,
        continuation_records: Sequence[JsonObject],
        since: SessionReadCursor | None,
    ) -> _HistoryDelta:
        _store_continuation.append_continuation(connection, address, continuation_records)
        if since is None:
            return None
        return _store_history.message_rows_since(connection, address, since)

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

    def temporary_binding(self, address: SessionAddress) -> sqlite3.Row | None:
        with self._runtime.read_ctx() as connection:
            return _store_owned.temporary_binding(connection, address)

    def temporary_binding_by_participant(
        self, *, owner_name: str, group_id: str, participant_id: str
    ) -> tuple[SessionAddress, sqlite3.Row] | None:
        with self._runtime.read_ctx() as connection:
            return _store_owned.temporary_binding_by_participant(
                connection, owner_name=owner_name, group_id=group_id, participant_id=participant_id
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
    ) -> list[tuple[SessionAddress, sqlite3.Row]]:
        with self._runtime.read_ctx() as connection:
            return _store_owned.temporary_bindings(
                connection, owner_name=owner_name, group_id=group_id, after=after, limit=limit
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
        with self._runtime.read_ctx() as connection:
            return _store_owned.temporary_group_titles(
                connection, owner_name=owner_name, group_ids=group_ids
            )

    def owned_session_summary_rows(
        self,
        *,
        owner_name: str | None = None,
        group_id: str | None = None,
        metadata_keys: Sequence[str] = (),
    ) -> list[sqlite3.Row]:
        with self._runtime.read_ctx() as connection:
            return _store_owned.owned_session_summary_rows(
                connection, owner_name=owner_name, group_id=group_id, metadata_keys=metadata_keys
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
        continuation_records: Sequence[JsonObject] = (),
        since: SessionReadCursor | None = None,
    ) -> SessionReadBatch | None:
        """Receipt-carrying variant of :meth:`append_messages` with the same options."""

        def _fn(connection: sqlite3.Connection) -> _HistoryDelta:
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
            )
            return self._journal_and_select(connection, address, continuation_records, since)

        delta = self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)
        return None if delta is None else _store_history.read_batch(delta)

    def delivery_receipt(
        self, address: SessionAddress, *, generation_id: str, owner_name: str, receipt_id: str
    ) -> sqlite3.Row | None:
        with self._runtime.read_ctx() as connection:
            return _store_owned.delivery_receipt(
                connection,
                address,
                generation_id=generation_id,
                owner_name=owner_name,
                receipt_id=receipt_id,
            )

    def record_run_owner(
        self,
        address: SessionAddress,
        *,
        run_id: str,
        owner: RunExecutionOwner,
        input_id: str | None = None,
    ) -> None:
        return self._execute_write(
            lambda connection: _store_owned.record_run_owner(
                connection, address, run_id=run_id, owner=owner, input_id=input_id
            )
        )

    def start_run(
        self,
        address: SessionAddress,
        *,
        run_id: str,
        work_id: str | None,
        run_kind: str,
        contributes_to_activity: bool,
        started_at: str,
    ) -> None:
        from core.sessions import _store_runs

        self._execute_write(
            lambda connection: _store_runs.start_run(
                connection,
                address,
                run_id=run_id,
                work_id=work_id,
                run_kind=run_kind,
                contributes_to_activity=contributes_to_activity,
                started_at=started_at,
            )
        )

    def finish_run(self, address: SessionAddress, completion: SessionRunCompletion) -> JsonObject:
        from core.sessions import _store_runs

        return self._execute_write(
            lambda connection: _store_runs.finish_run(connection, address, completion)
        )

    def recover_interrupted_runs(self) -> None:
        from core.sessions import _store_runs

        self._execute_write(_store_runs.recover_interrupted_runs)

    def record_run_kind(self, address: SessionAddress, run_kind: str) -> None:
        from core.sessions import _store_runs

        self._execute_write(
            lambda connection: _store_runs.record_run_kind(connection, address, run_kind)
        )

    def record_run_start(self, address: SessionAddress, *, run_id: str) -> None:
        return self._execute_write(
            lambda connection: _store_owned.record_run_start(connection, address, run_id=run_id)
        )

    def owned_runs(
        self,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        with self._runtime.read_ctx() as connection:
            return _store_owned.owned_runs(
                connection,
                owner_name=owner_name,
                group_id=group_id,
                participant_id=participant_id,
                after=after,
                limit=limit,
            )

    def owned_runs_by_id(
        self, *, owner_name: str, group_id: str, run_ids: Sequence[str]
    ) -> list[sqlite3.Row]:
        unique = list(dict.fromkeys(run_ids))
        limit = _store_owned.OWNED_RUN_LOOKUP_LIMIT
        rows: list[sqlite3.Row] = []
        with self._runtime.read_ctx() as connection:
            for start in range(0, len(unique), limit):
                rows.extend(
                    _store_owned.owned_runs_by_id(
                        connection,
                        owner_name=owner_name,
                        group_id=group_id,
                        run_ids=unique[start : start + limit],
                    )
                )
        return rows

    def owned_run_by_input(self, address: SessionAddress, input_id: str) -> sqlite3.Row | None:
        with self._runtime.read_ctx() as connection:
            return _store_owned.owned_run_by_input(connection, address, input_id)

    def run_start_boundaries(self, addresses: Sequence[SessionAddress]) -> list[sqlite3.Row]:
        with self._runtime.read_ctx() as connection:
            return _store_owned.run_start_boundaries(connection, addresses)

    def messages(self, address: SessionAddress) -> list[ChatMessage]:
        return self._read_decoded(lambda connection: _store_history.messages(connection, address))

    def active_messages(self, address: SessionAddress) -> list[ChatMessage]:
        return self._read_decoded(
            lambda connection: _store_history.active_messages(connection, address)
        )

    def active_user_message_count(self, address: SessionAddress, *, limit: int) -> int:
        with self._runtime.read_ctx() as connection:
            return _store_history.active_user_message_count(connection, address, limit=limit)

    def tool_result_persisted(self, address: SessionAddress, tool_call_id: str) -> bool:
        with self._runtime.read_ctx() as connection:
            return _store_history.tool_result_persisted(connection, address, tool_call_id)

    def latest_note(self, address: SessionAddress, *, content_prefix: str) -> ChatMessage | None:
        return self._read_decoded(
            lambda connection: _store_history.latest_note(
                connection, address, content_prefix=content_prefix
            )
        )

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
        background_roles: Sequence[str],
        background_tool_names: Sequence[str],
        after: tuple[str, int] | None = None,
    ) -> SessionChatHistorySnapshot:
        return self._read_decoded(
            lambda connection: _store_history.chat_history_snapshot(
                connection,
                address,
                limit=limit,
                before_message_id=before_message_id,
                before_sequence=before_sequence,
                expected_generation_id=expected_generation_id,
                excluded_roles=excluded_roles,
                complete_run_segment=complete_run_segment,
                background_roles=background_roles,
                background_tool_names=background_tool_names,
                after=after,
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
        with self._runtime.read_ctx() as connection:
            return _store_history.history_snapshot(
                connection, address, snapshot_sequence=snapshot_sequence
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
        with self._runtime.read_ctx() as connection:
            return _store_history.history_section_stats(
                connection,
                address,
                expected_generation_id=expected_generation_id,
                snapshot_sequence=snapshot_sequence,
                sections=sections,
                excluded_tool_name=excluded_tool_name,
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
        with self._runtime.read_ctx() as connection:
            return _store_history.reflection_runs(connection, address)

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

    def messages_since(
        self, address: SessionAddress, cursor: SessionReadCursor | None
    ) -> SessionReadBatch | None:
        with self._runtime.read_ctx() as connection:
            delta = _store_history.message_rows_since(connection, address, cursor)
        return None if delta is None else _store_history.read_batch(delta)

    def continuation(self, address: SessionAddress) -> SessionContinuationState | None:
        with self._runtime.read_ctx() as connection:
            return _store_continuation.continuation(connection, address)

    def append_continuation(self, address: SessionAddress, records: Sequence[JsonObject]) -> None:
        return self._execute_write(
            lambda connection: _store_continuation.append_continuation(connection, address, records)
        )

    def clear_continuation(self, address: SessionAddress) -> None:
        return self._execute_write(
            lambda connection: _store_continuation.clear_continuation(connection, address)
        )

    def bookend_timestamps(self, address: SessionAddress) -> tuple[str, str] | None:
        with self._runtime.read_ctx() as connection:
            return _store_history.bookend_timestamps(connection, address)

    def current_skill_activation_messages(self, address: SessionAddress) -> list[ChatMessage]:
        return self._read_decoded(
            lambda connection: _store_history.current_skill_activation_messages(connection, address)
        )

    def list_addresses(
        self,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        include_all_scopes: bool = False,
        exclude_owner_managed: bool = False,
    ) -> list[SessionAddress]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.list_addresses(
                connection,
                project_id=project_id,
                agent_id=agent_id,
                include_all_scopes=include_all_scopes,
                exclude_owner_managed=exclude_owner_managed,
            )

    def list_state_rows(self, project_id: str | None, agent_id: str) -> list[sqlite3.Row]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.list_state_rows(connection, project_id, agent_id)

    def list_summary_rows_for_scope(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        metadata_keys: Sequence[str] = (),
    ) -> list[sqlite3.Row]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.list_summary_rows_for_scope(
                connection, project_id, agent_id, metadata_keys=metadata_keys
            )

    @staticmethod
    def metadata_from_state(state: Any) -> JsonObject:
        return _store_values._session_metadata_from_state(state)

    def list_summary_rows(
        self,
        scopes: Sequence[tuple[str | None, str]],
        *,
        limit: int,
        cursor: tuple[float, str, str, str] | None,
        include_subagents: bool,
        include_memory_reflections: bool,
        include_skill_reflections: bool,
        include_cron: bool,
        include_channels: bool,
        required_address: SessionAddress | None,
    ) -> tuple[list[sqlite3.Row], sqlite3.Row | None, int, bool]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.list_summary_rows(
                connection,
                scopes,
                limit=limit,
                cursor=cursor,
                include_subagents=include_subagents,
                include_memory_reflections=include_memory_reflections,
                include_skill_reflections=include_skill_reflections,
                include_cron=include_cron,
                include_channels=include_channels,
                required_address=required_address,
            )

    def list_activity_rows(self, project_id: str | None, agent_id: str) -> list[sqlite3.Row]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.list_activity_rows(connection, project_id, agent_id)

    def session_ids_with_messages(
        self,
        project_id: str | None,
        agent_id: str,
        roles: Sequence[str],
        since: datetime | None,
        until: datetime | None,
    ) -> set[str]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.session_ids_with_messages(
                connection, project_id, agent_id, roles, since, until
            )

    def list_history_revisions(
        self, project_id: str | None, agent_id: str
    ) -> list[tuple[SessionAddress, str, int]]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.list_history_revisions(connection, project_id, agent_id)

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.list_history_versions(connection, addresses)

    def fts_health(self) -> FtsHealth:
        """Return operator-facing FTS state with explicit canonical coverage checks."""
        try:
            with self._runtime.read_ctx() as connection:
                return _store_fts._fts_health_from_connection(connection, verify_coverage=True)
        except Exception as exc:
            return FtsHealth(state="unavailable", reason=f"FTS health check failed: {exc}")

    def is_fts_available(self) -> bool:
        """Return cheap marker-backed availability; rebuild verification owns coverage scans."""
        try:
            with self._runtime.read_ctx() as connection:
                return _store_fts._fts_health_from_connection(
                    connection, verify_coverage=False
                ).available
        except Exception:
            return False

    def recall_context(self, address: SessionAddress, message_id: str) -> builtins.list[JsonObject]:
        """Return bounded conversation text beside a search anchor."""
        with self._runtime.read_ctx() as connection:
            return _store_history.recall_context(connection, address, message_id)

    def fts_search(
        self,
        query: str,
        *,
        project_id: str | None,
        agent_id: str | None,
        session_id: str | None = None,
        match_mode: str = "all_terms",
        limit: int = _store_values._SEARCH_RESULT_LIMIT,
        roles: Sequence[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        excluded_session_ids: Sequence[str] = (),
    ) -> builtins.list[tuple[SessionAddress, str, str, str, float]]:
        def select(
            **fallback: Any,
        ) -> Callable[[], builtins.list[tuple[SessionAddress, str, str, str, float]]]:
            with self._runtime.read_ctx() as connection:
                return _store_search.search(
                    connection,
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
                    **fallback,
                )

        try:
            decode = select()
        except sqlite3.Error as exc:
            if "fts" in str(exc).lower() or "messages_fts" in str(exc).lower():
                error_message = str(exc)
                with suppress(Exception):
                    self._execute_write(
                        lambda connection: _store_fts._detach_fts(connection, error_message)
                    )
            decode = select(use_fts=False, fallback_reason="fts_error")
        return decode()

    def archive(self, address: SessionAddress) -> None:
        return self._execute_write(lambda connection: _store_mutations.archive(connection, address))

    def move(
        self,
        source: SessionAddress,
        target: SessionAddress,
        prepare_metadata: Callable[[JsonObject, int], None],
    ) -> None:
        return self._execute_write(
            lambda connection: _store_mutations.move(connection, source, target, prepare_metadata)
        )

    def fork(
        self,
        source: SessionAddress,
        target: SessionAddress,
        prepare_metadata: Callable[[JsonObject, int], None],
        *,
        generate_id: bool = False,
        allow_owner_managed_source: bool = False,
    ) -> SessionAddress:
        return self._execute_write(
            lambda connection: _store_mutations.fork(
                connection,
                source,
                target,
                prepare_metadata,
                generate_id=generate_id,
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
