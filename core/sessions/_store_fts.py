"""Disposable full-text index lifecycle and canonical coverage."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from core.sessions import _store_values
from core.sessions.errors import FtsHealth
from core.sessions.schema import (
    FTS_COMPLETED_HIGH_WATER_KEY,
    FTS_DEGRADED_REASON_KEY,
    FTS_GENERATION_KEY,
    FTS_SQL,
    FTS_SQL_FALLBACK,
    FTS_STALE_KEY,
    FTS_STORAGE_VERSION,
    FTS_STORAGE_VERSION_KEY,
    FTS_TABLE,
    FTS_TARGET_HIGH_WATER_KEY,
    FTS_TRIGGERS,
    FTS_TRIGRAM_TABLE,
    FTS_TRIGRAM_VIEW,
    FTS_VIEW,
)

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


_FTS_BATCH_SIZE = 100
_FTS_REBUILD_THROTTLE_S = 0.01
_FTS_REBUILD_HOOK: Callable[[str, int], None] | None = None


def _fts_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_fts_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)", (key, value))


def _history_high_water(connection: sqlite3.Connection) -> int:
    return int(_fts_meta(connection, "history_identity") or 0)


def _rebuild_identities(
    connection: sqlite3.Connection, completed: int, target: int
) -> list[sqlite3.Row]:
    """Merge indexed prefixes without sorting the remaining cross-entity history."""
    rows: list[sqlite3.Row] = []
    for query in (
        "SELECT message_key, role, searchable, active FROM messages "
        "WHERE message_key>? AND message_key<=? ORDER BY message_key LIMIT ?",
        "SELECT result_key AS message_key, 'tool' AS role, 1 AS searchable, result_active AS active "
        "FROM tool_calls WHERE result_id IS NOT NULL AND result_key>? AND result_key<=? "
        "ORDER BY result_key LIMIT ?",
        "SELECT terminal_key AS message_key, 'run_summary' AS role, 0 AS searchable, terminal_active AS active "
        "FROM runs WHERE terminal_sequence IS NOT NULL AND terminal_key>? AND terminal_key<=? "
        "ORDER BY terminal_key LIMIT ?",
        "SELECT snapshot_key AS message_key, 'compaction_checkpoint' AS role, 1 AS searchable, active "
        "FROM compaction_checkpoints WHERE snapshot_key>? AND snapshot_key<=? ORDER BY snapshot_key LIMIT ?",
        "SELECT edit_key AS message_key, 'history_edit' AS role, 0 AS searchable, 0 AS active "
        "FROM history_edits WHERE edit_key>? AND edit_key<=? ORDER BY edit_key LIMIT ?",
    ):
        rows.extend(connection.execute(query, (completed, target, _FTS_BATCH_SIZE)))
    return sorted(rows, key=lambda row: row["message_key"])[:_FTS_BATCH_SIZE]


def _message_is_searchable(message: ChatMessage) -> bool:
    """Test index eligibility without materializing large Message search text."""
    from core.recall.canonical import (
        SESSION_RECALL_CONVERSATION_ROLES,
        is_recall_artifact_message,
    )
    from core.sessions.history import is_skill_context_note

    if (
        message.role not in SESSION_RECALL_CONVERSATION_ROLES
        or is_recall_artifact_message(message)
        or is_skill_context_note(message)
    ):
        return False
    return bool(
        message.content
        or message.reasoning
        or message.name
        or message.error_kind
        or message.tool_calls
    )


def _fts_table_exists(connection: sqlite3.Connection, table: str = FTS_TABLE) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _drop_fts_triggers(connection: sqlite3.Connection) -> None:
    for trigger in FTS_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _drop_fts(connection: sqlite3.Connection) -> None:
    _drop_fts_triggers(connection)
    connection.execute(f"DROP TABLE IF EXISTS {FTS_TRIGRAM_TABLE}")
    connection.execute(f"DROP VIEW IF EXISTS {FTS_TRIGRAM_VIEW}")
    connection.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
    connection.execute(f"DROP VIEW IF EXISTS {FTS_VIEW}")


def _fts_coverage_ok(
    connection: sqlite3.Connection, *, verify_internal_index: bool = True
) -> tuple[bool, str | None]:
    if not _fts_table_exists(connection) or not _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        return False, "FTS tables are missing"
    missing_base = connection.execute(
        """
        SELECT 1
        FROM history_records AS m
        LEFT JOIN messages_fts_docsize AS indexed ON indexed.id = m.message_key
        WHERE (m.searchable = 1 AND m.active = 1 AND indexed.id IS NULL)
           OR ((m.searchable = 0 OR m.active = 0) AND indexed.id IS NOT NULL)
        LIMIT 1
        """
    ).fetchone()
    if missing_base is not None:
        return False, "canonical Messages are missing base FTS rows"
    invalid_trigram = connection.execute(
        """
        SELECT 1
        FROM history_records AS m
        LEFT JOIN messages_fts_trigram_docsize AS indexed ON indexed.id = m.message_key
        WHERE (m.searchable = 1 AND m.active = 1 AND m.role <> 'tool' AND indexed.id IS NULL)
           OR ((m.searchable = 0 OR m.active = 0 OR m.role = 'tool') AND indexed.id IS NOT NULL)
        LIMIT 1
        """
    ).fetchone()
    if invalid_trigram is not None:
        return False, "trigram FTS coverage does not match non-Tool Messages"
    if verify_internal_index:
        try:
            connection.execute(
                "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
            )
            connection.execute(
                "INSERT INTO messages_fts_trigram(messages_fts_trigram, rank) VALUES('integrity-check', 1)"
            )
        except sqlite3.DatabaseError:
            return False, "FTS index does not match canonical Messages"
    return True, None


def _fts_health_from_connection(
    connection: sqlite3.Connection, *, verify_coverage: bool
) -> FtsHealth:
    """Read FTS lifecycle state, optionally proving canonical row coverage."""
    if not _fts_table_exists(connection) or not _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        return FtsHealth(state="unavailable", reason="FTS tables are missing")
    storage_version = _fts_meta(connection, FTS_STORAGE_VERSION_KEY)
    generation = _fts_meta(connection, FTS_GENERATION_KEY)
    target = _fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY)
    completed = _fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY)
    stale = _fts_meta(connection, FTS_STALE_KEY)
    reason = _fts_meta(connection, FTS_DEGRADED_REASON_KEY)
    if storage_version != str(FTS_STORAGE_VERSION):
        return FtsHealth(
            state="degraded",
            reason="FTS storage version is missing or unsupported",
            generation=generation,
        )
    if not generation:
        return FtsHealth(state="degraded", reason="FTS rebuild generation is missing")
    try:
        target_value = int(target) if target is not None else -1
        completed_value = int(completed) if completed is not None else -1
    except (TypeError, ValueError):
        return FtsHealth(
            state="degraded",
            reason="FTS high-water metadata is malformed",
            generation=generation,
        )
    if target_value < 0 or completed_value < 0 or completed_value > target_value:
        return FtsHealth(
            state="degraded",
            reason="FTS high-water metadata is invalid",
            generation=generation,
            target_high_water=target_value,
            completed_high_water=completed_value,
        )
    coverage_ok = True
    coverage_reason = None
    if verify_coverage:
        coverage_ok, coverage_reason = _fts_coverage_ok(connection, verify_internal_index=False)
    if stale is not None or completed_value != target_value or not coverage_ok:
        rebuilding = stale == "rebuilding" or completed_value != target_value
        return FtsHealth(
            state="rebuilding" if rebuilding else "degraded",
            reason=reason or stale or coverage_reason or "FTS coverage is incomplete",
            generation=generation,
            target_high_water=target_value,
            completed_high_water=completed_value,
        )
    return FtsHealth(
        state="healthy",
        generation=generation,
        target_high_water=target_value,
        completed_high_water=completed_value,
    )


def _fts_rebuild_boundary(stage: str, high_water: int) -> None:
    if _FTS_REBUILD_HOOK is not None:
        _FTS_REBUILD_HOOK(stage, high_water)


def _ensure_fts_schema(connection: sqlite3.Connection) -> None:
    """Create or repair the derived FTS projection without weakening canonical storage."""
    try:
        health = _fts_health_from_connection(connection, verify_coverage=True)
        if health.available:
            return
        if (
            health.state == "rebuilding"
            and _fts_meta(connection, FTS_STORAGE_VERSION_KEY) == str(FTS_STORAGE_VERSION)
            and _fts_table_exists(connection)
            and _fts_table_exists(connection, FTS_TRIGRAM_TABLE)
        ):
            _backfill_fts(connection)
            _finish_fts_rebuild(connection)
            return
        if _fts_table_exists(connection) or _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
            _drop_fts(connection)
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + FTS_SQL)
        except sqlite3.Error:
            with suppress(sqlite3.Error):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            _drop_fts(connection)
            connection.executescript("BEGIN IMMEDIATE;\n" + FTS_SQL_FALLBACK)
        _set_fts_meta(connection, FTS_STORAGE_VERSION_KEY, str(FTS_STORAGE_VERSION))
        _set_fts_meta(connection, FTS_GENERATION_KEY, uuid.uuid4().hex)
        _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, "0")
        _set_fts_meta(connection, FTS_STALE_KEY, "rebuilding")
        _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, "FTS rebuild in progress")
        target = _history_high_water(connection)
        _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
        if target == 0:
            coverage_ok, coverage_reason = _fts_coverage_ok(connection)
            if not coverage_ok:
                raise sqlite3.DatabaseError(
                    coverage_reason or "empty FTS bootstrap failed its coverage check"
                )
            _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, "")
            connection.execute("DELETE FROM store_meta WHERE key = ?", (FTS_STALE_KEY,))
            connection.execute("COMMIT")
            return
        connection.execute("COMMIT")
        _backfill_fts(connection)
        _finish_fts_rebuild(connection)
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        try:
            connection.execute("BEGIN IMMEDIATE")
            _set_fts_meta(connection, FTS_STALE_KEY, "1")
            _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, f"FTS unavailable: {exc}")
            connection.execute("COMMIT")
        except sqlite3.Error:
            with suppress(sqlite3.Error):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
        _store_values._LOGGER.warning("Session FTS unavailable, using canonical scan: %s", exc)


def _backfill_fts(connection: sqlite3.Connection) -> None:
    """Populate both external-content indexes in committed bounded batches."""
    generation = _fts_meta(connection, FTS_GENERATION_KEY) or uuid.uuid4().hex
    _set_fts_meta(connection, FTS_GENERATION_KEY, generation)
    previous = _fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY)
    try:
        completed = max(0, int(previous)) if previous is not None else 0
    except ValueError:
        completed = 0
    target = _history_high_water(connection)
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(completed))
    connection.commit()
    while True:
        target = max(target, _history_high_water(connection))
        _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = _rebuild_identities(connection, completed, target)
            if not rows:
                connection.execute("COMMIT")
                break
            batch_max = int(rows[-1][0])
            # Bound identities before reading content. Joining the two UNION
            # views lets SQLite materialize every Session's text for each batch.
            sources = {
                int(source["message_key"]): source
                for source in connection.execute(
                    "SELECT * FROM messages_fts_source WHERE message_key > ? AND message_key <= ?",
                    (completed, batch_max),
                )
            }
            _fts_rebuild_boundary("before_batch_commit", batch_max)
            for row in rows:
                if not bool(row["searchable"]) or not bool(row["active"]):
                    continue
                source = sources[int(row["message_key"])]
                values = (
                    source["content"],
                    source["content_search"],
                    source["reasoning"],
                    source["name"],
                    source["error_kind"],
                    source["tool_calls"],
                )
                connection.execute(
                    "INSERT INTO messages_fts(rowid, content, content_search, reasoning, name, error_kind, tool_calls) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (int(row["message_key"]), *values),
                )
                if row["role"] != "tool":
                    connection.execute(
                        "INSERT INTO messages_fts_trigram(rowid, content, content_search, name, error_kind, tool_calls) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            int(row["message_key"]),
                            source["content"],
                            source["content_search"],
                            source["name"],
                            source["error_kind"],
                            source["tool_calls"],
                        ),
                    )
            _set_fts_meta(
                connection,
                FTS_COMPLETED_HIGH_WATER_KEY,
                str(max(completed, batch_max)),
            )
            connection.execute("COMMIT")
            completed = max(completed, batch_max)
            _fts_rebuild_boundary("after_batch_commit", batch_max)
        except BaseException:
            with suppress(BaseException):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            raise
        time.sleep(_FTS_REBUILD_THROTTLE_S)


def _finish_fts_rebuild(connection: sqlite3.Connection) -> None:
    """Rebuild FTS rows and clear stale only after explicit coverage checks."""
    coverage_ok, coverage_reason = _fts_coverage_ok(connection)
    if not coverage_ok:
        _set_fts_meta(
            connection,
            FTS_DEGRADED_REASON_KEY,
            coverage_reason or "FTS coverage is incomplete",
        )
        connection.commit()
        return
    final_target = _history_high_water(connection)
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(final_target))
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(final_target))
    _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, "")
    connection.execute("DELETE FROM store_meta WHERE key = ?", (FTS_STALE_KEY,))
    _fts_rebuild_boundary("after_final_clear", final_target)
    connection.commit()


def _detach_fts(connection: sqlite3.Connection, reason: str = "FTS write failed") -> None:
    """Mark FTS degraded and remove only its virtual table/triggers."""
    with suppress(sqlite3.Error):
        _set_fts_meta(connection, FTS_STALE_KEY, "1")
        _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, reason)
        _drop_fts(connection)
    _store_values._LOGGER.warning(
        "Session FTS detached after corruption; canonical writes continue"
    )


def _mark_fts_write(connection: sqlite3.Connection) -> None:
    if not _fts_table_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    target = _history_high_water(connection)
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(target))


def _insert_fts_message(connection: sqlite3.Connection, message_key: int) -> None:
    """Project one fully written normalized Message into disposable FTS."""
    if not _fts_table_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    state = connection.execute(
        "SELECT role, searchable, active FROM history_records WHERE message_key = ?", (message_key,)
    ).fetchone()
    if state is None or not bool(state["searchable"]) or not bool(state["active"]):
        return
    connection.execute(
        """
        INSERT INTO messages_fts(
            rowid, content, content_search, reasoning, name, error_kind, tool_calls
        )
        SELECT message_key, content, content_search, reasoning, name, error_kind, tool_calls
        FROM messages_fts_source
        WHERE message_key = ?
        """,
        (message_key,),
    )
    if state["role"] != "tool" and _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        connection.execute(
            """
            INSERT INTO messages_fts_trigram(
                rowid, content, content_search, name, error_kind, tool_calls
            )
            SELECT message_key, content, content_search, name, error_kind, tool_calls
            FROM messages_fts_source
            WHERE message_key = ?
            """,
            (message_key,),
        )


def _insert_fts_session(connection: sqlite3.Connection, session_key: int) -> None:
    """Project one already-normalized Session into both external-content indexes."""
    if not _fts_table_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    connection.execute(
        """
        INSERT INTO messages_fts(
            rowid, content, content_search, reasoning, name, error_kind, tool_calls
        )
        SELECT source.message_key, source.content, source.content_search, source.reasoning,
               source.name, source.error_kind, source.tool_calls
        FROM messages_fts_source AS source
        WHERE source.message_key IN (
            SELECT message_key FROM history_records WHERE session_key = ?
        )
        """,
        (session_key,),
    )
    if _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        connection.execute(
            """
            INSERT INTO messages_fts_trigram(
                rowid, content, content_search, name, error_kind, tool_calls
            )
            SELECT source.message_key, source.content, source.content_search, source.name,
                   source.error_kind, source.tool_calls
            FROM messages_fts_source AS source
            WHERE source.message_key IN (
                SELECT message_key FROM history_records WHERE session_key = ? AND role <> 'tool'
            )
            """,
            (session_key,),
        )


def _delete_fts_session(
    connection: sqlite3.Connection, session_key: int, *, from_sequence: int | None = None
) -> None:
    """Delete one Session's indexed projections set-wise before canonical state hides them.

    With *from_sequence*, only records at or after that history sequence are
    removed. External-content deletes must repeat the indexed values, which the
    source views still expose because canonical rows change only afterwards.
    """
    if not _fts_table_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    scope = "session_key = ?" if from_sequence is None else "session_key = ? AND seq >= ?"
    params = (session_key,) if from_sequence is None else (session_key, from_sequence)
    connection.execute(
        f"""
        INSERT INTO messages_fts(
            messages_fts, rowid, content, content_search, reasoning, name, error_kind, tool_calls
        )
        SELECT 'delete', source.message_key, source.content, source.content_search,
               source.reasoning, source.name, source.error_kind, source.tool_calls
        FROM messages_fts_source AS source
        WHERE source.message_key IN (SELECT message_key FROM history_records WHERE {scope})
        """,
        params,
    )
    if _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        connection.execute(
            f"""
            INSERT INTO messages_fts_trigram(
                messages_fts_trigram, rowid, content, content_search, name, error_kind, tool_calls
            )
            SELECT 'delete', source.message_key, source.content, source.content_search,
                   source.name, source.error_kind, source.tool_calls
            FROM messages_fts_source AS source
            WHERE source.message_key IN (
                SELECT message_key FROM history_records WHERE {scope} AND role <> 'tool'
            )
            """,
            params,
        )
