"""Scratch announcements at Chat's Run and Compaction boundaries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat._message_history import effective_compaction_messages
from core.chat._scratch_context import SCRATCH_ANNOUNCED_META_KEY, SCRATCH_NOTE_PREFIX
from core.runs import RunCancelledError, RunInterruptedError, RunKind, RunStatus
from core.sessions import ChatSession, SessionAddress
from core.tools import ToolRegistry, tool_success
from core.tools.scratch_areas import ScratchAreas
from tests.core.chat.chat_loop_compaction_test_support import auto_compact, compaction_runtime
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubRuntime,
    build_chat_loop,
)


class _ScratchFolders:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[SessionAddress] = []
        self.incarnation = "initial"
        self.recreated = False

    async def folder_for(self, address: SessionAddress) -> Any:
        self.calls.append(address)
        area = self.root / address.session_id
        return SimpleNamespace(
            own=area / address.session_id,
            area=area,
            tmp=area / address.session_id / "tmp",
            recreated=self.recreated,
            incarnation=self.incarnation,
        )


def _tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register("bash", "Execute a command.", {"type": "object"}, lambda *_: tool_success({}))
    return tools


def _runtime(tmp_path: Path, *, allowed_tools: list[str] | None = None) -> Any:
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=allowed_tools),
        adapter=StubAdapter([{"content": "Done", "tool_calls": None}] * 10),
        tools=_tools(),
    )
    runtime.scratch_areas = _ScratchFolders(tmp_path / "scratch")
    return runtime


def _notes(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [
        message
        for message in messages
        if message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(SCRATCH_NOTE_PREFIX)
    ]


@pytest.mark.asyncio
async def test_scratch_announces_once_and_forks_announce_their_own_folder(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="source")
    loop = build_chat_loop(runtime)
    for _ in range(2):
        run = await loop.start_run("coder", "Work", session_id=session.id)
        await run.wait()
    [note] = _notes(session.load_active())
    record = runtime.chat_sessions.metadata_value(session.address, SCRATCH_ANNOUNCED_META_KEY)
    assert record["note_id"] == note.id
    assert record["incarnation"] == "initial"
    assert isinstance(note.content, str)
    assert Path(record["own"]).as_posix() in note.content
    assert len(runtime.scratch_areas.calls) == 2
    assert all(
        SCRATCH_NOTE_PREFIX not in str(request["messages"][0]["content"])
        for request in runtime.adapter.requests
    )
    assert SCRATCH_NOTE_PREFIX in str(runtime.adapter.requests[0]["messages"])

    fork = await runtime.chat_sessions.fork(session.address)
    assert runtime.chat_sessions.metadata_value(fork.address, SCRATCH_ANNOUNCED_META_KEY) is None
    run = await loop.start_run("coder", "Continue", session_id=fork.id)
    await run.wait()
    [fork_note] = _notes(fork.load())
    assert isinstance(fork_note.content, str)
    assert fork.id in fork_note.content
    assert fork_note.id != note.id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["folder", "persistence", "request_rebuild"])
async def test_optional_scratch_failure_keeps_the_run_working_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_stage: str,
) -> None:
    runtime = _runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="source")
    unavailable = False
    generation = "initial"

    def identity(_address: SessionAddress) -> str:
        if unavailable and failure_stage == "folder":
            raise RuntimeError("private Scratch failure details")
        return generation

    runtime.scratch_areas = ScratchAreas(
        tmp_path, identity=identity, ancestors=lambda _: (), busy_sessions=lambda: ()
    )
    loop = build_chat_loop(runtime)
    append = ChatSession.append_many_async
    rebuild = loop._requests.rebuild_live_request_state

    async def append_notes(current: ChatSession, messages: list[ChatMessage], **kwargs: Any) -> Any:
        if unavailable and failure_stage == "persistence" and _notes(messages):
            raise RuntimeError("private Scratch failure details")
        return await append(current, messages, **kwargs)

    async def rebuild_request(*args: Any, **kwargs: Any) -> Any:
        if unavailable and failure_stage == "request_rebuild":
            raise RuntimeError("private Scratch failure details")
        return await rebuild(*args, **kwargs)

    monkeypatch.setattr(ChatSession, "append_many_async", append_notes)
    monkeypatch.setattr(loop._requests, "rebuild_live_request_state", rebuild_request)
    caplog.set_level(logging.INFO, logger="vbot")
    run = await loop.start_run("coder", "First task", session_id=session.id)
    await run.wait()
    initial = runtime.chat_sessions.metadata_value(session.address, SCRATCH_ANNOUNCED_META_KEY)
    caplog.clear()

    generation = "replacement"
    unavailable = True
    run = await loop.start_run("coder", "Continue the task", session_id=session.id)
    await run.wait()
    assert run.status is RunStatus.COMPLETED
    assert len(runtime.adapter.requests) == 2
    assert session.find_run_summary(run_id=run.id).status == "completed"
    if failure_stage != "request_rebuild":
        assert (
            runtime.chat_sessions.metadata_value(session.address, SCRATCH_ANNOUNCED_META_KEY)
            == initial
        )
    assert sum(record.levelno == logging.WARNING for record in caplog.records) == 1
    assert "private Scratch failure details" not in caplog.text

    unavailable = False
    run = await loop.start_run("coder", "Finish the task", session_id=session.id)
    await run.wait()
    assert run.status is RunStatus.COMPLETED
    record = runtime.chat_sessions.metadata_value(session.address, SCRATCH_ANNOUNCED_META_KEY)
    notes = _notes(session.load_active())
    assert len(notes) == 2
    assert record["note_id"] == notes[-1].id
    assert record["own"] != initial["own"]
    assert Path(record["own"]).is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_optional_scratch_does_not_swallow_run_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    runtime = _runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="source")

    async def cancel(_address: SessionAddress) -> None:
        if interrupted:
            raise RunInterruptedError("fixture-interruption")
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime.scratch_areas, "folder_for", cancel)
    run = await build_chat_loop(runtime).start_run("coder", "Work", session_id=session.id)
    with pytest.raises(RunInterruptedError if interrupted else RunCancelledError):
        await run.wait()
    assert run.status is (RunStatus.INTERRUPTED if interrupted else RunStatus.CANCELLED)
    assert not runtime.adapter.requests
    assert not _notes(session.load())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["recreated", "intervening_launch", "location"])
async def test_scratch_announces_a_changed_folder_even_when_old_files_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    runtime = _runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="source")
    loop = build_chat_loop(runtime)
    run = await loop.start_run("coder", "Work", session_id=session.id)
    await run.wait()
    initial = runtime.chat_sessions.metadata_value(session.address, SCRATCH_ANNOUNCED_META_KEY)
    surviving_file: Path | None = None
    if change == "location":
        # Archiving an intermediate Parent can change this Session's area while
        # leaving the old root's area, and its files, intact.
        surviving_file = Path(initial["own"]) / "earlier.txt"
        surviving_file.parent.mkdir(parents=True)
        surviving_file.write_text("earlier work", encoding="utf-8")
        runtime.scratch_areas.root = tmp_path / "replacement-area"
    else:
        runtime.scratch_areas.recreated = change == "recreated"
        runtime.scratch_areas.incarnation = "replacement"
    cleared_values: list[bool] = []
    from core.chat._run_execution import scratch_note

    def recording_note(folder: Any, *, cleared: bool, process_tools: bool) -> ChatMessage:
        cleared_values.append(cleared)
        return scratch_note(folder, cleared=cleared, process_tools=process_tools)

    monkeypatch.setattr("core.chat._run_execution.scratch_note", recording_note)
    run = await loop.start_run("coder", "Continue", session_id=session.id)
    await run.wait()
    assert cleared_values == [True]
    assert len(_notes(session.load())) == 2
    current = runtime.chat_sessions.metadata_value(session.address, SCRATCH_ANNOUNCED_META_KEY)
    assert current["incarnation"] == runtime.scratch_areas.incarnation
    if surviving_file is not None:
        assert current["own"] != initial["own"]
        assert surviving_file.read_text(encoding="utf-8") == "earlier work"
        latest_content = _notes(session.load())[-1].content
        assert isinstance(latest_content, str)
        assert Path(current["own"]).as_posix() in latest_content


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(RunKind))
async def test_scratch_is_only_announced_for_ordinary_file_capable_runs(
    tmp_path: Path, kind: RunKind
) -> None:
    runtime = _runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="source")
    run = await build_chat_loop(runtime).start_run(
        "coder", "Work", session_id=session.id, run_kind=kind
    )
    await run.wait()
    excluded = {
        RunKind.REFLECTION,
        RunKind.MEMORY_REFLECTION,
        RunKind.SKILL_REFLECTION,
        RunKind.LIBRARIAN,
        RunKind.LIVE,
    }
    assert bool(_notes(session.load())) is (kind not in excluded)
    assert bool(runtime.scratch_areas.calls) is (kind not in excluded)


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["agent", "restriction", "live"])
async def test_scratch_is_not_created_without_file_tools(tmp_path: Path, denial: str) -> None:
    runtime = _runtime(tmp_path, allowed_tools=[] if denial == "agent" else None)
    session = runtime.chat_sessions.create("coder", session_id="source")
    run = await build_chat_loop(runtime).start_run(
        "coder",
        "Work",
        session_id=session.id,
        tool_restriction=[] if denial == "restriction" else None,
        tool_denial_resolver=(lambda _: "blocked") if denial == "live" else None,
    )
    await run.wait()
    assert not _notes(session.load())
    assert not runtime.scratch_areas.calls


@pytest.mark.asyncio
async def test_scratch_announces_when_file_tools_become_available_mid_epoch(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, allowed_tools=[])
    session = runtime.chat_sessions.create("coder", session_id="source")
    loop = build_chat_loop(runtime)
    run = await loop.start_run("coder", "Talk", session_id=session.id)
    await run.wait()
    assert not _notes(session.load())
    runtime.agents._agent = replace(runtime.agents.get("coder"), allowed_tools=["bash"])
    run = await loop.start_run("coder", "Work", session_id=session.id)
    await run.wait()
    assert len(_notes(session.load())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("retain_note", [False, True])
async def test_compaction_preserves_exactly_one_current_scratch_announcement(
    tmp_path: Path, retain_note: bool
) -> None:
    runtime = compaction_runtime(tmp_path, tools=_tools())
    session = runtime.chat_sessions.create("coder", session_id="source")
    note = ChatMessage.note(SCRATCH_NOTE_PREFIX + "scratch contract sentinel")
    session.append_many(
        [
            note,
            ChatMessage.user("Work"),
            ChatMessage.assistant(model="openai/gpt-5.2", content="Done"),
        ]
    )
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Summary", projection=[note] if retain_note else [], compacted_token_count=10
    )
    loop = build_chat_loop(
        runtime, compaction_service=StubCompactionService(should_auto=True, checkpoint=checkpoint)
    )
    await auto_compact(loop, runtime.agents.get("coder"), session, usage={"input_tokens": 90})
    [preserved] = _notes(effective_compaction_messages(session.load_active()))
    assert preserved.id == note.id
    assert preserved.content == note.content


@pytest.mark.asyncio
async def test_run_reannounces_when_a_checkpoint_omitted_the_scratch_note(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="source")
    loop = build_chat_loop(runtime)
    run = await loop.start_run("coder", "Work", session_id=session.id)
    await run.wait()
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Summary", projection=[], compacted_token_count=10
        )
    )
    run = await loop.start_run("coder", "Continue", session_id=session.id)
    await run.wait()
    assert len(_notes(session.load())) == 2
    assert len(_notes(effective_compaction_messages(session.load_active()))) == 1
