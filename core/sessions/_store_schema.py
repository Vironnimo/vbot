"""Opening-time schema and normalized metadata reconciliation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.sessions import (
    _store_fts,
    _store_values,
)
from core.sessions.errors import (
    SessionStoreCorruptError,
    SessionStoreSchemaMismatchError,
)
from core.sessions.schema import (
    APPLICATION_ID,
    DATABASE_ID_META_KEY,
    SCHEMA_CONVERSION_FLOOR,
    SCHEMA_VERSION,
    reconcile_schema,
)


def _reconcile_open_database(
    connection: sqlite3.Connection, path: Path, *, expected_database_id: str | None
) -> None:
    if expected_database_id is not None:
        _verify_database_identity(connection, path, expected_database_id)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != 0 and version < SCHEMA_CONVERSION_FLOOR:
        raise SessionStoreSchemaMismatchError(
            f"Session database schema {version} requires the offline converter"
        )
    applied = reconcile_schema(connection)
    if applied:
        _store_values._LOGGER.info(
            "Reconciled Session database schema at %s: %s",
            path,
            "; ".join(applied),
        )
    _reconcile_session_metadata_projection(connection, path)
    _store_fts._ensure_fts_schema(connection)
    _verify_connection(connection, path)


def _reconcile_session_metadata_projection(connection: sqlite3.Connection, path: Path) -> None:
    row = connection.execute(
        "SELECT value FROM store_meta WHERE key = ?",
        (_store_values._SESSION_METADATA_PROJECTION_VERSION_KEY,),
    ).fetchone()
    if row is not None and str(row[0]) == _store_values._SESSION_METADATA_PROJECTION_VERSION:
        return
    rows = connection.execute("SELECT * FROM sessions ORDER BY session_key").fetchall()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for state in rows:
            metadata = _store_values._session_metadata_from_state(state)
            residual_payload, projection = _store_values._session_metadata_storage(metadata)
            connection.execute(
                "UPDATE sessions SET active_sort = "
                "COALESCE(julianday(COALESCE(last_message_at, created_at)), 0.0), "
                "metadata_json = ?, "
                + ", ".join(
                    f"{column} = ?" for column in _store_values._SESSION_METADATA_PROJECTION_COLUMNS
                )
                + " WHERE session_key = ?",
                (residual_payload, *projection, state["session_key"]),
            )
        connection.execute(
            "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
            (
                _store_values._SESSION_METADATA_PROJECTION_VERSION_KEY,
                _store_values._SESSION_METADATA_PROJECTION_VERSION,
            ),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    if rows:
        _store_values._LOGGER.info(
            "Reconciled normalized Session metadata projections at %s (sessions=%s)",
            path,
            len(rows),
        )


def _verify_connection(connection: sqlite3.Connection, path: Path) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        raise SessionStoreSchemaMismatchError(
            f"unsupported Session database version {version} at {path}"
        )
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    if application_id != APPLICATION_ID:
        raise SessionStoreCorruptError(f"not a vBot Session database: {path}")
    try:
        for table in (
            "store_meta",
            "sessions",
            "messages",
            "assistant_messages",
            "tool_calls",
            "tool_messages",
            "run_summaries",
            "continuations",
        ):
            connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        raise SessionStoreCorruptError(
            f"Session database structure is unreadable at {path}"
        ) from exc


def _verify_database_identity(connection: sqlite3.Connection, path: Path, database_id: str) -> None:
    row = connection.execute(
        "SELECT value FROM store_meta WHERE key = ?", (DATABASE_ID_META_KEY,)
    ).fetchone()
    if row is None or str(row[0]) != database_id:
        raise SessionStoreCorruptError(
            f"Session database identity does not match the store marker at {path}"
        )
