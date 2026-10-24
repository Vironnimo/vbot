"""Collection retains durable owners and expires ten-day-old unused uploads."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import core.attachments._collection as collection
from core.attachments import AttachmentRecord, AttachmentStore
from core.chat import ChatMessage
from core.chat.content_blocks import FileBlock
from core.chat.messages import ToolCall
from core.database import create_data_snapshot
from core.runs import Run
from core.sessions import ChatSessionManager


@pytest.fixture
def sessions(tmp_path: Path, current_format_data_directory: None) -> Iterator[ChatSessionManager]:
    manager = ChatSessionManager(tmp_path)
    yield manager
    manager.close()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    now = datetime(2026, 10, 25, tzinfo=UTC)
    monkeypatch.setattr(collection, "utc_now_timestamp", lambda: now.isoformat())
    return now


def _stored_at(record: AttachmentRecord, timestamp: str) -> AttachmentRecord:
    sidecar = Path(record.file_path).with_suffix(".json")
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    metadata["stored_at"] = timestamp
    sidecar.write_text(json.dumps(metadata), encoding="utf-8", newline="\n")
    return replace(record, stored_at=timestamp)


def _forget_prior_ownership(tmp_path: Path, attachment_id: str) -> None:
    # Simulate historical files whose former ownership ledger is absent.
    # Current and snapshot references must protect them independently.
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute(
            "DELETE FROM session_attachment_ids WHERE attachment_id = ?", (attachment_id,)
        )


@pytest.mark.parametrize(
    ("age", "expired"),
    [
        (timedelta(days=10) - timedelta(microseconds=1), False),
        (timedelta(days=10), True),
        (timedelta(days=10) + timedelta(microseconds=1), True),
        (None, False),
    ],
    ids=["fresh", "at-cutoff", "expired", "invalid-timestamp"],
)
def test_unused_uploads_expire_only_with_a_valid_ten_day_old_timestamp(
    tmp_path: Path,
    sessions: ChatSessionManager,
    clock: datetime,
    age: timedelta | None,
    expired: bool,
) -> None:
    store = AttachmentStore(tmp_path)
    record = _stored_at(
        store.store("draft.txt", b"unsubmitted draft"),
        (clock - age).isoformat() if age is not None else "not a timestamp",
    )
    assert sessions.unreferenced_attachment_ids() == ()
    assert store.collect_session_orphans(sessions) == int(expired)
    assert Path(record.file_path).exists() is not expired
    assert Path(record.file_path).with_suffix(".json").exists() is not expired


@pytest.mark.parametrize("reference", ["file", "tool", "path"])
def test_previously_admitted_blobs_are_reclaimed_without_expiring_fresh_uploads(
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


@pytest.mark.parametrize("root", ["fork", "archive", "audit", "snapshot", "queued", "started"])
@pytest.mark.asyncio
async def test_retained_history_roots_keep_their_attachments(
    tmp_path: Path, sessions: ChatSessionManager, root: str, clock: datetime
) -> None:
    store = AttachmentStore(tmp_path)
    record = _stored_at(
        store.store("file.txt", b"content"), (clock - timedelta(days=20)).isoformat()
    )
    source = sessions.create("coder")
    if root in {"queued", "started"}:
        await sessions.accept_accessor_input_async(
            source.address,
            "input",
            request_hash="original",
            payload={"content": "queued"},
            queue_id="queue",
            attachment_ids=(record.id,),
        )
        if root == "started":
            await sessions.start_run(
                Run(
                    run_id="started",
                    agent_id=source.address.agent_id,
                    session_id=source.id,
                    accessor_input_id="input",
                )
            )
        _forget_prior_ownership(tmp_path, record.id)
        assert store.collect_session_orphans(sessions) == 0
        assert store.get(record.id) == record
        return
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
        _forget_prior_ownership(tmp_path, record.id)
        assert record.id in sessions.referenced_attachment_ids()
        assert store.collect_session_orphans(sessions) == 0
        sessions.delete(fork.address)
        assert store.collect_session_orphans(sessions) == 1
    elif root == "archive":
        await sessions.archive(source.address)
        _forget_prior_ownership(tmp_path, record.id)
        assert store.collect_session_orphans(sessions) == 0
        assert store.get(record.id) == record
    elif root == "audit":
        source.apply_edit(source.load()[0].id, [ChatMessage.user("replacement")])
        _forget_prior_ownership(tmp_path, record.id)
        assert store.collect_session_orphans(sessions) == 0
        assert store.get(record.id) == record
    else:
        snapshot = create_data_snapshot(tmp_path, reason="test", databases=[sessions.database])
        assert snapshot is not None
        sessions.delete(source.address)
        _forget_prior_ownership(tmp_path, record.id)
        # An unrelated deletion remains collectible despite the retained snapshot.
        unrelated = store.store("unrelated.txt", b"unrelated")
        temporary = sessions.create("coder")
        temporary.append(ChatMessage.user(f"Path: {unrelated.file_path}"))
        sessions.delete(temporary.address)
        assert store.collect_session_orphans(sessions) == 1
        assert store.get(record.id) == record
        assert not Path(unrelated.file_path).exists()


@pytest.mark.parametrize("admitted", [True, False], ids=["orphan", "unused-upload"])
@pytest.mark.parametrize(
    "failure",
    [
        "invalid_sidecar",
        "unreadable_sidecar",
        "unreadable_snapshot",
        "partial_unlink",
        "linked_sidecar",
        "linked_blob",
        "redirected_root",
    ],
)
def test_uncertain_ownership_keeps_blobs_and_partial_collection_can_retry(
    tmp_path: Path,
    sessions: ChatSessionManager,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    admitted: bool,
    clock: datetime,
    deny_access: Callable[[Path], None],
) -> None:
    store = AttachmentStore(tmp_path)
    record = _stored_at(
        store.store("file.txt", b"content"), (clock - timedelta(days=20)).isoformat()
    )
    if admitted:
        session = sessions.create("coder")
        session.append(ChatMessage.user(f"Path: {record.file_path}"))
        sessions.delete(session.address)
    sidecar = Path(record.file_path).with_suffix(".json")
    if failure == "invalid_sidecar":
        sidecar.write_text(json.dumps({"invalid": True}), encoding="utf-8")
    elif failure == "unreadable_sidecar":
        deny_access(sidecar)
    elif failure == "unreadable_snapshot":
        snapshot = tmp_path / "snapshots" / "broken"
        snapshot.mkdir(parents=True)
    elif failure == "partial_unlink":
        unlink = Path.unlink

        def fail_sidecar(path: Path, missing_ok: bool = False) -> None:
            if path == sidecar:
                raise PermissionError("fixture denies sidecar removal")
            unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_sidecar)
    else:
        redirected = {
            "linked_sidecar": sidecar,
            "linked_blob": Path(record.file_path),
            "redirected_root": sidecar.parent,
        }[failure]
        actual_status = collection.stat_or_none

        def link_status(path: Path, *, follow_symlinks: bool = True) -> os.stat_result | None:
            status = actual_status(path, follow_symlinks=follow_symlinks)
            if path == redirected and status is not None:
                return os.stat_result((stat.S_IFLNK, *status[1:]))
            return status

        monkeypatch.setattr(collection, "stat_or_none", link_status)
    assert store.collect_session_orphans(sessions) == 0
    if admitted and failure != "unreadable_snapshot":
        assert record.id in sessions.unreferenced_attachment_ids()
    if failure == "partial_unlink":
        assert not Path(record.file_path).exists()
        monkeypatch.setattr(Path, "unlink", unlink)
        assert store.collect_session_orphans(sessions) == 1
        assert not sidecar.exists()
    else:
        assert Path(record.file_path).read_bytes() == b"content"
