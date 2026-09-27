"""Run controls over RPC: cancel, per-tool cancel, controls, Process cancel, Run result."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatMessage
from core.database import write_bootstrap_marker
from core.runs import ChatRunManager, Run
from core.sessions import ChatSessionManager, SessionAddress
from core.tools import ToolContext, tool_success
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RPC_ERROR_RUN_NOT_FOUND
from tests.core.sessions.history_fixtures import complete_run
from tests.server.rpc.chat_methods_test_support import call
from tests.server.rpc_test_support import JsonObject, StubAdapter, make_state

ADDRESS = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")


async def _hold_until_cancelled(_run: Run) -> str:
    await asyncio.Event().wait()
    return "done"


async def _held_run() -> tuple[SimpleNamespace, Run]:
    manager = ChatRunManager()
    run = await manager.start(ADDRESS, _hold_until_cancelled)
    await asyncio.sleep(0)
    return SimpleNamespace(chat_runs=manager), run


@pytest.mark.asyncio
async def test_cancel_stops_the_run_during_a_tool_and_ignores_its_late_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter(
        stream_deltas=[
            [
                {"type": "reasoning_delta", "text": "Need slow work."},
                {"type": "tool_call_delta", "id": "call_slow", "name_delta": "slow_tool"},
                {
                    "type": "tool_call_delta",
                    "id": "call_slow",
                    "arguments_delta": '{"value":"late"}',
                },
                {"type": "finish", "reason": "tool_calls"},
            ],
            [
                {"type": "content_delta", "text": "Should not be requested"},
                {"type": "finish", "reason": "stop"},
            ],
        ]
    )
    state = make_state(tmp_path, adapter)
    runtime = state.runtime
    slow_tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    tool_results: list[JsonObject] = []

    async def slow_tool(context: ToolContext, arguments: JsonObject) -> JsonObject:
        slow_tool_started.set()
        while not context.is_cancelled():
            await asyncio.sleep(0)
        await release_tool.wait()
        result = tool_success({"value": arguments["value"]})
        tool_results.append(result)
        return result

    runtime.tools.register("slow_tool", "Slow tool.", {"type": "object"}, slow_tool)
    runtime.chat_sessions.create("coder", session_id="session-one")
    stream_response = await call(
        state, "chat.stream", agent_id="coder", session_id="session-one", content="Start"
    )
    await slow_tool_started.wait()

    cancel_response = await call(state, "chat.cancel", run_id=stream_response["result"]["run_id"])
    release_tool.set()
    await asyncio.sleep(0)

    run = state.chat_runs.get(stream_response["result"]["run_id"])
    messages = runtime.chat_sessions.get(ADDRESS).load()
    assert cancel_response["result"]["status"] == "cancelled"
    assert [event.type for event in run.events if event.type != "provider_request_status"] == [
        "run_started",
        "user_message_persisted",
        "reasoning_delta",
        "tool_call_delta",
        "reasoning",
        "assistant_output",
        "model_step_usage",
        "tool_call_started",
        "run_cancelled",
    ]
    tool_delta = next(event for event in run.events if event.type == "tool_call_delta")
    assert tool_delta.payload == {
        "tool_call_id": "call_slow",
        "name_delta": "slow_tool",
        "arguments_delta": '{"value":"late"}',
    }
    assert [message.role for message in messages] == ["note", "user", "assistant", "run_summary"]
    assert str(messages[0].content).startswith('[reply-surface] {"kind":"webui"}')
    assert messages[-1].status == "cancelled"
    assert messages[-1].timing is not None
    assert tool_results == []
    assert len(adapter.stream_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("params", "reason"), [({"reason": "user"}, "user"), ({}, None)])
async def test_cancel_records_the_optional_reason_on_the_run(
    params: JsonObject, reason: str | None
) -> None:
    state, run = await _held_run()

    response = await call(state, "chat.cancel", run_id=run.id, **params)

    assert response["result"]["status"] == "cancelled"
    assert run.cancel_reason == reason


@pytest.mark.asyncio
async def test_cancel_tool_call_cancels_only_that_tool_call() -> None:
    state, run = await _held_run()
    aborted: list[str] = []
    run.register_tool_cancel("tool-1", lambda: aborted.append("tool-1"))
    try:
        response = await call(state, "chat.cancel_tool_call", run_id=run.id, tool_call_id="tool-1")

        assert response == {"ok": True, "result": {"ok": True}}
        assert aborted == ["tool-1"]
        assert run.tool_call_cancelled("tool-1") is True
        assert run.cancel_requested is False
    finally:
        run.request_cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_id", "tool_call_id", "missing"),
    [("missing-run", "tool-1", "missing-run"), (None, "tool-missing", "tool-missing")],
)
async def test_cancel_tool_call_reports_an_unknown_run_or_tool_call(
    run_id: str | None, tool_call_id: str, missing: str
) -> None:
    state, run = await _held_run()
    try:
        response = await call(
            state, "chat.cancel_tool_call", run_id=run_id or run.id, tool_call_id=tool_call_id
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_RUN_NOT_FOUND
        assert missing in response["error"]["message"]
        assert run.cancel_requested is False
    finally:
        run.request_cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "unsupported"),
    [
        ("chat.cancel", {"run_id": "any", "tool_call_id": "tool-1"}, "tool_call_id"),
        (
            "chat.cancel_tool_call",
            {"agent_id": "coder", "run_id": "any", "tool_call_id": "tool-1", "extra": True},
            "extra",
        ),
    ],
)
async def test_run_controls_reject_unsupported_params(
    method: str, params: JsonObject, unsupported: str
) -> None:
    response = await call(SimpleNamespace(), method, **params)

    assert response["ok"] is False
    assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST
    assert unsupported in response["error"]["message"]


@pytest.mark.asyncio
async def test_control_run_validates_the_full_address_and_returns_authoritative_state() -> None:
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
        assert (await call(state, "chat.control_run", **{**params, **changed}))["ok"] is False
    assert run.compaction_state == "idle"

    response = await call(state, "chat.control_run", **params)
    assert response["result"]["controls"]["compaction"] == "pending"

    run.begin_tool_call("call-one")
    handed_off: list[bool] = []

    def background() -> bool:
        handed_off.append(True)
        return True

    run.register_tool_background("call-one", background)
    background_params = {**params, "action": "background_tool", "tool_call_id": "call-one"}
    response = await call(state, "chat.control_run", **background_params)
    assert response["result"]["controls"]["background_tool_call_ids"] == []
    assert handed_off == [True]
    # A retired action and a cancelled Run offer no controls any more.
    assert (await call(state, "chat.control_run", **background_params))["ok"] is False
    run.request_cancel()
    assert (await call(state, "chat.control_run", **params))["ok"] is False


@pytest.mark.asyncio
async def test_cancel_process_cancels_as_the_user_for_the_addressed_agent() -> None:
    calls: list[tuple[str, str, str | None]] = []

    class RecordingProcessManager:
        async def cancel_for_user(
            self, process_id: str, agent_id: str, *, project_id: str | None = None
        ) -> Any:
            calls.append((process_id, agent_id, project_id))
            return SimpleNamespace(status="killed", cancelled_by_user=True)

    state = SimpleNamespace(runtime=SimpleNamespace(process_manager=RecordingProcessManager()))

    response = await call(
        state, "chat.cancel_process", agent_id="builder@project-one", process_id="process-one"
    )

    assert response == {"ok": True, "result": {"process_id": "process-one", "status": "cancelled"}}
    assert calls == [("process-one", "builder", "project-one")]


@pytest.mark.asyncio
async def test_run_result_reads_the_exact_run_after_the_session_continued(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    manager = ChatSessionManager(tmp_path)
    try:
        timing = {
            "started_at": "2026-09-11T10:00:00Z",
            "completed_at": "2026-09-11T10:00:01Z",
            "duration_ms": 1000,
        }
        session = manager.create("joel", project_id="project")
        for run_id, answer in (("first", "Which option?"), ("second", "Already continued")):
            session = session.start_run(run_id)
            session.append(ChatMessage.assistant(model="test/model", content=answer))
            complete_run(
                session,
                ChatMessage.run_summary(
                    run_id=run_id, status="completed", timing=timing, iteration_count=1
                ),
            )
        state = SimpleNamespace(runtime=SimpleNamespace(chat_sessions=manager))
        target = {"agent_id": "joel@project", "session_id": session.id, "run_id": "first"}

        response = await call(state, "chat.run_result", **target)
        missing = await call(state, "chat.run_result", **{**target, "run_id": "missing"})
        wrong_scope = await call(state, "chat.run_result", **{**target, "agent_id": "joel"})

        assert response["result"] == {
            "run_id": "first",
            "found": True,
            "content": "Which option?",
            "truncated": False,
        }
        assert missing["result"]["found"] is False
        assert wrong_scope["ok"] is False
    finally:
        manager.close()
