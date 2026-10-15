"""Collection retains durable owners and only reclaims previously admitted blobs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.attachments import AttachmentStore
from core.chat import ChatMessage
from core.chat.content_blocks import FileBlock
from core.chat.messages import ToolCall
from core.database import create_data_snapshot
from core.sessions import ChatSessionManager


@pytest.fixture
def sessions(tmp_path: Path, current_format_data_directory: None) -> Iterator[ChatSessionManager]:
    manager = ChatSessionManager(tmp_path)
    yield manager
    manager.close()


@pytest.mark.parametrize("reference", ["file", "tool", "path"])
def test_only_previously_admitted_blobs_become_collection_candidates(
    tmp_path: Path, sessions: ChatSessionManager, reference: str
) -> None:
    store = AttachmentStore(tmp_path)
    admitted = store.store("admitted.txt", b"admitted")
    pending = store.store("pending.txt", b"not admitted")
    session = sessions.create("coder")
    if reference == "file":
        session.append(
            ChatMessage.user(
                [FileBlock("file", admitted.id, admitted.filename, admitted.media_type)]
            )
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
                content="image",
                tool_media=[
                    {
                        "attachment_id": admitted.id,
                        "filename": admitted.filename,
                        "media_type": admitted.media_type,
                    }
                ],
            )
        )
    else:
        session.append(ChatMessage.user(f"Please read {admitted.file_path}"))
    assert store.collect_session_orphans(sessions) == 0
    sessions.delete(session.address)
    assert store.collect_session_orphans(sessions) == 1
    assert not Path(admitted.file_path).exists()
    assert store.get(pending.id) == pending
    assert sessions.unreferenced_attachment_ids() == ()


@pytest.mark.parametrize("root", ["fork", "archive", "audit", "snapshot"])
@pytest.mark.asyncio
async def test_retained_history_roots_keep_their_attachments(
    tmp_path: Path, sessions: ChatSessionManager, root: str
) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("file.txt", b"content")
    source = sessions.create("coder")
    source.append(
        ChatMessage.user(
            f"Path: {record.file_path}"
            if root == "audit"
            else [FileBlock("file", record.id, record.filename, record.media_type)]
        )
    )
    if root == "fork":
        fork = await sessions.fork(source.address)
        sessions.delete(source.address)
        assert record.id in sessions.referenced_attachment_ids()
        assert store.collect_session_orphans(sessions) == 0
        sessions.delete(fork.address)
        assert store.collect_session_orphans(sessions) == 1
    elif root == "archive":
        await sessions.archive(source.address)
        assert store.collect_session_orphans(sessions) == 0
        assert store.get(record.id) == record
    elif root == "audit":
        source.apply_edit(source.load()[0].id, [ChatMessage.user("replacement")])
        assert store.collect_session_orphans(sessions) == 0
        assert store.get(record.id) == record
    else:
        snapshot = create_data_snapshot(tmp_path, reason="test", databases=[sessions.database])
        assert snapshot is not None
        sessions.delete(source.address)
        # An unrelated deletion remains collectible despite the retained snapshot.
        unrelated = store.store("unrelated.txt", b"unrelated")
        temporary = sessions.create("coder")
        temporary.append(ChatMessage.user(f"Path: {unrelated.file_path}"))
        sessions.delete(temporary.address)
        assert store.collect_session_orphans(sessions) == 1
        assert store.get(record.id) == record
        assert not Path(unrelated.file_path).exists()


@pytest.mark.parametrize("failure", ["invalid_sidecar", "unreadable_snapshot", "partial_unlink"])
def test_uncertain_ownership_keeps_blobs_and_partial_collection_can_retry(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("file.txt", b"content")
    session = sessions.create("coder")
    session.append(ChatMessage.user(f"Path: {record.file_path}"))
    sessions.delete(session.address)
    sidecar = Path(record.file_path).with_suffix(".json")
    if failure == "invalid_sidecar":
        sidecar.write_text(json.dumps({"invalid": True}), encoding="utf-8")
    elif failure == "unreadable_snapshot":
        snapshot = tmp_path / "snapshots" / "broken"
        snapshot.mkdir(parents=True)
    else:
        unlink = Path.unlink

        def fail_sidecar(path: Path, missing_ok: bool = False) -> None:
            if path == sidecar:
                raise PermissionError("fixture denies sidecar removal")
            unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_sidecar)
    assert store.collect_session_orphans(sessions) == 0
    if failure != "unreadable_snapshot":
        assert record.id in sessions.unreferenced_attachment_ids()
    if failure == "partial_unlink":
        assert not Path(record.file_path).exists()
        monkeypatch.undo()
        assert store.collect_session_orphans(sessions) == 1
        assert not sidecar.exists()
    else:
        assert Path(record.file_path).read_bytes() == b"content"
