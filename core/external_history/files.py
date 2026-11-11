"""Read source transcripts; source-specific protocol data never becomes vBot control data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.external_history.errors import ExternalHistoryError
from core.external_history.records import (
    ExternalEntry,
    ExternalSession,
    conversation_entry,
    text,
    timestamp,
)

MAX_SOURCE_BYTES = 256 * 1024 * 1024


def read_records(path: Path) -> Any:
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ExternalHistoryError(
            f"Source exceeds 256 MiB: {path.as_posix()}. Export a smaller conversation."
        )
    try:
        with path.open(encoding="utf-8-sig") as stream:
            if path.suffix.lower() == ".jsonl":
                records = []
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ExternalHistoryError(
                            f"Invalid JSON at {path.as_posix()}:{number}. Nothing was imported. "
                            "Wait for the source writer to finish or use a completed export."
                        ) from exc
                    if not isinstance(record, dict):
                        raise ExternalHistoryError(
                            f"Expected an object at {path.as_posix()}:{number}."
                        )
                    records.append(record)
                return records
            return json.load(stream)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalHistoryError(
            f"Cannot decode {path.as_posix()} as a UTF-8 transcript: {exc}."
        ) from exc


def read_session(source: str, path: Path) -> ExternalSession:
    data = read_records(path)
    modified = timestamp(path.stat().st_mtime, "1970-01-01T00:00:00.000000Z")
    result = ExternalSession(source, path.stem, path.as_posix(), path.stem, modified)
    if source == "opencode":
        _opencode(result, data)
    elif isinstance(data, list):
        if source == "openclaw":
            _require_linear_openclaw(data)
        _lines(result, data)
    elif isinstance(data, dict) and isinstance(data.get("messages"), list):
        info = data.get("session", data)
        result.id = str(info.get("id", info.get("session_id", result.id)))
        result.title = str(info.get("title", result.id))
        result.cwd = str(info.get("cwd", ""))
        if not all(isinstance(item, dict) for item in data["messages"]):
            raise ExternalHistoryError("The transcript contains a non-object message.")
        result.entries = [conversation_entry(item, modified) for item in data["messages"]]
    else:
        raise ExternalHistoryError(
            f"Unrecognized {source} transcript at {path.as_posix()}. Use that "
            f"application's conversation export."
        )
    if not result.entries:
        raise ExternalHistoryError(
            f"No conversation entries found in {path.as_posix()}. Nothing was imported."
        )
    if result.title in {path.stem, result.id}:
        first = next(
            (e.content for e in result.entries if e.role == "user" and e.kind == "message"),
            result.id,
        )
        result.title = " ".join(first.split())[:160] or result.id
    result.updated_at = result.entries[-1].timestamp
    return result


def _require_linear_openclaw(records: list[dict[str, Any]]) -> None:
    """A branched append-only log is not itself the selected conversation."""
    previous: str | None = None
    seen: set[str] = set()
    for row in records:
        kind = row.get("type")
        if kind == "session":
            ambiguous = bool(row.get("inheritedThrough"))
        else:
            ambiguous = (
                kind in {"leaf", "reset", "branch_summary"}
                or row.get("appendMode") == "side"
                or "targetId" in row
                and kind != "label"
            )
            if "parentId" in row:
                identity = row.get("id")
                ambiguous = ambiguous or (
                    not isinstance(identity, str) or identity in seen or row["parentId"] != previous
                )
                if isinstance(identity, str):
                    seen.add(identity)
                    previous = identity
        if ambiguous:
            raise ExternalHistoryError(
                "This OpenClaw log contains branch navigation or inherited paths. "
                "Use a complete export of the selected conversation, or read its "
                "SQLite database with the active-path index. Nothing was imported."
            )


def _lines(result: ExternalSession, records: list[dict[str, Any]]) -> None:
    has_codex_items = any(row.get("type") == "response_item" for row in records)
    for row in records:
        kind = row.get("type")
        if result.source == "codex":
            payload = row.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                result.id = str(payload.get("id", result.id))
                result.cwd = str(payload.get("cwd", ""))
            elif kind == "response_item":
                item_kind = payload.get("type")
                when = timestamp(row.get("timestamp"), result.updated_at)
                if item_kind == "message":
                    entry = conversation_entry({**payload, "timestamp": when}, when)
                    result.entries.append(
                        ExternalEntry(entry.role, entry.content, when, row, entry.kind)
                    )
                elif item_kind in {
                    "function_call",
                    "custom_tool_call",
                    "function_call_output",
                    "custom_tool_call_output",
                    "web_search_call",
                    "computer_call",
                    "local_shell_call",
                }:
                    result.entries.append(
                        ExternalEntry("tool", text(payload), when, row, "tool_activity")
                    )
                elif item_kind == "reasoning":
                    readable = payload.get("summary") or payload.get("content")
                    if readable:
                        result.entries.append(
                            ExternalEntry("assistant", text(readable), when, row, "reasoning")
                        )
                elif item_kind:
                    result.entries.append(
                        ExternalEntry("assistant", text(payload), when, row, "source_record")
                    )
            elif kind == "compacted":
                result.entries.append(
                    ExternalEntry(
                        "assistant",
                        text(payload.get("message", payload)),
                        timestamp(row.get("timestamp"), result.updated_at),
                        row,
                        "compaction",
                    )
                )
            elif (
                kind == "event_msg"
                and not has_codex_items
                and payload.get("type") in {"user_message", "agent_message"}
            ):
                role = "user" if payload["type"] == "user_message" else "assistant"
                result.entries.append(
                    ExternalEntry(
                        role,
                        text(payload.get("message")),
                        timestamp(row.get("timestamp"), result.updated_at),
                        row,
                    )
                )
            continue
        if result.source == "claude-code":
            if row.get("sessionId"):
                result.id = str(row["sessionId"])
            result.cwd = str(row.get("cwd", result.cwd))
            if kind == "custom-title":
                result.title = str(row.get("customTitle", result.title))
            if (
                kind in {"user", "assistant"}
                and isinstance(row.get("message"), dict)
                or kind == "system"
                and row.get("content")
            ):
                result.entries.append(conversation_entry(row, result.updated_at))
            continue
        if result.source == "openclaw":
            if kind == "session":
                result.id = str(row.get("id", result.id))
                result.cwd = str(row.get("cwd", ""))
            elif kind == "message" and isinstance(row.get("message"), dict):
                result.entries.append(conversation_entry(row, result.updated_at))
            elif kind in {"compaction", "branch_summary", "custom_message"}:
                result.entries.append(
                    ExternalEntry(
                        "assistant",
                        text(row.get("summary", row.get("content", row))),
                        timestamp(row.get("timestamp"), result.updated_at),
                        row,
                        str(kind),
                    )
                )
            continue
        if "role" in row or isinstance(row.get("message"), dict):
            result.entries.append(conversation_entry(row, result.updated_at))


def _opencode(result: ExternalSession, data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ExternalHistoryError(
            "OpenCode imports require its JSON session export (info and messages)."
        )
    info = data.get("info", {})
    result.id = str(info.get("id", result.id))
    result.title = str(info.get("title", result.id))
    result.cwd = str(info.get("directory", ""))
    for row in data["messages"]:
        if not isinstance(row, dict):
            raise ExternalHistoryError(
                "OpenCode export contains a non-object message. Nothing was imported."
            )
        # v1 exports wrap info/parts; v2 exports typed messages with content.
        message = row.get("info", row)
        role = str(message.get("role", message.get("type", "assistant")))
        parts = row.get("parts", message.get("content", []))
        fragments = [text(message["text"])] if "text" in message else []
        kind = "message"
        for part in parts if isinstance(parts, list) else [parts]:
            if isinstance(part, dict) and part.get("type") in {"text", "reasoning"}:
                fragments.append(text(part.get("text")))
            else:
                fragments.append(text(part))
                if isinstance(part, dict) and part.get("type") == "tool":
                    kind = "tool_activity"
        if role not in {"user", "assistant"}:
            kind = "context"
            if not fragments:
                fragments.append(text(message))
        if message.get("files"):
            fragments.append("Source attachments: " + text(message["files"]))
        when = timestamp((message.get("time") or {}).get("created"), result.updated_at)
        result.entries.append(ExternalEntry(role, "\n\n".join(fragments), when, row, kind))
