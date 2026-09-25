"""Disposable full-text indexes: upkeep, rebuild and canonical coverage.

Both indexes are external-content FTS5 tables over views of the entries that
are current in some Session's view (``FTS_MEMBERSHIP_SQL``). Writers keep them
exact with one rule: before a change can end an entry's membership, its rows
are forgotten through the view (which still returns the indexed values); after
the change, the same candidates are indexed again, and the view admits only
those that are still members. A failing index is detached, never allowed to
block a canonical write, and rebuilt in committed batches on the next open.
"""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from core.sessions import _store_values
from core.sessions.errors import FtsHealth
from core.sessions.schema import (
    FTS_COMPLETED_HIGH_WATER_KEY,
    FTS_DEGRADED_REASON_KEY,
    FTS_GENERATION_KEY,
    FTS_MEMBERSHIP_SQL,
    FTS_SQL,
    FTS_SQL_FALLBACK,
    FTS_STALE_KEY,
    FTS_STORAGE_VERSION,
    FTS_STORAGE_VERSION_KEY,
    FTS_TABLE,
    FTS_TARGET_HIGH_WATER_KEY,
    FTS_TRIGRAM_MEMBERSHIP_SQL,
    FTS_TRIGRAM_TABLE,
    FTS_TRIGRAM_VIEW,
    FTS_VIEW,
)

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


# Entry keys one rebuild batch covers before it commits.
_FTS_BATCH_WINDOW = 500
_FTS_REBUILD_THROTTLE_S = 0.01
_FTS_REBUILD_HOOK: Callable[[str, int], None] | None = None

_STANDARD_COLUMNS = "content, search_text, reasoning, name, error_kind, tool_calls"
_TRIGRAM_COLUMNS = "content, search_text"


def _fts_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_fts_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)", (key, value))


def _history_high_water(connection: sqlite3.Connection) -> int:
    """The newest entry key ever assigned; AUTOINCREMENT never reuses keys."""
    row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name = 'entries'").fetchone()
    return 0 if row is None else int(row[0])


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


def _fts_writable(connection: sqlite3.Connection) -> bool:
    """Whether writers maintain the indexes: they exist and no rebuild is pending."""
    return _fts_table_exists(connection) and _fts_meta(connection, FTS_STALE_KEY) is None


def _drop_fts(connection: sqlite3.Connection) -> None:
    connection.execute(f"DROP TABLE IF EXISTS {FTS_TRIGRAM_TABLE}")
    connection.execute(f"DROP VIEW IF EXISTS {FTS_TRIGRAM_VIEW}")
    connection.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
    connection.execute(f"DROP VIEW IF EXISTS {FTS_VIEW}")


def _project(
    connection: sqlite3.Connection, keys_sql: str, params: Sequence[Any], *, delete: bool
) -> None:
    command = "'delete', " if delete else ""
    connection.execute(
        f"INSERT INTO {FTS_TABLE} ({FTS_TABLE + ', ' if delete else ''}rowid, {_STANDARD_COLUMNS}) "
        f"SELECT {command}source.entry_key, {_STANDARD_COLUMNS} FROM {FTS_VIEW} AS source "
        f"WHERE source.entry_key IN ({keys_sql})",
        params,
    )
    if _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        connection.execute(
            f"INSERT INTO {FTS_TRIGRAM_TABLE} ({FTS_TRIGRAM_TABLE + ', ' if delete else ''}rowid, {_TRIGRAM_COLUMNS}) "
            f"SELECT {command}source.entry_key, {_TRIGRAM_COLUMNS} FROM {FTS_TRIGRAM_VIEW} AS source "
            f"WHERE source.entry_key IN ({keys_sql})",
            params,
        )


def fts_forget(connection: sqlite3.Connection, keys_sql: str, params: Sequence[Any]) -> None:
    """Remove the index rows of the member entries *keys_sql* selects.

    Call it before the change that may end their membership: the source views
    return an entry's indexed values only while it is still a member.
    """
    if _fts_writable(connection):
        _project(connection, keys_sql, params, delete=True)


def fts_index(connection: sqlite3.Connection, keys_sql: str, params: Sequence[Any]) -> None:
    """Index the entries *keys_sql* selects that are members after a change."""
    if not _fts_writable(connection):
        return
    _project(connection, keys_sql, params, delete=False)
    high_water = str(_history_high_water(connection))
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, high_water)
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, high_water)


_KEY_LIST_SQL = "SELECT value FROM json_each(?)"


def fts_forget_keys(connection: sqlite3.Connection, keys: Sequence[int]) -> None:
    if keys:
        fts_forget(connection, _KEY_LIST_SQL, (_store_values._key_list(keys),))


def fts_index_keys(connection: sqlite3.Connection, keys: Sequence[int]) -> None:
    if keys:
        fts_index(connection, _KEY_LIST_SQL, (_store_values._key_list(keys),))


def _coverage_mismatch(connection: sqlite3.Connection, table: str, membership: str) -> bool:
    """Whether the index misses a member entry or holds a row that is not one."""
    mismatch = connection.execute(
        f"SELECT 1 FROM entries AS e LEFT JOIN {table}_docsize AS d ON d.id = e.entry_key "
        f"WHERE CASE WHEN {membership} THEN d.id IS NULL ELSE d.id IS NOT NULL END LIMIT 1"
    ).fetchone()
    if mismatch is not None:
        return True
    orphan = connection.execute(
        f"SELECT 1 FROM {table}_docsize AS d "
        "WHERE NOT EXISTS (SELECT 1 FROM entries WHERE entry_key = d.id) LIMIT 1"
    ).fetchone()
    return orphan is not None


def _fts_coverage_ok(
    connection: sqlite3.Connection, *, verify_internal_index: bool = True
) -> tuple[bool, str | None]:
    if not _fts_table_exists(connection):
        return False, "FTS tables are missing"
    trigram = _fts_table_exists(connection, FTS_TRIGRAM_TABLE)
    if _coverage_mismatch(connection, FTS_TABLE, FTS_MEMBERSHIP_SQL):
        return False, "search index coverage does not match current entries"
    if trigram and _coverage_mismatch(connection, FTS_TRIGRAM_TABLE, FTS_TRIGRAM_MEMBERSHIP_SQL):
        return False, "trigram index coverage does not match current conversation entries"
    if verify_internal_index:
        try:
            connection.execute(
                f"INSERT INTO {FTS_TABLE} ({FTS_TABLE}, rank) VALUES ('integrity-check', 1)"
            )
            if trigram:
                connection.execute(
                    f"INSERT INTO {FTS_TRIGRAM_TABLE} ({FTS_TRIGRAM_TABLE}, rank) VALUES ('integrity-check', 1)"
                )
        except sqlite3.DatabaseError:
            return False, "FTS index does not match canonical entries"
    return True, None


def _fts_health_from_connection(
    connection: sqlite3.Connection, *, verify_coverage: bool
) -> FtsHealth:
    """Read FTS lifecycle state, optionally proving canonical row coverage.

    The trigram index is optional: SQLite builds without its tokenizer keep
    the standard index only, and search then matches short terms by scan.
    """
    if not _fts_table_exists(connection):
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
        ):
            _backfill_fts(connection)
            _finish_fts_rebuild(connection)
            return
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
    """Populate both external-content indexes in committed entry-key windows."""
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
        if completed >= target:
            break
        connection.execute("BEGIN IMMEDIATE")
        try:
            batch_max = min(completed + _FTS_BATCH_WINDOW, target)
            _project(
                connection,
                "SELECT entry_key FROM entries WHERE entry_key > ? AND entry_key <= ?",
                (completed, batch_max),
                delete=False,
            )
            _fts_rebuild_boundary("before_batch_commit", batch_max)
            _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(batch_max))
            connection.execute("COMMIT")
            completed = batch_max
            _fts_rebuild_boundary("after_batch_commit", batch_max)
        except BaseException:
            with suppress(BaseException):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            raise
        time.sleep(_FTS_REBUILD_THROTTLE_S)


def _finish_fts_rebuild(connection: sqlite3.Connection) -> None:
    """Clear the rebuild marker only after an explicit coverage check."""
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
    """Mark FTS degraded and drop only the derived index objects."""
    with suppress(sqlite3.Error):
        _set_fts_meta(connection, FTS_STALE_KEY, "1")
        _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, reason)
        _drop_fts(connection)
    _store_values._LOGGER.warning(
        "Session FTS detached after corruption; canonical writes continue"
    )
