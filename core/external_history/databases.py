"""Snapshot reads of the external SQLite formats; no source migrations or writes."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.external_history.errors import ExternalHistoryError
from core.external_history.files import _lines, _opencode, _require_linear_openclaw
from core.external_history.records import ExternalSession, conversation_entry, timestamp


@contextmanager
def opened(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def tables(db: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db: sqlite3.Connection, table: str, *, alias: str = "") -> str:
    """Name the source's visible columns, preserving unknown fields in raw provenance."""
    prefix = f'"{alias}".' if alias else ""
    return ", ".join(
        prefix + '"' + row[0].replace('"', '""') + '"'
        for row in db.execute(
            "SELECT name FROM pragma_table_xinfo(?) WHERE hidden != 1 ORDER BY cid", (table,)
        )
    )


def _table(source: str, names: set[str]) -> str:
    choices = {
        "hermes": ("sessions",),
        "opencode": ("session_v2", "session"),
        "openclaw": ("session_windows",),
    }
    for table in choices.get(source, ()):
        if table in names:
            return table
    raise ExternalHistoryError(
        f"Unsupported {source} database schema. Use a JSON/JSONL conversation export; no "
        f"source was changed."
    )


def list_database(source: str, path: Path) -> list[dict[str, Any]]:
    with opened(path) as db:
        table = _table(source, tables(db))
        result = []
        for item in db.execute(f'SELECT {_columns(db, table)} FROM "{table}"'):
            row = dict(item)
            identity = str(row.get("id", row.get("session_id", "")))
            updated = row.get(
                "time_updated", row.get("ended_at") or row.get("started_at", row.get("updated_at"))
            )
            result.append(
                {
                    "source": source,
                    "id": identity,
                    "path": path.as_posix(),
                    "title": row.get("title") or row.get("display_name") or identity,
                    "cwd": row.get("directory", ""),
                    "updated_at": timestamp(
                        updated, timestamp(path.stat().st_mtime, "1970-01-01T00:00:00.000000Z")
                    ),
                    "entry_count": row.get("message_count"),
                    "warnings": [],
                }
            )
        return result


def read_database(source: str, path: Path, session_id: str) -> ExternalSession:
    with opened(path) as db:
        names = tables(db)
        table = _table(source, names)
        identity = "session_id" if source == "openclaw" else "id"
        found = db.execute(
            f'SELECT {_columns(db, table)} FROM "{table}" WHERE "{identity}" = ?', (session_id,)
        ).fetchone()
        if found is None:
            raise ExternalHistoryError(
                f"Session {session_id!r} was not found in {path.as_posix()}. List sessions "
                f"to select an existing id."
            )
        info = dict(found)
        fallback = timestamp(
            info.get("started_at", info.get("time_created")),
            timestamp(path.stat().st_mtime, "1970-01-01T00:00:00.000000Z"),
        )
        result = ExternalSession(
            source,
            session_id,
            path.as_posix(),
            str(info.get("title") or session_id),
            fallback,
            str(info.get("directory", "")),
        )
        if source == "hermes":
            for row in _hermes_display_rows(db, info):
                raw = {k: _value(v) for k, v in row.items()}
                if isinstance(raw.get("content"), dict):
                    raise ExternalHistoryError(
                        "Hermes contains binary message content. Export the complete "
                        "conversation with Hermes before importing."
                    )
                if isinstance(raw.get("tool_calls"), str):
                    raw["tool_calls"] = json.loads(raw["tool_calls"])
                if isinstance(raw.get("content"), str) and raw["content"].lstrip().startswith("["):
                    try:
                        blocks = json.loads(raw["content"])
                        if isinstance(blocks, list):
                            raw["content"] = blocks
                    except json.JSONDecodeError:
                        pass
                result.entries.append(conversation_entry(raw, fallback))
        elif source == "opencode":
            modern = table == "session_v2"
            if info.get("fork_session_id"):
                raise ExternalHistoryError(
                    "This OpenCode Session inherits history from a fork. "
                    "Export it with OpenCode and import the complete JSON export."
                )
            message_table = "session_message" if modern else "message"
            part_table = "session_part" if modern else "part"
            order = "seq" if modern else "time_created, id"
            messages = []
            for row in db.execute(
                f'SELECT {_columns(db, message_table)} FROM "{message_table}" '
                f"WHERE session_id = ? ORDER BY {order}",
                (session_id,),
            ):
                data = json.loads(row["data"])
                parts = []
                if part_table in names:
                    part_order = "ordinal" if modern else "id"
                    parts = [
                        json.loads(part["data"])
                        for part in db.execute(
                            f'SELECT data FROM "{part_table}" '
                            f"WHERE message_id = ? ORDER BY {part_order}",
                            (row["id"],),
                        )
                    ]
                data.setdefault("id", row["id"])
                data.setdefault("time", {"created": row["time_created"]})
                if modern:
                    data.setdefault("type", row["type"])
                    if parts:
                        data["content"] = parts
                    messages.append(data)
                else:
                    messages.append({"info": data, "parts": parts})
            _opencode(result, {"info": info, "messages": messages})
        elif source == "openclaw":
            if "transcript_events" not in names:
                raise ExternalHistoryError(
                    "This OpenClaw database has no readable transcript events. Use a "
                    "transcript export."
                )
            records = []
            # The active path keeps original pre-Compaction entries, but excludes
            # abandoned branches. Never replace missing cold payloads with a summary.
            query = (
                f"SELECT {_columns(db, 'transcript_events', alias='event')} "
                "FROM transcript_events AS event "
            )
            if "session_transcript_active_events" in names:
                query += (
                    "JOIN session_transcript_active_events AS active ON "
                    "active.session_id = event.session_id AND active.event_seq = "
                    "event.seq WHERE event.session_id = ? ORDER BY "
                    "active.active_position"
                )
            else:
                query += "WHERE event.session_id = ? ORDER BY event.seq"
            for row in db.execute(query, (session_id,)):
                raw = dict(row)
                payload = raw.get("event_json")
                if payload is None and raw.get("event_zstd") is not None:
                    from compression import zstd

                    payload = zstd.decompress(raw["event_zstd"]).decode("utf-8")
                if payload is None:
                    raise ExternalHistoryError(
                        "OpenClaw moved part of this transcript into cold storage. Export "
                        "the complete transcript with OpenClaw, then import that file. "
                        "Nothing was imported."
                    )
                records.append(json.loads(payload))
            if "session_transcript_active_events" not in names:
                _require_linear_openclaw(records)
            _lines(result, records)
        if not result.entries:
            raise ExternalHistoryError(
                "The selected source has no readable conversation entries. Nothing was imported."
            )
        result.updated_at = result.entries[-1].timestamp
        return result


def _value(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeError:
            return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def _hermes_display_rows(db: sqlite3.Connection, info: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the selected display generation, never resurrect a reset or withdrawn turn."""
    parent_id = info.get("parent_session_id")
    config = info.get("model_config") or {}
    if isinstance(config, str):
        config = json.loads(config)
    explicit_child = info.get("source") == "tool" or (
        parent_id
        and isinstance(config, dict)
        and parent_id
        in [config.get(key) for key in ("_branched_from", "_delegate_from", "_reset_from")]
    )
    if parent_id and not explicit_child:
        parent = db.execute(
            f"SELECT {_columns(db, 'sessions')} FROM sessions WHERE id = ?", (parent_id,)
        ).fetchone()
        # Cross-segment compression also has copied tails, summary carriers and
        # withdrawal identities. A raw parent walk cannot reconstruct that display.
        if (
            parent is None
            or "end_reason" not in dict(parent)
            or parent["end_reason"] == "compression"
        ):
            raise ExternalHistoryError(
                "This Hermes Session has inherited compression history that needs a complete "
                "Hermes conversation export. Import that JSON export; nothing was imported."
            )
    rows = [
        dict(row)
        for row in db.execute(
            f"SELECT {_columns(db, 'messages')} FROM messages WHERE session_id = ? ORDER BY id",
            (info["id"],),
        )
    ]
    visible = []
    for row in rows:
        if not row.get("active", 1) and not row.get("compacted", 0):
            continue
        metadata = row.get("display_metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if isinstance(metadata, dict) and metadata.get("model_only"):
            continue
        visible.append(row)
    if not visible:
        return []
    if any(row.get("compacted") for row in visible) and any(
        row.get("display_order") is None for row in visible
    ):
        raise ExternalHistoryError(
            "Hermes compacted history has no complete display order. Export the complete "
            "conversation with Hermes and import that JSON file."
        )
    if all(row.get("display_order") is not None for row in visible):
        # Hermes displays the original archived Tool Result instead of its later
        # shortened copy. The same-slot live generation still wins.
        first_tool_slot: dict[tuple[Any, Any], int] = {}
        for row in visible:
            if row.get("role") == "tool" and row.get("message_uid") and not row.get("active", 1):
                key = (row["message_uid"], row.get("timestamp"))
                first_tool_slot[key] = min(
                    first_tool_slot.get(key, row["display_order"]), row["display_order"]
                )
        slots: dict[int, dict[str, Any]] = {}
        for row in visible:
            slot = row["display_order"]
            if (
                row.get("role") == "tool"
                and first_tool_slot.get((row.get("message_uid"), row.get("timestamp")), slot) < slot
            ):
                continue
            previous = slots.get(slot)
            if previous is None or (row.get("active", 1), row["id"]) > (
                previous.get("active", 1),
                previous["id"],
            ):
                slots[slot] = row
        return [slots[slot] for slot in sorted(slots)]
    return visible
