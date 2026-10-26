"""Script calls share Run authority while retaining activity outside Model Context."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import core.tools.execute_code as execute_code
from core.chat._model_step import ModelStepBoundary
from core.chat.wire_shaping import _embed_notes_into_request
from core.extensions import ExtensionRegistry
from core.runs import RunCancelledError
from core.sessions import SessionAddress
from core.tools import ToolContext, ToolRegistry, tool_success
from core.tools._code_process import CodeExecutionResult
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop
from tests.core.chat.tool_dispatch_test_support import ToolDispatchHarness, call
from tests.core.permissions.command_check_support import FakeAnalyzer, fake_checker

_ADDRESS = SessionAddress(None, "coder", "session-one")


def _runtime(tmp_path: Path, handler: Any) -> Any:
    tools = ToolRegistry()
    tools.register("bash", "test-sentinel", {"type": "object"}, lambda _c, _a: tool_success({}))
    tools.register("probe", "test-sentinel", {"type": "object"}, handler)
    execute_code.register_execute_code_tool(
        tools, command_checker=fake_checker(FakeAnalyzer()), credential_names=lambda: set()
    )
    runtime = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"]),
        tools=tools,
        adapter=StubAdapter(
            [
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "program",
                            "name": "execute_code",
                            "arguments": {"code": "print('selected')"},
                        }
                    ],
                },
                {"content": "done"},
            ]
        ),
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    return runtime


@pytest.mark.asyncio
async def test_script_dispatch_preserves_authority_payloads_and_hides_intermediates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[ToolContext] = []
    acknowledgements: list[str] = []
    payload_ids: list[str] = []
    denied = False

    def probe(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        contexts.append(context)
        payload_ids.append(context.attach_result_payload({"complete": [1, 2, 3]}))
        context.after_result_persisted(lambda: acknowledgements.append(context.tool_call_id))
        return tool_success({"intermediate": "private intermediate body", "arguments": arguments})

    runtime = _runtime(tmp_path, probe)
    hooks: list[str] = []
    extensions = ExtensionRegistry()
    extensions.install_handler(
        "observer", "tool_call", lambda _c, **p: hooks.append(p["tool_name"])
    )
    runtime.extensions = extensions

    async def worker(_code: str, **kwargs: Any) -> CodeExecutionResult:
        nonlocal denied
        result = await kwargs["invoke"]("probe", {"value": 7})
        assert result["data"]["intermediate"] == "private intermediate body"
        assert acknowledgements == [contexts[0].tool_call_id]
        assert await runtime.chat_sessions.tool_result_payload_async(
            _ADDRESS, payload_ids[0], owner_name="tool:probe"
        ) == {"complete": [1, 2, 3]}
        denied = True
        refused = await kwargs["invoke"]("probe", {})
        assert refused["ok"] is False
        return CodeExecutionResult("failed", 1, "selected\n", "caught error\n", 2, 1, [])

    monkeypatch.setattr(execute_code, "run_python", worker)
    loop = build_chat_loop(runtime)
    run = await loop.start_run(
        "coder",
        "go",
        session_id="session-one",
        tool_denial_resolver=lambda name: "policy changed" if denied and name == "probe" else None,
    )
    await run.wait()
    assert len(contexts) == 1
    assert contexts[0].run_id == run.id and contexts[0].programmatic
    assert hooks == ["execute_code", "probe"]
    messages = runtime.chat_sessions.get(_ADDRESS).load()
    children = [message for message in messages if message.programmatic]
    assert [message.role for message in children] == ["assistant", "tool", "assistant", "tool"]
    assert all(message.parent_tool_call_id == "program" for message in children)
    invocations = runtime.chat_sessions.get(_ADDRESS).tool_invocations_since().invocations
    assert len(invocations) == 3
    assert len({item.invocation_key for item in invocations}) == 3
    assert [item.result_ok for item in invocations if item.name == "probe"] == [True, False]
    # Build from reloaded canonical history, including normal Tools in the same Run.
    wire = _embed_notes_into_request(messages)
    wire_text = json.dumps(wire)
    assert "private intermediate body" not in wire_text
    assert "pcall_" not in wire_text
    assert "selected" in wire_text and '"name": "execute_code"' in wire_text
    assert all(not message.get("programmatic") for message in wire)


@pytest.mark.asyncio
async def test_cancelled_program_keeps_completed_children_and_settles_unfinished_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = asyncio.Event()
    acknowledged: list[str] = []

    async def probe(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("wait"):
            context.attach_result_payload({"orphan": True})
            waiting.set()
            await asyncio.Event().wait()
        context.after_result_persisted(lambda: acknowledged.append(context.tool_call_id))
        return tool_success({"saved": True})

    runtime = _runtime(tmp_path, probe)

    async def worker(_code: str, **kwargs: Any) -> CodeExecutionResult:
        await kwargs["invoke"]("probe", {})
        await kwargs["invoke"]("probe", {"wait": True})
        raise AssertionError("unreachable")

    monkeypatch.setattr(execute_code, "run_python", worker)
    run = await build_chat_loop(runtime).start_run("coder", "go", session_id="session-one")
    await waiting.wait()
    run.request_cancel(reason="user")
    with pytest.raises(RunCancelledError):
        await run.wait()
    session = runtime.chat_sessions.get(_ADDRESS)
    children = [message for message in session.load() if message.programmatic]
    assert [message.role for message in children] == ["assistant", "tool", "assistant"]
    assert acknowledged == [children[1].tool_call_id]
    invocations = [
        item for item in session.tool_invocations_since().invocations if item.name == "probe"
    ]
    assert [(item.status, item.result_ok) for item in invocations] == [
        ("completed", True),
        ("cancelled", None),
    ]
    assert "pcall_" not in json.dumps(_embed_notes_into_request(session.load()))


@pytest.mark.asyncio
async def test_programmatic_calls_keep_serial_barriers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def probe(_context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        label = arguments.get("label", "serial")
        order.append(f"{label}:start")
        if label == "first":
            started.set()
            await release.wait()
        order.append(f"{label}:end")
        return tool_success({})

    runtime = _runtime(tmp_path, probe)
    runtime.tools.register(
        "serial", "test-sentinel", {"type": "object"}, probe, parallel_safe=False
    )

    async def worker(_code: str, **kwargs: Any) -> CodeExecutionResult:
        invoke = kwargs["invoke"]
        results = await asyncio.gather(
            invoke("probe", {"label": "first"}),
            invoke("serial", {}),
            invoke("probe", {"label": "last"}),
        )
        assert all(result["ok"] for result in results)
        return CodeExecutionResult("completed", 0, "selected\n", "", 3, 0, [])

    monkeypatch.setattr(execute_code, "run_python", worker)
    run = await build_chat_loop(runtime).start_run("coder", "go", session_id="session-one")
    await started.wait()
    assert order == ["first:start"]
    release.set()
    await run.wait()
    assert order == [
        "first:start",
        "first:end",
        "serial:start",
        "serial:end",
        "last:start",
        "last:end",
    ]


@pytest.mark.asyncio
async def test_parallel_script_calls_apply_each_durable_history_delta_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[Any] = []
    tool_context = ModelStepBoundary.tool_context

    def capture_context(self: Any, context: Any, *args: Any, **kwargs: Any) -> Any:
        contexts.append(context)
        return tool_context(self, context, *args, **kwargs)

    async def worker(_code: str, **kwargs: Any) -> CodeExecutionResult:
        results = await asyncio.gather(*(kwargs["invoke"]("probe", {}) for _ in range(12)))
        assert all(result["ok"] for result in results)
        return CodeExecutionResult("completed", 0, "selected\n", "", 12, 0, [])

    monkeypatch.setattr(execute_code, "run_python", worker)
    monkeypatch.setattr(ModelStepBoundary, "tool_context", capture_context)
    runtime = _runtime(tmp_path, lambda _c, _a: tool_success({"value": 1}))
    run = await build_chat_loop(runtime).start_run("coder", "go", session_id="session-one")
    await run.wait()
    snapshot = contexts[0].session_snapshot
    assert len(snapshot.messages) == len({message.id for message in snapshot.messages})
    assert len(snapshot.active_lineage) == len({message.id for message in snapshot.active_lineage})
    assert sum(message.programmatic for message in snapshot.messages) == 24
    reloaded = runtime.chat_sessions.get(_ADDRESS).load()
    snapshot_ids = [message.id for message in snapshot.messages]
    assert [message.id for message in reloaded if message.role != "run_summary"] == snapshot_ids


@pytest.mark.asyncio
@pytest.mark.usefixtures("current_format_data_directory")
async def test_programmatic_removed_name_never_remaps_to_another_operation(tmp_path: Path) -> None:
    tools = ToolRegistry()

    async def program(context: ToolContext, _arguments: Any) -> Any:
        return await context.call_tool("remote_write_a", {})

    def other(_context: ToolContext, _arguments: Any) -> Any:
        pytest.fail("removed name was remapped to another operation")

    tools.register("program", "test-sentinel", {"type": "object"}, program)
    tools.register("remote_write_b", "test-sentinel", {"type": "object"}, other)
    harness = ToolDispatchHarness(tmp_path, tools)
    result = await harness.dispatch(
        [call("program")],
        base_allowed_tools=["program", "remote_write_b"],
        removed_tool_names=frozenset({"remote_write_a"}),
    )
    assert result.results[0]["error"]["code"] == "tool_removed"
    children = [message for message in harness.session.load() if message.programmatic]
    assert children[-1].name == "remote_write_a"
