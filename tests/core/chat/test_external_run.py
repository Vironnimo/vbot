"""External Runs: a Model outside the Agentic Loop drives a Run of its own Session."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import core.chat._external_run as external_run_module
from core.chat import ChatError
from core.chat.messages import INPUT_ORIGIN_LIVE_VOICE
from core.runs import RunCancelledError, RunKind, RunStatus
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)
from tests.core.chat.chat_loop_tools_test_support import WAIT_SECONDS, final

MODEL = "openai/gpt-5.2"


@dataclass(frozen=True)
class LiveAgent(StubAgent):
    builtin: str = "live_voice"


def _runtime(tmp_path: Path, tools: ToolRegistry, responses: list[Any] | None = None) -> Any:
    agent = LiveAgent(id="coder", model=MODEL, allowed_tools=["lookup"])
    return StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=StubAdapter(responses or []), tools=tools
    )


def _tools(*, gate: asyncio.Event | None = None) -> ToolRegistry:
    tools = ToolRegistry()

    async def lookup(_context: Any, arguments: dict[str, Any]) -> Any:
        if gate is not None:
            await gate.wait()
        return tool_success({"found": arguments.get("q")})

    tools.register("lookup", "Look something up.", {"type": "object"}, lookup)
    tools.register(
        "relay",
        "Relay a request.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"relayed": arguments.get("request")}),
        requires_opt_in=True,
    )
    return tools


def _stored(runtime: Any, session_id: str) -> list[tuple[str, Any]]:
    session = runtime.chat_sessions.get(session_address("coder", session_id))
    return [
        (message.role, message.tool_calls[0].name if message.tool_calls else message.content)
        for message in session.load()
        if message.role != "run_summary"
    ]


@pytest.mark.asyncio
async def test_external_run_records_the_conversation_and_runs_tool_calls(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _tools())
    loop = build_chat_loop(runtime)

    external = await loop.start_external_run(
        "coder", model="voice-model", title="Live call", extra_tools=["relay"]
    )
    assert [definition["name"] for definition in external.tool_definitions] == [
        "lookup",
        "relay",
    ]
    await external.record_user("What is the weather?")
    looked_up = await external.run_tool("call_1", "lookup", {"q": "weather"})
    # A namespace prefix some Models write still names the offered Tool.
    relayed = await external.run_tool("call_2", "functions.relay", {"request": "check"})
    await external.record_note("vBot update: a Run finished.")
    await external.record_assistant("It is sunny.")
    await external.finish()

    run = external.run
    await asyncio.wait_for(run.wait(), WAIT_SECONDS)
    assert run.status == RunStatus.COMPLETED
    assert run.run_kind == RunKind.LIVE
    assert looked_up["ok"] is True and looked_up["data"] == {"found": "weather"}
    assert relayed["data"] == {"relayed": "check"}
    stored = _stored(runtime, external.session_id)
    assert [role for role, _ in stored] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "note",
        "assistant",
    ]
    assert stored[1][1] == "lookup" and stored[3][1] == "relay"
    address = session_address("coder", external.session_id)
    assert runtime.chat_sessions.metadata_value(address, "auto_title") == "Live call"
    assert external.ended
    assert (await external.run_tool("call_3", "lookup", {}))["ok"] is False
    assert runtime.adapter.requests == []


@pytest.mark.asyncio
async def test_cancelling_an_external_run_ends_the_conversation(tmp_path: Path) -> None:
    gate = asyncio.Event()
    runtime = _runtime(tmp_path, _tools(gate=gate))
    loop = build_chat_loop(runtime)
    cancelled: list[bool] = []
    external = await loop.start_external_run(
        "coder", model="voice-model", title="Live call", on_cancel=lambda: cancelled.append(True)
    )
    call = asyncio.create_task(external.run_tool("call_1", "lookup", {"q": "slow"}))
    await asyncio.sleep(0)
    run = external.run

    run.request_cancel()
    with pytest.raises(RunCancelledError):
        await asyncio.wait_for(run.wait(), WAIT_SECONDS)
    with pytest.raises(asyncio.CancelledError):
        await call

    assert run.status == RunStatus.CANCELLED
    assert cancelled == [True]
    session = runtime.chat_sessions.get(session_address("coder", external.session_id))
    results = [json.loads(m.content) for m in session.load() if m.role == "tool"]
    assert [result["error"]["code"] for result in results] == ["tool_stopped"]


@pytest.mark.asyncio
async def test_a_call_still_running_when_the_conversation_ends_is_stored_as_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(external_run_module, "_SETTLE_SECONDS", 0)
    gate = asyncio.Event()
    runtime = _runtime(tmp_path, _tools(gate=gate))
    loop = build_chat_loop(runtime)
    external = await loop.start_external_run("coder", model="voice-model", title="Live call")
    call = asyncio.create_task(external.run_tool("call_1", "lookup", {"q": "slow"}))
    await asyncio.sleep(0)

    await external.finish()
    await asyncio.wait_for(external.run.wait(), WAIT_SECONDS)
    # The call settles after the conversation ended; its turn is already stored.
    gate.set()
    await asyncio.wait_for(call, WAIT_SECONDS)

    session = runtime.chat_sessions.get(session_address("coder", external.session_id))
    stored = [message for message in session.load() if message.role in {"assistant", "tool"}]
    assert [message.role for message in stored] == ["assistant", "tool"]
    assert stored[0].tool_calls[0].name == "lookup"
    assert json.loads(stored[1].content)["error"]["code"] == "tool_stopped"


@pytest.mark.asyncio
async def test_a_deadline_while_a_finished_call_is_stored_returns_its_result(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, _tools())
    loop = build_chat_loop(runtime)
    external = await loop.start_external_run("coder", model="voice-model", title="Live call")
    session = external._session  # noqa: SLF001
    append_many_async = session.append_many_async
    storing, release = asyncio.Event(), asyncio.Event()

    async def slow_append(*arguments: Any, **options: Any) -> Any:
        storing.set()
        await release.wait()
        return await append_many_async(*arguments, **options)

    session.append_many_async = slow_append  # type: ignore[method-assign]
    deadlines: list[asyncio.Timeout] = []

    async def bounded_call() -> Any:
        async with asyncio.timeout(None) as deadline:
            deadlines.append(deadline)
            return await external.run_tool("call_1", "lookup", {"q": "weather"})

    call = asyncio.create_task(bounded_call())
    await storing.wait()
    deadlines[0].reschedule(asyncio.get_running_loop().time())
    await asyncio.sleep(0)
    release.set()
    result = await asyncio.wait_for(call, WAIT_SECONDS)
    await external.finish()

    # The voice Model hears the result that history holds, not a timeout.
    assert result["ok"] is True and result["data"] == {"found": "weather"}
    assert [role for role, _ in _stored(runtime, external.session_id)] == ["assistant", "tool"]


@pytest.mark.asyncio
async def test_external_run_failure_and_discard(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _tools())
    loop = build_chat_loop(runtime)

    failed = await loop.start_external_run("coder", model="voice-model", title="Live call")
    await failed.finish(failure="The voice connection dropped.")
    run = failed.run
    with pytest.raises(ChatError):
        await asyncio.wait_for(run.wait(), WAIT_SECONDS)
    assert run.status == RunStatus.FAILED
    assert _stored(runtime, failed.session_id) == [("error", "The voice connection dropped.")]

    unused = await loop.start_external_run("coder", model="voice-model", title="Live call")
    await unused.discard()
    assert not runtime.chat_sessions.exists(session_address("coder", unused.session_id))


@pytest.mark.asyncio
async def test_live_agent_sessions_take_only_live_runs(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _tools(), [final("Done")])
    runtime.chat_sessions.create("coder", session_id="backend", run_kind=RunKind.LIVE)
    loop = build_chat_loop(runtime)

    with pytest.raises(ChatError, match="no typed messages"):
        await loop.start_run("coder", "Hi", session_id="backend")
    with pytest.raises(ChatError, match="no typed messages"):
        await loop.queue_run("coder", "Hi", session_id="backend")

    run = await loop.start_run(
        "coder",
        "Check the build.",
        session_id="backend",
        input_origin=INPUT_ORIGIN_LIVE_VOICE,
        run_kind=RunKind.LIVE,
        contributes_to_agent_activity=False,
        context_note="Said since the last request: none.",
    )
    await asyncio.wait_for(run.wait(), WAIT_SECONDS)
    roles = _stored(runtime, "backend")
    note_index = roles.index(("note", "Said since the last request: none."))
    assert [role for role, _ in roles[note_index:]][:3] == ["note", "note", "user"]
    assert roles[note_index + 2] == ("user", "Check the build.")
