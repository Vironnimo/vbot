"""External formats stay read-only and become inert conversation history."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from core.chat.imported_history import import_transition_note
from core.external_history import ExternalHistory, ExternalHistoryError
from core.external_history.history import import_metadata


@pytest.mark.parametrize("source", ["codex", "claude-code", "opencode", "hermes", "openclaw"])
def test_export_preserves_text_and_foreign_tool_payloads(tmp_path: Path, source: str) -> None:
    data: Any
    user = {"role": "user", "content": "Continue Zürich 東京"}
    call = {
        "role": "assistant",
        "content": [],
        "tool_calls": [
            {
                "id": "foreign-call",
                "function": {"name": "old_shell", "arguments": '{"cmd":"echo hi"}'},
            }
        ],
    }
    result = {
        "role": "tool",
        "name": "old_shell",
        "tool_call_id": "foreign-call",
        "content": "test-output",
    }
    if source == "codex":
        data = [
            {"type": "session_meta", "payload": {"id": "s", "cwd": "/old"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "duplicate"}},
            {"type": "response_item", "payload": {"type": "message", **user}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "old_shell",
                    "call_id": "foreign-call",
                    "arguments": '{"cmd":"echo hi"}',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "foreign-call",
                    "output": "test-output",
                },
            },
        ]
    elif source == "claude-code":
        data = [
            {"type": "user", "sessionId": "s", "message": user},
            {
                "type": "assistant",
                "sessionId": "s",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "old_shell",
                            "id": "foreign-call",
                            "input": {"cmd": "echo hi"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": "s",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "foreign-call",
                            "content": "test-output",
                        }
                    ],
                },
            },
        ]
    elif source == "openclaw":
        data = [
            {"type": "session", "id": "s"},
            *[{"type": "message", "message": message} for message in [user, call, result]],
        ]
    elif source == "opencode":
        data = {
            "info": {"id": "s"},
            "messages": [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": user["content"]}]},
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "old_shell",
                            "callID": "foreign-call",
                            "state": {"input": {"cmd": "echo hi"}, "output": "test-output"},
                        }
                    ],
                },
            ],
        }
    else:
        data = {"session": {"id": "s"}, "messages": [user, call, result]}
    path = tmp_path / ("source.jsonl" if isinstance(data, list) else "source.json")
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in data)
        if isinstance(data, list)
        else json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )
    before = path.read_bytes()
    reader = ExternalHistory()
    assert reader.list(source, path=str(path))["items"][0]["id"] == "s"
    session = reader.read(source, "s", path=str(path))
    messages = session.messages()
    assert "Zürich 東京" in str(messages[0].content)
    assert "test-output" in "\n".join(str(m.content) for m in messages)
    assert "old_shell" in "\n".join(str(m.content) for m in messages)
    assert all(
        m.role in {"user", "assistant"} and not m.tool_calls and not m.tool_call_id
        for m in messages
    )
    assert messages[-1].role == "assistant"
    assert all(m.external_history and m.external_history["raw"] for m in messages)
    assert import_transition_note(messages) is not None
    assert path.read_bytes() == before


@pytest.mark.parametrize("source", ["hermes", "opencode", "openclaw"])
def test_sqlite_sources_read_a_snapshot_without_writing(tmp_path: Path, source: str) -> None:
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as db:
        if source == "hermes":
            db.executescript(
                "CREATE TABLE sessions (id TEXT, parent_session_id TEXT, title TEXT); "
                "CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, "
                "content TEXT, display_identity BLOB);"
            )
            db.execute("INSERT INTO sessions VALUES ('s', NULL, 'title')")
            db.execute("INSERT INTO messages VALUES (1, 's', 'user', 'hello', ?)", (b"\xff\x00",))
        elif source == "opencode":
            db.executescript(
                "CREATE TABLE session_v2 (id TEXT, title TEXT); "
                "CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, "
                "seq INTEGER, time_created INTEGER, data TEXT); "
                "CREATE TABLE session_part (message_id TEXT, ordinal INTEGER, data TEXT);"
            )
            db.execute("INSERT INTO session_v2 VALUES ('s','title')")
            db.execute(
                "INSERT INTO session_message VALUES ('m','s','user',1,1700000000000,?)",
                (json.dumps({"text": "hello"}),),
            )
        else:
            db.executescript(
                "CREATE TABLE session_windows (session_id TEXT); "
                "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT); "
                "CREATE TABLE session_transcript_active_events "
                "(session_id TEXT, event_seq INTEGER, active_position INTEGER);"
            )
            db.execute("INSERT INTO session_windows VALUES ('s')")
            db.execute(
                "INSERT INTO transcript_events VALUES ('s',1,?)",
                (json.dumps({"type": "message", "message": {"role": "user", "content": "hello"}}),),
            )
            db.execute("INSERT INTO session_transcript_active_events VALUES ('s',1,0)")
    before = path.read_bytes()
    reader = ExternalHistory()
    assert reader.list(source, path=str(path))["items"][0]["id"] == "s"
    assert reader.read(source, "s", path=str(path)).entries[0].content == "hello"
    assert path.read_bytes() == before


def test_large_entries_page_without_losing_text_and_invalid_logs_refuse(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    content = "東京\n" * 4500
    path.write_text(
        json.dumps({"session": {"id": "s"}, "messages": [{"role": "user", "content": content}]}),
        encoding="utf-8",
    )
    reader = ExternalHistory()
    offset = char_offset = 0
    chunks: list[str] = []
    while True:
        page = reader.show("hermes", "s", path=str(path), offset=offset, char_offset=char_offset)
        chunks.extend(item["content"] for item in page["items"])
        assert sum(len(item["content"]) for item in page["items"]) <= 6000
        if page["next_offset"] is None:
            break
        offset, char_offset = page["next_offset"], page["next_char_offset"]
    assert "".join(chunks) == content
    invalid = tmp_path / "broken.jsonl"
    invalid.write_text(
        '{"type":"user","message":{"role":"user","content":"ok"}}\n{"incomplete":', encoding="utf-8"
    )
    with pytest.raises(ExternalHistoryError):
        reader.read("claude-code", "broken", path=str(invalid))


def test_discovery_does_not_read_plugin_caches(tmp_path: Path, deny_access) -> None:
    (tmp_path / "sessions").mkdir()
    (tmp_path / "plugins").mkdir()
    deny_access(tmp_path / "plugins")
    assert ExternalHistory().list("codex", path=str(tmp_path))["items"] == []


@pytest.mark.parametrize("parent_reason", ["reset", "branched", "compression"])
def test_hermes_respects_session_boundaries_and_display_history(
    tmp_path: Path, parent_reason: str
) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE sessions (id TEXT, parent_session_id TEXT, end_reason TEXT);"
            "CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT,"
            "active INTEGER, compacted INTEGER, display_order INTEGER, display_metadata TEXT,"
            "message_uid TEXT, timestamp INTEGER);"
        )
        db.execute("INSERT INTO sessions VALUES ('parent',NULL,?)", (parent_reason,))
        db.execute("INSERT INTO sessions VALUES ('s','parent',NULL)")
        db.executemany(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (1, "parent", "user", "old parent context", 1, 0, 0, None, None, 1),
                (2, "s", "user", "prompt", 0, 1, 0, None, None, 2),
                (3, "s", "assistant", "withdrawn answer", 0, 0, 1, None, None, 3),
                (4, "s", "assistant", "replacement", 1, 0, 1, None, None, 4),
                (5, "s", "tool", "complete result", 0, 1, 2, None, "t", 5),
                (6, "s", "user", "prompt", 1, 0, 0, None, None, 2),
                (7, "s", "tool", "shortened result", 1, 0, 3, None, "t", 5),
                (8, "s", "user", "synthetic merge", 1, 0, 4, '{"model_only":true}', None, 6),
            ],
        )
    reader = ExternalHistory()
    if parent_reason == "compression":
        with pytest.raises(ExternalHistoryError, match="complete Hermes conversation export"):
            reader.read("hermes", "s", path=str(path))
    else:
        entries = reader.read("hermes", "s", path=str(path)).entries
        assert [entry.content for entry in entries[:2]] == ["prompt", "replacement"]
        assert len(entries) == 3 and "complete result" in entries[2].content


@pytest.mark.parametrize(
    "tail",
    [
        {
            "type": "message",
            "id": "a2",
            "parentId": "u",
            "message": {"role": "assistant", "content": "alternate"},
        },
        {"type": "leaf", "id": "control", "parentId": "a", "targetId": "u"},
    ],
)
def test_openclaw_branch_logs_require_an_active_path(tmp_path: Path, tail: dict[str, Any]) -> None:
    path = tmp_path / "s.jsonl"
    rows = [
        {"type": "session", "id": "s"},
        {
            "type": "message",
            "id": "u",
            "parentId": None,
            "message": {"role": "user", "content": "prompt"},
        },
        {
            "type": "message",
            "id": "a",
            "parentId": "u",
            "message": {"role": "assistant", "content": "answer"},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert len(ExternalHistory().read("openclaw", "s", path=str(path)).entries) == 2
    label = {"type": "label", "id": "label", "parentId": "a", "targetId": "a", "label": "saved"}
    path.write_text("\n".join(json.dumps(row) for row in [*rows, label]), encoding="utf-8")
    assert len(ExternalHistory().read("openclaw", "s", path=str(path)).entries) == 2
    path.write_text("\n".join(json.dumps(row) for row in [*rows, tail]), encoding="utf-8")
    with pytest.raises(ExternalHistoryError, match="branch navigation"):
        ExternalHistory().read("openclaw", "s", path=str(path))
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE session_windows (session_id TEXT);"
            "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT);"
            "INSERT INTO session_windows VALUES ('s');"
        )
        db.executemany(
            "INSERT INTO transcript_events VALUES ('s',?,?)",
            [(i, json.dumps(row)) for i, row in enumerate([*rows, tail])],
        )
    with pytest.raises(ExternalHistoryError, match="branch navigation"):
        ExternalHistory().read("openclaw", "s", path=str(database))


def test_import_identity_depends_on_content_not_file_mtime(tmp_path: Path) -> None:
    path = tmp_path / "source.json"
    payload: dict[str, Any] = {
        "session": {"id": "s"},
        "messages": [{"role": "user", "content": "hello"}],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    reader = ExternalHistory()
    first = reader.read("hermes", "s", path=str(path))
    os.utime(path, (1700000000, 1700000000))
    second = reader.read("hermes", "s", path=str(path))
    assert first.entries[0].timestamp != second.entries[0].timestamp
    assert import_metadata(first)["digest"] == import_metadata(second)["digest"]
    payload["messages"][0]["content"] = "changed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        import_metadata(first)["digest"]
        != import_metadata(reader.read("hermes", "s", path=str(path)))["digest"]
    )
