"""External Runs: a Model outside the Agentic Loop drives a Run of its own Session."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

import core.chat._external_run as external_run_module
from core.chat import ChatError
from core.chat.messages import INPUT_ORIGIN_LIVE_VOICE
from core.json_documents import document_change
from core.permissions import normalize_permission_rules
from core.runs import RunCancelledError, RunKind, RunStatus
from core.tools import ToolContext, ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubProject,
    StubProjects,
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


def _tools(
    *,
    gate: asyncio.Event | None = None,
    started: asyncio.Event | None = None,
    after_persisted: Callable[[ToolContext], None] | None = None,
) -> ToolRegistry:
    tools = ToolRegistry()

    async def lookup(context: ToolContext, arguments: dict[str, Any]) -> Any:
        if after_persisted is not None:
            context.after_result_persisted(lambda: after_persisted(context))
        if started is not None:
            started.set()
        if gate is not None:
            task = asyncio.current_task()
            assert task is not None

            def stop_lookup() -> None:
                task.cancel()

            context.on_cancel(stop_lookup)
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
@pytest.mark.parametrize("callback_fails", [False, True])
async def test_external_run_records_the_conversation_and_runs_tool_calls(
    tmp_path: Path, callback_fails: bool
) -> None:
    acknowledged: list[str] = []

    def after_persisted(context: ToolContext) -> None:
        session = runtime.chat_sessions.get(session_address("coder", context.session_id))
        assert any(
            message.role == "tool" and message.tool_call_id == context.tool_call_id
            for message in session.load()
        )
        acknowledged.append(context.tool_call_id)
        if callback_fails:
            raise RuntimeError("callback failed after its durable acknowledgement")

    runtime = _runtime(tmp_path, _tools(after_persisted=after_persisted))
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
    assert acknowledged == ["call_1"]


@pytest.mark.asyncio
async def test_external_run_tool_calls_work_in_the_project_inside_the_agents_rules(
    tmp_path: Path,
) -> None:
    seen: list[ToolContext] = []

    def probe(context: ToolContext, _arguments: Any) -> Any:
        seen.append(context)
        return tool_success({})

    tools = ToolRegistry()
    tools.register("lookup", "Look something up.", {"type": "object"}, probe)
    repo = tmp_path / "repo"
    repo.mkdir()
    agent = LiveAgent(
        id="coder",
        model=MODEL,
        allowed_tools=["lookup"],
        workspace=tmp_path / "workspace",
        root_project_id="proj",
        permission_rules=normalize_permission_rules(
            {"read": "work_area", "write": "work_area", "deny": ["Read({workdir}/secrets)"]}
        ),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path / "data",
        agent=agent,
        adapter=StubAdapter([]),
        tools=tools,
        projects=StubProjects({"proj": StubProject("proj", str(repo), [])}),
    )
    external = await build_chat_loop(runtime).start_external_run(
        "coder", model="voice-model", title="Live call"
    )

    address = session_address("coder", external.session_id)
    execution = {"computer_id": "laptop", "allowed_computer_ids": ["laptop", "server"]}
    runtime.chat_sessions.mutate_metadata(
        address, lambda metadata: metadata.update(execution=execution)
    )
    await external.run_tool("call_1", "lookup", {})
    # The user tightens the rules while the call runs, as vBot saves the edit.
    runtime.agents._agent = replace(
        agent, permission_rules=normalize_permission_rules({"write": "scratch"})
    )
    with document_change(tmp_path / "data" / "agents" / "coder" / "agent.json"):
        pass
    narrowed = {"computer_id": "laptop", "allowed_computer_ids": []}
    runtime.chat_sessions.mutate_metadata(
        address, lambda metadata: metadata.update(execution=narrowed)
    )
    await external.run_tool("call_2", "lookup", {})
    await external.finish()

    first, second = seen
    # Relative paths resolve in the Working Project, like a Chat Run's.
    assert first.cwd == repo
    boundary = first.permissions
    assert boundary.check_path(repo / "notes.md", "write") is None
    denied = boundary.check_path(repo / "secrets" / "key", "read")
    assert denied is not None and denied.reason == "deny_rule"
    outside = boundary.check_path(tmp_path / "elsewhere.md", "read")
    assert outside is not None and outside.reason == "not_permitted"
    # The edit reaches the next call.
    refused = second.permissions.check_path(repo / "notes.md", "write")
    assert refused is not None and refused.reason == "not_permitted"
    assert [first.execution, second.execution] == [execution, narrowed]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["admission", "handler"])
async def test_cancelling_an_external_run_ends_the_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_at: str
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    acknowledged: list[str] = []
    runtime = _runtime(
        tmp_path,
        _tools(
            gate=gate,
            started=started,
            after_persisted=lambda context: acknowledged.append(context.tool_call_id),
        ),
    )
    loop = build_chat_loop(runtime)
    cancelled: list[bool] = []
    external = await loop.start_external_run(
        "coder", model="voice-model", title="Live call", on_cancel=lambda: cancelled.append(True)
    )
    admitted, release_admission = asyncio.Event(), asyncio.Event()
    if cancel_at == "admission":
        start_invocation = external._session.start_tool_invocation_async  # noqa: SLF001

        async def gated_admission(**options: Any) -> int:
            key = await start_invocation(**options)
            admitted.set()
            await release_admission.wait()
            return key

        monkeypatch.setattr(external._session, "start_tool_invocation_async", gated_admission)  # noqa: SLF001
    call = asyncio.create_task(external.run_tool("call_1", "lookup", {"q": "slow"}))
    await asyncio.wait_for((admitted if cancel_at == "admission" else started).wait(), WAIT_SECONDS)
    run = external.run

    run.request_cancel()
    release_admission.set()
    with pytest.raises(RunCancelledError):
        await asyncio.wait_for(run.wait(), WAIT_SECONDS)
    with pytest.raises(asyncio.CancelledError):
        await call

    assert run.status == RunStatus.CANCELLED
    assert cancelled == [True]
    session = runtime.chat_sessions.get(session_address("coder", external.session_id))
    results = [json.loads(m.content) for m in session.load() if m.role == "tool"]
    assert [result["error"]["code"] for result in results] == ["tool_stopped"]
    assert acknowledged == []
    assert started.is_set() is (cancel_at == "handler")
    [invocation] = session.tool_invocations_since().invocations
    assert invocation.status == "cancelled" and invocation.result_ok is None
    assert run.tool_call_count == 1


@pytest.mark.asyncio
async def test_a_call_still_running_when_the_conversation_ends_is_stored_as_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(external_run_module, "_SETTLE_SECONDS", 0)
    gate, started = asyncio.Event(), asyncio.Event()
    acknowledged: list[str] = []
    runtime = _runtime(
        tmp_path,
        _tools(
            gate=gate,
            started=started,
            after_persisted=lambda context: acknowledged.append(context.tool_call_id),
        ),
    )
    loop = build_chat_loop(runtime)
    external = await loop.start_external_run("coder", model="voice-model", title="Live call")
    call = asyncio.create_task(external.run_tool("call_1", "lookup", {"q": "slow"}))
    await started.wait()

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
    assert acknowledged == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["deadline", "finish"])
async def test_a_deadline_while_a_finished_call_is_stored_returns_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    acknowledged: list[str] = []
    runtime = _runtime(
        tmp_path, _tools(after_persisted=lambda context: acknowledged.append(context.tool_call_id))
    )
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
    assert acknowledged == []
    finishing: asyncio.Task[None] | None = None
    if ending == "deadline":
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        await asyncio.sleep(0)
    else:
        monkeypatch.setattr(external_run_module, "_SETTLE_SECONDS", 0)
        settled = asyncio.Event()
        settle_calls = external._settle_calls  # noqa: SLF001

        async def observe_settle() -> None:
            await settle_calls()
            settled.set()

        monkeypatch.setattr(external, "_settle_calls", observe_settle)
        finishing = asyncio.create_task(external.finish())
        await settled.wait()
        assert not finishing.done()
    release.set()
    result = await asyncio.wait_for(call, WAIT_SECONDS)
    if finishing is not None:
        await finishing
    else:
        await external.finish()

    # The voice Model hears the result that history holds, not a timeout.
    assert result["ok"] is True and result["data"] == {"found": "weather"}
    assert [role for role, _ in _stored(runtime, external.session_id)] == ["assistant", "tool"]
    assert acknowledged == ["call_1"]


@pytest.mark.asyncio
async def test_a_tool_result_that_cannot_be_stored_is_not_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acknowledged: list[str] = []
    runtime = _runtime(
        tmp_path, _tools(after_persisted=lambda context: acknowledged.append(context.tool_call_id))
    )
    external = await build_chat_loop(runtime).start_external_run(
        "coder", model="voice-model", title="Live call"
    )

    async def cannot_store(*_arguments: Any, **_options: Any) -> None:
        raise OSError("simulated store failure")

    monkeypatch.setattr(external._session, "append_many_async", cannot_store)  # noqa: SLF001
    result = await external.run_tool("call_1", "lookup", {"q": "weather"})
    await external.finish()
    await asyncio.wait_for(external.run.wait(), WAIT_SECONDS)

    assert result["ok"] is True
    assert acknowledged == []
    assert _stored(runtime, external.session_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("replaced", [False, True])
async def test_external_run_failure_and_discard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: bool
) -> None:
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
    address = session_address("coder", unused.session_id)
    replacement_generation = None
    if replaced:
        await unused.finish()
        await unused.run.wait()
        async with runtime.chat_runs.session_admission_guard(address):
            await runtime.chat_sessions.archive(address)
            replacement_generation = runtime.chat_sessions.create(
                "coder", session_id=unused.session_id
            ).generation_id
    await unused.discard()
    assert runtime.chat_sessions.exists(address) is replaced
    if replaced:
        assert runtime.chat_sessions.get(address).generation_id == replacement_generation

    # A start that fails after its Run was admitted ends that Run, then removes its Session.
    async def failing_bind_permissions(self: Any, project_root: Path | None) -> None:
        raise RuntimeError("boundary build failed")

    monkeypatch.setattr(
        external_run_module.ExternalRun, "bind_permissions", failing_bind_permissions
    )
    sessions = {session.id for session in runtime.chat_sessions.list("coder")}
    with pytest.raises(RuntimeError, match="boundary build failed"):
        await loop.start_external_run("coder", model="voice-model", title="Live call")
    assert runtime.chat_runs.active_runs() == []
    assert {session.id for session in runtime.chat_sessions.list("coder")} == sessions


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
