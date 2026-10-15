"""Older canonical histories become complete attachment reachability roots."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.chat.content_blocks import FileBlock
from core.chat.messages import ToolCall
from core.sessions import ChatSessionManager
from core.sessions._store_attachments import snapshot_references


@pytest.mark.parametrize("reference", ["file", "tool", "path"])
def test_old_history_backfill_and_old_snapshot_reads_preserve_attachment_handles(
    tmp_path: Path, reference: str
) -> None:
    attachment_id = "legacy_file" if reference == "file" else "att_sample123"
    manager = ChatSessionManager(tmp_path)
    try:
        session = manager.create("coder")
        if reference == "file":
            session.append(
                ChatMessage.user([FileBlock("file", attachment_id, "file.txt", "text/plain")])
            )
        elif reference == "tool":
            session.append(
                ChatMessage.assistant(
                    model="test", content=None, tool_calls=[ToolCall("call", "read", {})]
                )
            )
            session.append(
                ChatMessage.tool(
                    tool_call_id="call",
                    name="read",
                    content="result",
                    tool_media=[
                        {
                            "attachment_id": attachment_id,
                            "filename": "file.txt",
                            "media_type": "text/plain",
                        }
                    ],
                )
            )
        else:
            session.append(
                ChatMessage.user(f"Path: /data/artifacts/attachments/{attachment_id}.txt")
            )
    finally:
        manager.close()
    # Recreate the pre-index format in this disposable fixture only.
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute("DROP TABLE session_attachment_refs")
        connection.execute("DROP TABLE session_attachment_ids")
        connection.execute(
            "DELETE FROM kernel_migrations WHERE name = 'sessions.0005_attachment_references'"
        )
        assert snapshot_references(connection) == {attachment_id}
    reopened = ChatSessionManager(tmp_path)
    try:
        assert reopened.referenced_attachment_ids() == {attachment_id}
        reopened.delete(session.address)
        assert reopened.unreferenced_attachment_ids() == (attachment_id,)
    finally:
        reopened.close()
