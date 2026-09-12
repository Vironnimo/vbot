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

from core.chat.errors import ChatSessionError
from core.sessions import (
    _store_continuation,
    _store_fts,
    _store_history,
    _store_import,
    _store_mutations,
    _store_owned,
    _store_queries,
    _store_schema,
    _store_search,
    _store_values,
)
from core.sessions._types import JsonObject
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
    FTS_COMPLETED_HIGH_WATER_KEY,
    FTS_DEGRADED_REASON_KEY,
    FTS_GENERATION_KEY,
    FTS_SQL,
    FTS_SQL_FALLBACK,
    FTS_STALE_KEY,
    FTS_STORAGE_VERSION,
    FTS_STORAGE_VERSION_KEY,
    FTS_TARGET_HIGH_WATER_KEY,
    FTS_TRIGRAM_TABLE,
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
    from core.sessions._types import SessionAddress, SessionReadCursor


_WriteResult = TypeVar("_WriteResult")


class SessionStore:
    """One canonical SQLite database with explicit read/write snapshots."""

    def __init__(self, path: Path, *, _offline: bool = False) -> None:
        self.path = Path(path)
        self._runtime = SQLiteRuntime(self.path)
        self._offline = _offline
        self._offline_bulk_import = False
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

    def prepare_offline_bulk_import(self) -> None:
        """Open one disposable canonical-data transaction without maintaining FTS."""
        if not self._offline:
            raise RuntimeError("bulk import mode is available only to the offline converter")
        if self._offline_bulk_import:
            raise RuntimeError("bulk import mode is already active")
        mode = str(self._writer.execute("PRAGMA journal_mode=MEMORY").fetchone()[0]).lower()
        if mode != "memory":
            raise SessionStoreUnavailableError("staged Session database rejected MEMORY journal")
        self._writer.execute("PRAGMA synchronous=OFF")
        self._writer.execute("PRAGMA temp_store=MEMORY")
        self._writer.execute(f"PRAGMA cache_size=-{_store_values._OFFLINE_IMPORT_CACHE_KIB}")
        _store_fts._drop_fts(self._writer)
        _store_fts._set_fts_meta(self._writer, FTS_STALE_KEY, "offline-import")
        self._writer.execute("BEGIN IMMEDIATE")
        self._offline_bulk_import = True

    def finish_offline_bulk_import(self) -> None:
        """Commit canonical rows and build both disposable FTS indexes in SQLite."""
        if not self._offline or not self._offline_bulk_import:
            raise RuntimeError("bulk import mode is not active")
        self._writer.execute("COMMIT")
        self._offline_bulk_import = False
        try:
            self._writer.executescript(FTS_SQL)
        except sqlite3.Error:
            _store_fts._drop_fts(self._writer)
            self._writer.executescript(FTS_SQL_FALLBACK)
        target = int(
            self._writer.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[0]
        )
        _store_fts._set_fts_meta(self._writer, FTS_STORAGE_VERSION_KEY, str(FTS_STORAGE_VERSION))
        _store_fts._set_fts_meta(self._writer, FTS_GENERATION_KEY, uuid.uuid4().hex)
        _store_fts._set_fts_meta(self._writer, FTS_TARGET_HIGH_WATER_KEY, str(target))
        _store_fts._set_fts_meta(self._writer, FTS_COMPLETED_HIGH_WATER_KEY, "0")
        _store_fts._set_fts_meta(self._writer, FTS_STALE_KEY, "rebuilding")
        _store_fts._set_fts_meta(self._writer, FTS_DEGRADED_REASON_KEY, "FTS rebuild in progress")
        self._writer.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        if _store_fts._fts_table_exists(self._writer, FTS_TRIGRAM_TABLE):
            self._writer.execute(
                "INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')"
            )
        coverage_ok, coverage_reason = _store_fts._fts_coverage_ok(self._writer)
        if not coverage_ok:
            raise SessionStoreCorruptError(
                coverage_reason or "offline FTS rebuild did not cover canonical Messages"
            )
        _store_fts._set_fts_meta(self._writer, FTS_COMPLETED_HIGH_WATER_KEY, str(target))
        _store_fts._set_fts_meta(self._writer, FTS_DEGRADED_REASON_KEY, "")
        self._writer.execute("DELETE FROM store_meta WHERE key = ?", (FTS_STALE_KEY,))

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

    def import_generation(
        self,
        address: SessionAddress,
        *,
        generation_id: str,
        messages: Sequence[ChatMessage],
        metadata: JsonObject,
        activity: JsonObject,
        continuation: Sequence[JsonObject],
        archived: bool,
        created_at: str,
    ) -> None:
        if not self._offline:
            raise ChatSessionError("generation import is available only to the offline converter")

        def operation(connection: sqlite3.Connection) -> None:
            _store_import.import_generation(
                connection,
                address,
                generation_id=generation_id,
                messages=messages,
                metadata=metadata,
                activity=activity,
                continuation=continuation,
                archived=archived,
                created_at=created_at,
                index_fts=not self._offline_bulk_import,
            )

        if self._offline_bulk_import:
            # The generated name is never built from an unvalidated imported id.
            savepoint = f"generation_{uuid.uuid4().hex}"
            self._writer.execute(f"SAVEPOINT {savepoint}")
            try:
                operation(self._writer)
                self._writer.execute(f"RELEASE {savepoint}")
            except BaseException:
                self._writer.execute(f"ROLLBACK TO {savepoint}")
                self._writer.execute(f"RELEASE {savepoint}")
                raise
            return
        self._execute_write(operation, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)

    def ensure_live(self, address: SessionAddress) -> None:
        return self._execute_write(
            lambda connection: _store_mutations.ensure_live(connection, address)
        )

    def exists(self, address: SessionAddress, *, include_archived: bool = False) -> bool:
        with self._runtime.read_ctx() as connection:
            return _store_queries.exists(connection, address, include_archived=include_archived)

    def state(self, address: SessionAddress, *, include_archived: bool = False) -> sqlite3.Row:
        with self._runtime.read_ctx() as connection:
            return _store_queries.state(connection, address, include_archived=include_archived)

    def metadata(self, address: SessionAddress) -> JsonObject:
        return _store_values._session_metadata_from_state(self.state(address))

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
        with self._runtime.read_ctx() as connection:
            return _store_queries.descriptor_sources(connection, addresses)

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

    def append_messages(self, address: SessionAddress, messages: Sequence[ChatMessage]) -> None:
        return self._execute_write(
            lambda connection: _store_mutations.append_messages(connection, address, messages),
            patience_s=TRANSCRIPT_WRITE_PATIENCE_S,
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
        return self._execute_write(
            lambda connection: _store_owned.append_messages_with_receipts(
                connection,
                address,
                generation_id=generation_id,
                owner_name=owner_name,
                messages=messages,
                receipts=receipts,
                deduplicate_carrier=deduplicate_carrier,
            ),
            patience_s=TRANSCRIPT_WRITE_PATIENCE_S,
        )

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

    def run_start_boundaries(self, addresses: Sequence[SessionAddress]) -> list[sqlite3.Row]:
        with self._runtime.read_ctx() as connection:
            return _store_owned.run_start_boundaries(connection, addresses)

    def messages(self, address: SessionAddress) -> list[ChatMessage]:

        with self._runtime.read_ctx() as connection:
            return _store_history.messages(connection, address)

    def active_messages(self, address: SessionAddress) -> list[ChatMessage]:
        with self._runtime.read_ctx() as connection:
            return _store_history.active_messages(connection, address)

    def active_user_message_count(self, address: SessionAddress, *, limit: int) -> int:
        with self._runtime.read_ctx() as connection:
            return _store_history.active_user_message_count(connection, address, limit=limit)

    def latest_note(self, address: SessionAddress, *, content_prefix: str) -> ChatMessage | None:
        with self._runtime.read_ctx() as connection:
            return _store_history.latest_note(connection, address, content_prefix=content_prefix)

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
    ) -> tuple[
        list[ChatMessage],
        bool,
        frozenset[str],
        JsonObject,
        list[ChatMessage],
        list[ChatMessage],
        str,
        int | None,
    ]:
        with self._runtime.read_ctx() as connection:
            return _store_history.chat_history_snapshot(
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
            )

    def status_snapshot(
        self,
        address: SessionAddress,
    ) -> tuple[str | None, int, JsonObject | None, JsonObject, int]:
        with self._runtime.read_ctx() as connection:
            return _store_history.status_snapshot(connection, address)

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
        with self._runtime.read_ctx() as connection:
            return _store_history.history_records(
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
        with self._runtime.read_ctx() as connection:
            return _store_history.history_around(
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

    def reflection_runs(self, address: SessionAddress) -> list[JsonObject]:
        with self._runtime.read_ctx() as connection:
            return _store_history.reflection_runs(connection, address)

    def run_summary(
        self,
        address: SessionAddress,
        *,
        run_id: str | None = None,
        work_id: str | None = None,
    ) -> ChatMessage | None:
        with self._runtime.read_ctx() as connection:
            return _store_history.run_summary(connection, address, run_id=run_id, work_id=work_id)

    def run_result(
        self,
        address: SessionAddress,
        *,
        run_id: str | None = None,
        work_id: str | None = None,
        require_latest: bool = False,
    ) -> tuple[ChatMessage | None, ChatMessage, str | None] | None:
        with self._runtime.read_ctx() as connection:
            return _store_history.run_result(
                connection, address, run_id=run_id, work_id=work_id, require_latest=require_latest
            )

    def messages_since(
        self, address: SessionAddress, cursor: SessionReadCursor | None
    ) -> tuple[list[ChatMessage], SessionReadCursor] | None:
        with self._runtime.read_ctx() as connection:
            return _store_history.messages_since(connection, address, cursor)

    def continuation(self, address: SessionAddress) -> list[JsonObject]:
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

    def list_addresses(
        self,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        include_all_scopes: bool = False,
    ) -> list[SessionAddress]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.list_addresses(
                connection,
                project_id=project_id,
                agent_id=agent_id,
                include_all_scopes=include_all_scopes,
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

    def list_recall_summary_rows(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        include_subagents: bool,
        excluded_session_id: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[sqlite3.Row]:
        with self._runtime.read_ctx() as connection:
            return _store_queries.list_recall_summary_rows(
                connection,
                project_id,
                agent_id,
                include_subagents=include_subagents,
                excluded_session_id=excluded_session_id,
                since=since,
                until=until,
                limit=limit,
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
        try:
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
                )
        except sqlite3.Error as exc:
            if "fts" in str(exc).lower() or "messages_fts" in str(exc).lower():
                error_message = str(exc)
                with suppress(Exception):
                    self._execute_write(
                        lambda connection: _store_fts._detach_fts(connection, error_message)
                    )
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
                    use_fts=False,
                    fallback_reason="fts_error",
                )

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
