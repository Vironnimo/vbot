"""Tests for chat methods."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
    ReplySurface,
)
from core.chat.content_blocks import FileMentionBlock, TextBlock
from core.runs import ActiveRunError
from core.tools.file_state import FileReadState
from server.rpc.chat_methods import (
    _control_run_chat,
    _send_chat,
    _stream_chat,
)
from server.rpc.errors import RpcError
from tests.server.rpc.chat_methods_test_support import (
    _NoCommandDispatcher,
    _RecordingLoop,
)


@pytest.mark.asyncio
async def test_run_result_keeps_exact_scope_when_session_has_continued(tmp_path):
    from core.sessions import ChatSessionManager
    from core.sessions.format import write_bootstrap_marker
    from server.rpc.chat_methods import _chat_run_result

    write_bootstrap_marker(tmp_path)
    manager = ChatSessionManager(tmp_path)
    try:
        timing = {
            "started_at": "2026-09-11T10:00:00Z",
            "completed_at": "2026-09-11T10:00:01Z",
            "duration_ms": 1000,
        }
        session = manager.create("joel", project_id="project")
        session.append(ChatMessage.assistant(model="test/model", content="Which option?"))
        session.append(
            ChatMessage.run_summary(
                run_id="first", status="completed", timing=timing, iteration_count=1
            )
        )
        session.append(ChatMessage.assistant(model="test/model", content="Already continued"))
        session.append(
            ChatMessage.run_summary(
                run_id="second", status="completed", timing=timing, iteration_count=1
            )
        )
        state = SimpleNamespace(runtime=SimpleNamespace(chat_sessions=manager))
        target = {
            "agent_id": "joel@project",
            "session_id": session.address.session_id,
            "run_id": "first",
        }
        result = await _chat_run_result(state, target)
        assert result == {
            "run_id": "first",
            "found": True,
            "content": "Which option?",
            "truncated": False,
        }
        assert not (await _chat_run_result(state, {**target, "run_id": "missing"}))["found"]
        with pytest.raises(RpcError):
            await _chat_run_result(state, {**target, "agent_id": "joel"})
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_controls_validate_full_address_and_return_authoritative_state() -> None:
    from core.runs import Run

    run = Run(run_id="run-control", agent_id="builder", project_id="vbot", session_id="s1")
    run.set_compaction_state("idle")
    state = SimpleNamespace(chat_runs=SimpleNamespace(get=lambda _id: run))
    params = {"agent_id": "builder@vbot", "session_id": "s1", "run_id": run.id, "action": "compact"}
    for changed in (
        {"agent_id": "builder"},
        {"session_id": "other"},
        {"action": "unknown"},
        {"tool_call_id": "unexpected"},
    ):
        with pytest.raises(RpcError):
            await _control_run_chat(state, {**params, **changed})
    assert run.compaction_state == "idle"
    response = await _control_run_chat(state, params)
    assert response["controls"]["compaction"] == "pending"
    run.begin_tool_call("call-one")
    handed_off = []

    def background():
        handed_off.append(True)
        return True

    run.register_tool_background("call-one", background)
    background_params = {**params, "action": "background_tool", "tool_call_id": "call-one"}
    response = await _control_run_chat(state, background_params)
    assert response["controls"]["background_tool_call_ids"] == []
    assert handed_off == [True]
    with pytest.raises(RpcError):
        await _control_run_chat(state, background_params)
    run.request_cancel()
    with pytest.raises(RpcError):
        await _control_run_chat(state, params)


def _make_state(loop: _RecordingLoop) -> SimpleNamespace:
    # The bridge helper reads the event bus; a no-op namespace is enough since the
    # tests assert on the recorded loop call, not on bridged events.
    event_bus = SimpleNamespace(publish=lambda *a, **k: None)
    runtime = SimpleNamespace()
    return SimpleNamespace(
        chat_loop=loop,
        streaming_chat_loop=loop,
        runtime=runtime,
        event_bus=event_bus,
        chat_runs=SimpleNamespace(),
        command_dispatcher=_NoCommandDispatcher(),
    )


@pytest.mark.asyncio
async def test_send_bare_agent_runs_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["agent_id"] == "builder"
    assert loop.start_calls[0]["project_id"] is None
    assert loop.start_calls[0]["reply_surface"] == ReplySurface.webui()


@pytest.mark.asyncio
async def test_send_marks_identity_session_as_current(monkeypatch: pytest.MonkeyPatch) -> None:
    # A user message re-aims the identity agent's current-session pointer, so a
    # restart re-opens the session the user last wrote to. The agents channel
    # notifies other windows of the new current marking.
    loop = _RecordingLoop()
    state = _make_state(loop)
    updates: list[tuple[str, str]] = []
    published: list[str] = []
    state.runtime.agents = SimpleNamespace(
        get=lambda agent_id: SimpleNamespace(current_session_id=""),
        update=lambda agent_id, **kwargs: updates.append((agent_id, kwargs["current_session_id"])),
    )
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)
    monkeypatch.setattr(
        "server.rpc.chat_methods.publish_resource_changed",
        lambda _state, kind, **kwargs: published.append(kind),
    )

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert updates == [("builder", "s1")]
    assert published == ["agents"]


@pytest.mark.asyncio
async def test_send_skips_current_session_mark_when_already_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the session is already the agent's current one, no update or
    # resource_changed signal is emitted — re-marking the same session on
    # every message would tear down the chat view in every connected window.
    loop = _RecordingLoop()
    state = _make_state(loop)
    state.runtime.agents = SimpleNamespace(
        get=lambda agent_id: SimpleNamespace(current_session_id="s1"),
        update=lambda *a, **k: pytest.fail("must not update"),
    )
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)
    monkeypatch.setattr(
        "server.rpc.chat_methods.publish_resource_changed",
        lambda *a, **k: pytest.fail("must not publish"),
    )

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})


@pytest.mark.asyncio
async def test_send_does_not_mark_project_agent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A project (config) agent has no anchor-level current pointer; sending to
    # one must not attempt an agent update.
    loop = _RecordingLoop()
    state = _make_state(loop)
    state.runtime.agents = SimpleNamespace(update=lambda *a, **k: pytest.fail("must not update"))
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder@vbot", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["project_id"] == "vbot"


@pytest.mark.asyncio
async def test_send_marks_session_before_enqueue_when_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pointer must move even when the session is busy and the message is
    # queued: the user still wrote to that session.
    loop = _RecordingLoop()
    loop.start_error = ActiveRunError("busy")
    state = _make_state(loop)
    updates: list[tuple[str, str]] = []
    state.runtime.agents = SimpleNamespace(
        get=lambda agent_id: SimpleNamespace(current_session_id=""),
        update=lambda agent_id, **kwargs: updates.append((agent_id, kwargs["current_session_id"])),
    )
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)
    monkeypatch.setattr(
        "server.rpc.chat_methods._bridge_queued_item_to_event_bus", lambda *a, **k: None
    )
    monkeypatch.setattr("server.rpc.chat_methods._publish_queue_changed", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert updates == [("builder", "s1")]


@pytest.mark.asyncio
async def test_send_qualified_agent_runs_project_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder@vbot", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["agent_id"] == "builder"
    assert loop.start_calls[0]["project_id"] == "vbot"


@pytest.mark.asyncio
async def test_send_expands_file_mentions_into_snapshot_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # An @-mentioned file is snapshotted before the loop sees the content: the
    # string message becomes blocks (original text first, then the snapshot),
    # and the file is stamped as read for the session.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.md").write_text("snapshot body", encoding="utf-8")
    file_state = FileReadState()
    loop = _RecordingLoop()
    state = _make_state(loop)
    state.runtime = SimpleNamespace(
        projects=SimpleNamespace(get=lambda project_id: SimpleNamespace(cwd="")),
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: SimpleNamespace(workspace=str(workspace))
        ),
        storage=SimpleNamespace(data_dir=str(tmp_path)),
        file_read_state=file_state,
    )
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(
        state,
        {
            "agent_id": "builder",
            "session_id": "s1",
            "content": "look at @notes.md",
            "file_mentions": ["notes.md"],
        },
    )

    content = loop.start_calls[0]["content"]
    assert isinstance(content, list)
    assert content[0] == TextBlock(type="text", text="look at @notes.md")
    assert isinstance(content[1], FileMentionBlock)
    assert content[1].status == "inlined"
    assert content[1].text == "snapshot body"
    assert file_state.check_stale("s1", (workspace / "notes.md").resolve()) is None


@pytest.mark.asyncio
async def test_send_without_file_mentions_keeps_string_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["content"] == "hi"


@pytest.mark.asyncio
async def test_send_invalid_address_is_invalid_request() -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)

    with pytest.raises(RpcError) as exc_info:
        await _send_chat(
            state, {"agent_id": "builder@bad project", "session_id": "s1", "content": "hi"}
        )

    assert exc_info.value.code == "invalid_request"
    assert loop.start_calls == []


@pytest.mark.asyncio
async def test_stream_qualified_agent_runs_project_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _stream_chat(state, {"agent_id": "tester@vbot", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["agent_id"] == "tester"
    assert loop.start_calls[0]["project_id"] == "vbot"
    assert loop.start_calls[0]["reply_surface"] == ReplySurface.webui()
