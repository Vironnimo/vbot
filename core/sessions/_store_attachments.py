"""Attachment reachability across every retained entry, including own audits."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from core.utils.ids import is_safe_id

# Paths and inline links are usable handles too. Retain their blobs even when
# the containing message has no structured media block. Include the previous
# UUID-hex attachment ids; explicit attachment_id fields accept every safe id.
_INLINE_ID = re.compile(r"(?<![\w-])(?:att_[a-z0-9_-]+|[a-f0-9]{32})(?![\w-])", re.IGNORECASE)


def attachment_ids(value: Any) -> set[str]:
    """Find structured blob references and attachment handles in persisted text."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(match.group().lower() for match in _INLINE_ID.finditer(value))
    elif isinstance(value, Mapping):
        explicit = value.get("attachment_id")
        if isinstance(explicit, str) and is_safe_id(explicit.lower()):
            found.add(explicit.lower())
        for child in value.values():
            found.update(attachment_ids(child))
    elif isinstance(value, list | tuple):
        for child in value:
            found.update(attachment_ids(child))
    return found


def record(connection: sqlite3.Connection, entry_key: int, values: Any) -> None:
    ids = attachment_ids(values)
    connection.executemany(
        "INSERT OR IGNORE INTO session_attachment_ids (attachment_id) VALUES (?)",
        ((attachment_id,) for attachment_id in ids),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO session_attachment_refs (entry_key, attachment_id) VALUES (?, ?)",
        ((entry_key, attachment_id) for attachment_id in ids),
    )


def _stored_values(connection: sqlite3.Connection) -> Iterator[tuple[int, Any]]:
    # Select only content-bearing columns, never Session visibility: archives,
    # superseded history and materialized fork copies are equally live roots.
    sources = (
        ("entry_text", "entry_key", ("content", "blocks_json"), ""),
        ("assistant_reasoning", "entry_key", ("reasoning", "summary_json", "meta_json"), ""),
        ("assistant_output_files", "entry_key", ("path",), ""),
        ("checkpoint_projections", "entry_key", ("projection_json",), ""),
        (
            "tool_call_payloads AS p",
            "c.entry_key",
            ("arguments_json", "display_json", "media_json"),
            " JOIN tool_calls AS c ON c.call_key = p.call_key",
        ),
        (
            "tool_result_payloads AS p",
            "c.entry_key",
            ("payload_json",),
            " JOIN tool_calls AS c ON c.call_key = p.call_key",
        ),
    )
    for table, key, columns, join in sources:
        available = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table.split()[0]})")
        }
        selected_columns = tuple(column for column in columns if column in available)
        if not selected_columns:
            if table == "entry_text":
                raise sqlite3.DatabaseError("Session snapshot has no canonical entry text")
            continue
        for row in connection.execute(
            f"SELECT {key}, {', '.join(selected_columns)} FROM {table}{join}"
        ):
            for column, value in zip(selected_columns, row[1:], strict=True):
                if value is not None:
                    try:
                        decoded = json.loads(value) if column.endswith("_json") else value
                    except ValueError as exc:
                        raise sqlite3.DatabaseError(
                            "Session snapshot contains invalid JSON"
                        ) from exc
                    yield int(row[0]), decoded


def backfill(connection: sqlite3.Connection) -> None:
    """Adopt existing references without claiming never-admitted uploads."""
    for entry_key, value in _stored_values(connection):
        record(connection, entry_key, value)


def referenced(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT attachment_id FROM session_attachment_refs "
            "UNION SELECT attachment_id FROM session_accessor_input_attachments"
        )
    )


def snapshot_references(connection: sqlite3.Connection) -> frozenset[str]:
    """Read current or older snapshot formats without migrating the snapshot."""
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'session_attachment_refs'"
        ).fetchone()
        is not None
    ):
        indexed_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT attachment_id FROM session_attachment_refs"
            )
        }
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
                "AND name = 'session_accessor_input_attachments'"
            ).fetchone()
            is not None
        ):
            indexed_ids.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT attachment_id FROM session_accessor_input_attachments"
                )
            )
        return frozenset(indexed_ids)
    ids: set[str] = set()
    for _entry_key, value in _stored_values(connection):
        ids.update(attachment_ids(value))
    return frozenset(ids)


def unreferenced(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT attachment_id FROM session_attachment_ids AS known WHERE NOT EXISTS "
            "(SELECT 1 FROM session_attachment_refs AS live "
            "WHERE live.attachment_id = known.attachment_id) AND NOT EXISTS "
            "(SELECT 1 FROM session_accessor_input_attachments AS queued "
            "WHERE queued.attachment_id = known.attachment_id) ORDER BY attachment_id"
        )
    )


def forget(connection: sqlite3.Connection, ids: Sequence[str] | Iterable[str]) -> None:
    connection.executemany(
        "DELETE FROM session_attachment_ids WHERE attachment_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM session_attachment_refs "
        "WHERE attachment_id = session_attachment_ids.attachment_id) AND NOT EXISTS "
        "(SELECT 1 FROM session_accessor_input_attachments "
        "WHERE attachment_id = session_attachment_ids.attachment_id)",
        ((attachment_id,) for attachment_id in ids),
    )
