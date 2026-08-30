"""End-to-end coverage for the one-time JSONL-to-SQLite Session conversion."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress
from scripts.converters import jsonl_to_sqlite as conversion
from scripts.converters.jsonl_sessions import LegacySession

main = conversion.main


def _write_session(path: Path, messages: list[ChatMessage], *, torn_tail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        json.dumps(message.to_dict(), ensure_ascii=False) + "\n" for message in messages
    )
    path.write_text(content + torn_tail, encoding="utf-8")


def test_conversion_preserves_live_and_archived_generations_at_one_address(
    tmp_path: Path,
) -> None:
    address = SessionAddress(None, "coder", "shared")
    live_message = ChatMessage.user("live")
    archived_message = ChatMessage.user("archived")
    _write_session(tmp_path / "agents" / "coder" / "sessions" / "shared.jsonl", [live_message])
    _write_session(
        tmp_path / "archive" / "sessions" / "agents" / "coder" / "shared.jsonl",
        [archived_message],
    )

    assert main([str(tmp_path)]) == 0

    manager = ChatSessionManager(tmp_path)
    try:
        assert manager.get(address).load() == [live_message]
    finally:
        manager.close()
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        statuses = [
            row[0]
            for row in connection.execute(
                "SELECT status FROM sessions WHERE agent_id = 'coder' "
                "AND session_id = 'shared' ORDER BY status"
            )
        ]
    assert statuses == ["archived", "live"]
    assert not (tmp_path / "agents" / "coder" / "sessions" / "shared.jsonl").exists()


def test_conversion_tolerates_only_an_incomplete_final_jsonl_record(tmp_path: Path) -> None:
    message = ChatMessage.user("complete")
    transcript = tmp_path / "agents" / "coder" / "sessions" / "session-one.jsonl"
    _write_session(transcript, [message], torn_tail='{"id":"partial"')

    assert main([str(tmp_path)]) == 0

    manager = ChatSessionManager(tmp_path)
    try:
        assert manager.get(SessionAddress(None, "coder", "session-one")).load() == [message]
    finally:
        manager.close()


def test_dry_run_validates_without_relocating_or_publishing(tmp_path: Path) -> None:
    transcript = tmp_path / "agents" / "coder" / "sessions" / "session-one.jsonl"
    _write_session(transcript, [ChatMessage.user("hello")])

    assert main([str(tmp_path), "--dry-run"]) == 0

    assert transcript.exists()
    assert not (tmp_path / "sessions.db").exists()
    assert not (tmp_path / "session-conversion.json").exists()


def test_conversion_refuses_to_publish_when_a_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "agents" / "coder" / "sessions" / "session-one.jsonl"
    _write_session(transcript, [ChatMessage.user("before")])
    original_import = conversion._import

    def import_then_change(path: Path, sources: list[LegacySession]) -> None:
        original_import(path, sources)
        with transcript.open("a", encoding="utf-8") as file:
            file.write(json.dumps(ChatMessage.user("after").to_dict()) + "\n")

    monkeypatch.setattr(conversion, "_import", import_then_change)

    with pytest.raises(RuntimeError, match="changed during offline conversion"):
        main([str(tmp_path)])

    assert not (tmp_path / "sessions.db").exists()
    assert (tmp_path / "session-conversion.json").exists()
