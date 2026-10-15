"""Run controls over RPC: cancel, stop all, tool cancel, controls, Process cancel, Run result."""

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
from core.tools.process_tree import RunningProcess
from server.file_delivery import FileDelivery
from server.rpc.errors import (
    RPC_ERROR_COMMAND_NOT_FOUND,
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_RUN_NOT_FOUND,
)
from tests.core.runs.runs_test_support import RunTimelines
from tests.core.sessions.history_fixtures import complete_run
from tests.core.tools.command_manager_helpers import (
    OWNER,
    Harness,
    command_harness,
    format_report,
    turns,
)
from tests.server.rpc.chat_methods_test_support import call
from tests.server.rpc_test_support import JsonObject, StubAdapter, make_state

ADDRESS = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")


async def _hold_until_cancelled(_run: Run) -> str:
    await asyncio.Event().wait()
    return "done"


async def _held_run(address: SessionAddress = ADDRESS) -> tuple[SimpleNamespace, Run]:
    manager = ChatRunManager()
    run = await manager.start(address, _hold_until_cancelled)
    await asyncio.sleep(0)
    return SimpleNamespace(chat_runs=manager, file_delivery=FileDelivery()), run


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
    timelines = RunTimelines(state.chat_runs)
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
    events = await timelines.events(run)
    messages = runtime.chat_sessions.get(ADDRESS).load()
    assert cancel_response["result"]["status"] == "cancelled"
    assert [event.type for event in events if event.type != "provider_request_status"] == [
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
    tool_delta = next(event for event in events if event.type == "tool_call_delta")
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
@pytest.mark.parametrize("project_id", [None, "project"])
async def test_cancel_tool_call_cancels_only_that_tool_call(project_id: str | None) -> None:
    state, run = await _held_run(SessionAddress(project_id, "coder", "session-one"))
    aborted: list[str] = []
    run.register_tool_cancel("tool-1", lambda: aborted.append("tool-1"))
    try:
        owner = "coder@project" if project_id else "coder"
        for wrong in ("other", "coder@elsewhere", "coder" if project_id else "coder@project"):
            refused = await call(
                state, "chat.cancel_tool_call", agent_id=wrong, run_id=run.id, tool_call_id="tool-1"
            )
            assert refused["error"]["code"] == RPC_ERROR_RUN_NOT_FOUND
            assert not run.tool_call_cancelled("tool-1")
            assert aborted == []
        response = await call(
            state, "chat.cancel_tool_call", agent_id=owner, run_id=run.id, tool_call_id="tool-1"
        )

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
async def test_stop_all_stops_the_session_tree_and_reports_how_much_it_stopped() -> None:
    stopped: list[SessionAddress] = []

    async def stop_tree(address: SessionAddress) -> int:
        stopped.append(address)
        return 3

    state = SimpleNamespace(runtime=SimpleNamespace(subagents=SimpleNamespace(stop_tree=stop_tree)))

    response = await call(state, "chat.stop_all", agent_id="builder@vbot", session_id="s1")

    assert response == {"ok": True, "result": {"ok": True, "stopped": 3}}
    assert stopped == [SessionAddress(project_id="vbot", agent_id="builder", session_id="s1")]


def _commands_state(commands: Harness) -> SimpleNamespace:
    return SimpleNamespace(runtime=SimpleNamespace(command_manager=commands.manager))


async def _background_command(commands: Harness) -> tuple[str, Any]:
    command_id, process = await commands.launch()
    commands.manager.to_background(command_id, deliver=True)
    return command_id, process


@pytest.mark.asyncio
async def test_stop_command_stops_a_background_command_for_the_user(tmp_path: Path) -> None:
    async with command_harness(tmp_path) as commands:
        state = _commands_state(commands)
        running, _process = await _background_command(commands)
        ended, process = await _background_command(commands)
        process.exit(0)
        await commands.finish(ended)
        # Its shell exited while a server it started still runs; its result
        # was delivered then.
        lingering, shell = await _background_command(commands)
        shell.exit(0, leftovers=[RunningProcess(20_001, "node")])
        await commands.advance(3.25)
        delivered = list(commands.sink.bodies)

        async def stop(command_id: str) -> JsonObject:
            return await call(
                state, "chat.stop_command", session_id=OWNER.session_id, command_id=command_id
            )

        stopped = await stop(running)
        report = await commands.finish(running)
        late = await stop(ended)
        server_stopped = await stop(lingering)
        lingering_report = commands.manager.report(lingering)

    assert stopped == {"ok": True, "result": {"stopped": True, "status": "stopped"}}
    assert (report.status, report.stop_reason) == ("stopped", "user")
    # The Agent learns that the user stopped it.
    assert commands.sink.bodies == [*delivered, format_report(report)]
    # A command that had ended keeps its own end.
    assert late == {"ok": True, "result": {"stopped": False, "status": "completed"}}
    assert commands.manager.report(ended).status == "completed"
    # The processes it left running are stopped; it keeps its end, and nothing
    # more is delivered.
    assert server_stopped == {"ok": True, "result": {"stopped": True, "status": "completed"}}
    assert (lingering_report.finished, lingering_report.still_running) == (True, ())


@pytest.mark.asyncio
async def test_command_output_reads_the_status_and_the_newest_200_lines(tmp_path: Path) -> None:
    async with command_harness(tmp_path) as commands:
        state = _commands_state(commands)
        command_id, process = await _background_command(commands)
        process.write("".join(f"line {index}\n" for index in range(1, 206)))
        await turns()

        response = await call(
            state, "chat.command_output", session_id=OWNER.session_id, command_id=command_id
        )

    assert response["ok"] is True, response
    assert (response["result"]["status"], response["result"]["still_running"]) == (
        "running",
        False,
    )
    lines = response["result"]["output"].splitlines()
    assert (len(lines), lines[0], lines[-1]) == (200, "line 6", "line 205")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.stop_command", "chat.command_output"])
@pytest.mark.parametrize("case", ["unknown", "other-session", "foreground"])
async def test_command_rpcs_see_only_the_sessions_background_commands(
    tmp_path: Path, method: str, case: str
) -> None:
    async with command_harness(tmp_path) as commands:
        state = _commands_state(commands)
        command_id, _process = await commands.launch()
        if case != "foreground":
            commands.manager.to_background(command_id, deliver=True)
        session_id = "session-other" if case == "other-session" else OWNER.session_id
        requested = "cmd_unknown" if case == "unknown" else command_id

        response = await call(state, method, session_id=session_id, command_id=requested)
        status = commands.manager.report(command_id).status

    assert response["ok"] is False
    assert response["error"]["code"] == RPC_ERROR_COMMAND_NOT_FOUND
    assert status == "running"


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
        ("chat.stop_all", {"agent_id": "coder", "session_id": "s1", "run_id": "any"}, "run_id"),
        (
            "chat.stop_command",
            {"agent_id": "coder", "session_id": "s1", "command_id": "cmd_one"},
            "agent_id",
        ),
        (
            "chat.command_output",
            {"session_id": "s1", "command_id": "cmd_one", "lines": 10},
            "lines",
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
