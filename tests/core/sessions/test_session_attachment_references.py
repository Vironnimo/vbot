"""Older canonical histories become complete attachment reachability roots."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.chat.content_blocks import FileBlock
from core.chat.messages import ToolCall
from core.runs import Run
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


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["removed", "started", "steered"])
async def test_accepted_input_attachments_remain_reachable_until_disposition_settles(
    manager, transition: str
) -> None:
    session = manager.create("coder")
    await manager.accept_accessor_input_async(
        session.address,
        "input",
        request_hash="original",
        payload={"content": "queued", "future": True},
        queue_id="queue",
        attachment_ids=("att_before",),
    )
    assert manager.referenced_attachment_ids() == {"att_before"}
    assert manager.unreferenced_attachment_ids() == ()
    edited = await manager.update_accessor_input_async(
        session.address,
        "input",
        payload_patch={"content": "edited"},
        attachment_ids=("att_after",),
    )
    assert edited.attachment_ids == ("att_after",)
    assert edited.payload["future"] is True
    with sqlite3.connect(manager._store.path) as connection:
        assert snapshot_references(connection) == {"att_after"}
    if transition == "removed":
        assert await manager.remove_accessor_input_async(session.address, "input")
        assert (await manager.lookup_accessor_input_async("input")).payload == {}
    elif transition == "started":
        run = Run(
            run_id="started",
            agent_id=session.address.agent_id,
            session_id=session.id,
            accessor_input_id="input",
        )
        await manager.start_run(run)
        # Run admission precedes its User carrier. This root must survive that gap.
        assert manager.referenced_attachment_ids() == {"att_after"}
        manager.recover_interrupted_runs()
    else:
        writer = session.start_run("steered")
        await writer.append_many_async(
            [ChatMessage.user([FileBlock("file", "att_after", "file.txt", "text/plain")])],
            accessor_input_ids=("input",),
        )
    assert (await manager.lookup_accessor_input_async("input")).attachment_ids == ()
    expected = {"att_after"} if transition == "steered" else set()
    assert manager.referenced_attachment_ids() == expected
    assert set(manager.unreferenced_attachment_ids()) == {"att_before", "att_after"} - expected
