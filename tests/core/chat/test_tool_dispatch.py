"""Tests for tool-call dispatch wiring through ``_dispatch_tool_calls``."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat.messages import JsonObject, ToolCall
from core.chat.tool_dispatch import (
    _dispatch_tool_calls,
    _resolve_tool_cwd,
    _sync_skill_context_messages,
)
from core.extensions import Deny, ExtensionRegistry, Modify, Replace
from core.runs import TOOL_CALL_STARTED_EVENT, Run, RunStatus
from core.sessions import ChatSessionManager
from core.tools import (
    ToolContext,
    ToolRegistry,
    tool_failure,
    tool_success,
)

CANCELLED_BY_USER_MESSAGE = "Command aborted by the user"


@dataclass(frozen=True)
class _StubAgent:
    id: str
    workspace: Path
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    memory_prompt_mode: str = "agent_user"


class _StubRuntime:
    """Minimal stand-in for the runtime attributes ``_dispatch_tool_calls`` reads."""

    def __init__(self, tools: ToolRegistry, data_dir: Path) -> None:
        self.tools = tools
        self.storage = _StubStorage(data_dir)
        self.system_prompts = _StubSystemPrompts()
        self.extensions = None


class _StubStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _StubSystemPrompts:
    app_dir = Path.cwd()


def _build_session(tmp_path: Path, agent_id: str = "coder", session_id: str = "session-one") -> Any:
    manager = ChatSessionManager(tmp_path)
    return manager.create(agent_id, session_id=session_id)


def _build_runtime_and_agent(tmp_path: Path, tools: ToolRegistry) -> tuple[Any, _StubAgent]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    agent = _StubAgent(id="coder", workspace=workspace, allowed_tools=["*"])
    runtime = _StubRuntime(tools, tmp_path)
    return runtime, agent


def _decode_tool_result(message_content: object) -> JsonObject:
    assert isinstance(message_content, str)
    return cast(JsonObject, json.loads(message_content))


class TestDispatchCancelWiring:
    @pytest.mark.asyncio
    async def test_cancel_registration_hook_receives_per_call_id_through_on_cancel(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        registered: dict[str, list[Callable[[], None]]] = {}

        def cancellable_handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            def on_abort() -> None:
                pass

            context.on_cancel(on_abort)
            registered.setdefault(context.tool_call_id, []).append(on_abort)
            return tool_success({"tool_call_id": context.tool_call_id})

        tools = ToolRegistry()
        tools.register(
            "cancellable",
            "Tool for testing cancel wiring.",
            {"type": "object"},
            cancellable_handler,
        )
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [
            ToolCall(id="call-1", name="cancellable", arguments={}),
            ToolCall(id="call-2", name="cancellable", arguments={}),
        ]

        # Act
        await _dispatch_tool_calls(
            runtime,
            agent,
            tool_calls,
            session,
            run,
            nesting_depth=0,
        )

        # Assert: each call routed its callback through the per-call registrar,
        # so the registry carries the right id-bound entry per sibling call.
        assert set(registered) == {"call-1", "call-2"}
        assert len(registered["call-1"]) == 1
        assert len(registered["call-2"]) == 1

    @pytest.mark.asyncio
    async def test_cancelled_tool_call_yields_cancelled_by_user_envelope(
        self, tmp_path: Path
    ) -> None:
        # Arrange: tool blocks until its cancel callback fires, then returns
        # the handler's cancelled_by_user envelope when was_cancelled_by_user flips.
        cancel_fired = asyncio.Event()

        async def cancellable_handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            context.on_cancel(cancel_fired.set)
            try:
                await asyncio.wait_for(cancel_fired.wait(), timeout=5.0)
            except TimeoutError:
                return tool_failure("timeout", "cancel callback never fired")
            if context.was_cancelled_by_user():
                return tool_failure("cancelled_by_user", CANCELLED_BY_USER_MESSAGE)
            return tool_success({"unexpected": True})

        tools = ToolRegistry()
        tools.register(
            "cancellable",
            "Tool that returns the cancelled_by_user envelope after cancel.",
            {"type": "object"},
            cancellable_handler,
        )
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-cancel", name="cancellable", arguments={})]

        # Act: start dispatch in the background; once the tool has registered
        # its cancel callback, fire the per-tool-call cancel from outside.
        dispatch_task = asyncio.create_task(
            _dispatch_tool_calls(
                runtime,
                agent,
                tool_calls,
                session,
                run,
                nesting_depth=0,
            )
        )
        await _wait_for_registry_entry(run, "call-cancel", timeout=5.0)
        cancelled = run.cancel_tool_call("call-cancel")
        messages, _ = await dispatch_task

        # Assert
        assert cancelled is True
        assert len(messages) == 1
        result = _decode_tool_result(messages[0].content)
        assert result == tool_failure("cancelled_by_user", CANCELLED_BY_USER_MESSAGE)
        assert messages[0].tool_call_id == "call-cancel"

    @pytest.mark.asyncio
    async def test_per_tool_cancel_leaves_run_running_and_does_not_set_cancel_requested(
        self, tmp_path: Path
    ) -> None:
        # Arrange: same blocking-cancel tool as above.
        cancel_fired = asyncio.Event()

        async def cancellable_handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            context.on_cancel(cancel_fired.set)
            try:
                await asyncio.wait_for(cancel_fired.wait(), timeout=5.0)
            except TimeoutError:
                return tool_failure("timeout", "cancel callback never fired")
            if context.was_cancelled_by_user():
                return tool_failure("cancelled_by_user", CANCELLED_BY_USER_MESSAGE)
            return tool_success({"ok": True})

        tools = ToolRegistry()
        tools.register(
            "cancellable",
            "Tool that returns the cancelled_by_user envelope after cancel.",
            {"type": "object"},
            cancellable_handler,
        )
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-cancel", name="cancellable", arguments={})]

        # Act
        dispatch_task = asyncio.create_task(
            _dispatch_tool_calls(
                runtime,
                agent,
                tool_calls,
                session,
                run,
                nesting_depth=0,
            )
        )
        await _wait_for_registry_entry(run, "call-cancel", timeout=5.0)
        run.cancel_tool_call("call-cancel")
        await dispatch_task

        # Assert: per-tool cancel must not flip the run's cancel_requested or status.
        assert run.cancel_requested is False
        assert run.status is RunStatus.RUNNING
        assert run.cancel_reason is None

    @pytest.mark.asyncio
    async def test_per_tool_cancel_registry_entry_is_cleared_after_dispatch(
        self, tmp_path: Path
    ) -> None:
        # Arrange: simple tool that registers a no-op cancel callback.
        def cancellable_handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            context.on_cancel(lambda: None)
            return tool_success({"tool_call_id": context.tool_call_id})

        tools = ToolRegistry()
        tools.register(
            "cancellable",
            "Tool that registers a cancel callback.",
            {"type": "object"},
            cancellable_handler,
        )
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="cancellable", arguments={})]

        # Act
        await _dispatch_tool_calls(
            runtime,
            agent,
            tool_calls,
            session,
            run,
            nesting_depth=0,
        )

        # Assert: dispatch must clear the per-call registry entry, both when
        # the call was never cancelled and after a cancel that completed.
        assert run.tool_call_cancelled("call-1") is False
        assert "call-1" not in run._tool_cancel_callbacks  # noqa: SLF001

        # And a fresh call with the same id starts clean.
        def same_id_handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            return tool_success({"reused": True})

        tools.register(
            "reused", "Tool reusing an existing id.", {"type": "object"}, same_id_handler
        )
        second_tool_calls = [ToolCall(id="call-1", name="reused", arguments={})]
        messages, _ = await _dispatch_tool_calls(
            runtime,
            agent,
            second_tool_calls,
            session,
            run,
            nesting_depth=0,
        )
        assert _decode_tool_result(messages[0].content) == tool_success({"reused": True})

    @pytest.mark.asyncio
    async def test_dispatch_returns_completed_result_when_run_cancel_arrives(
        self, tmp_path: Path
    ) -> None:
        # Arrange: a tool that signals when it has started so the test
        # can flip the run cancel flag during the in-flight dispatch.
        # The dispatch must still return the computed result so the
        # chat-loop persist loop can record it before honoring the
        # run cancel — this is the bug the write-side fix prevents.
        tool_started = asyncio.Event()

        async def slow_handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            tool_started.set()
            # Yield to give the test a chance to flip cancel_requested
            # before the tool returns.
            await asyncio.sleep(0.05)
            return tool_success({"ok": True})

        tools = ToolRegistry()
        tools.register("slow", "Slow tool.", {"type": "object"}, slow_handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-slow", name="slow", arguments={})]

        async def flip_flag_after_tool_starts() -> None:
            await tool_started.wait()
            run.cancel_requested = True

        flip_task = asyncio.create_task(flip_flag_after_tool_starts())
        messages, _ = await _dispatch_tool_calls(
            runtime,
            agent,
            tool_calls,
            session,
            run,
            nesting_depth=0,
        )
        await flip_task

        # Assert: dispatch returned the tool's computed result; the cancel
        # flag is honored at the chat-loop persist-loop boundary, not by
        # silently dropping the result here.
        assert len(messages) == 1
        assert _decode_tool_result(messages[0].content) == tool_success({"ok": True})


async def _wait_for_registry_entry(run: Run, tool_call_id: str, *, timeout: float) -> None:
    """Poll until the per-tool-call cancel registry has an entry for *tool_call_id*."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if tool_call_id in run._tool_cancel_callbacks:  # noqa: SLF001
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"per-tool-call cancel callback for {tool_call_id!r} was never registered")


def _started_event_arguments(run: Run) -> JsonObject:
    """Return the arguments recorded on the run's tool_call_started event."""
    for event in run.events:
        if event.type == TOOL_CALL_STARTED_EVENT:
            return cast(JsonObject, event.payload["tool_call"]["arguments"])
    raise AssertionError("no tool_call_started event was emitted")


class TestExtensionDecisionWiring:
    """The tool_call decision model wired through ``_dispatch_tool_calls``."""

    @pytest.mark.asyncio
    async def test_denied_tool_call_yields_error_envelope_and_never_executes(
        self, tmp_path: Path
    ) -> None:
        executed: list[str] = []

        def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            executed.append(context.tool_call_id)
            return tool_success({"ran": True})

        tools = ToolRegistry()
        tools.register("guarded", "Guarded tool.", {"type": "object"}, handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        registry = ExtensionRegistry()
        registry.install_handler(
            "guard", "tool_call", lambda ctx, **payload: Deny("not allowed here")
        )
        runtime.extensions = registry
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="guarded", arguments={"x": 1})]

        messages, _ = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        result = _decode_tool_result(messages[0].content)
        assert result["ok"] is False
        assert result["error"]["code"] == "tool_call_denied"
        assert "not allowed here" in result["error"]["message"]
        assert "guard" in result["error"]["message"]
        # the guarded tool handler never ran
        assert executed == []

    @pytest.mark.asyncio
    async def test_modified_input_reaches_handler_and_started_event(self, tmp_path: Path) -> None:
        def echo_handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
            return tool_success({"echo": arguments})

        tools = ToolRegistry()
        tools.register("echo", "Echo tool.", {"type": "object"}, echo_handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        registry = ExtensionRegistry()
        registry.install_handler(
            "rewriter", "tool_call", lambda ctx, **payload: Modify({"cmd": "rewritten"})
        )
        runtime.extensions = registry
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="echo", arguments={"cmd": "original"})]

        messages, _ = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        result = _decode_tool_result(messages[0].content)
        # the tool executed with the modified arguments
        assert result["data"]["echo"] == {"cmd": "rewritten"}
        # and the started event shows the effective (modified) arguments
        assert _started_event_arguments(run) == {"cmd": "rewritten"}

    @pytest.mark.asyncio
    async def test_replace_short_circuits_with_envelope(self, tmp_path: Path) -> None:
        executed: list[str] = []

        def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            executed.append(context.tool_call_id)
            return tool_success({"ran": True})

        tools = ToolRegistry()
        tools.register("replaced", "Replaceable tool.", {"type": "object"}, handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        registry = ExtensionRegistry()
        replacement = tool_success({"replaced": True})
        registry.install_handler(
            "replacer", "tool_call", lambda ctx, **payload: Replace(replacement)
        )
        runtime.extensions = registry
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="replaced", arguments={})]

        messages, _ = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        assert _decode_tool_result(messages[0].content) == replacement
        assert executed == []

    @pytest.mark.asyncio
    async def test_tool_result_hook_replaces_envelope(self, tmp_path: Path) -> None:
        def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            return tool_success({"original": True})

        tools = ToolRegistry()
        tools.register("t", "Tool.", {"type": "object"}, handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        registry = ExtensionRegistry()
        replacement = tool_success({"patched": True})
        registry.install_handler("patcher", "tool_result", lambda ctx, **payload: replacement)
        runtime.extensions = registry
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="t", arguments={})]

        messages, _ = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        assert _decode_tool_result(messages[0].content) == replacement

    @pytest.mark.asyncio
    async def test_add_note_from_hook_lands_in_session(self, tmp_path: Path) -> None:
        def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            return tool_success({"ran": True})

        tools = ToolRegistry()
        tools.register("t", "Tool.", {"type": "object"}, handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        registry = ExtensionRegistry()

        def note_hook(ctx: Any, **payload: Any) -> None:
            ctx.add_note("hook was here")
            return None

        registry.install_handler("noter", "tool_call", note_hook)
        runtime.extensions = registry
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="t", arguments={})]

        await _dispatch_tool_calls(runtime, agent, tool_calls, session, run, nesting_depth=0)

        note_contents = [m.content for m in session.load() if m.role == "note"]
        assert "hook was here" in note_contents


class TestReadMediaInjections:
    """``read_media`` artifacts surface as media injections for the chat loop."""

    @pytest.mark.asyncio
    async def test_read_media_artifact_becomes_media_injection(self, tmp_path: Path) -> None:
        def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            return tool_success(
                {"content": "loaded"},
                artifacts=[
                    {
                        "kind": "read_media",
                        "attachment_id": "att-1",
                        "filename": "diagram.png",
                        "media_type": "image/png",
                    }
                ],
            )

        tools = ToolRegistry()
        tools.register("read", "Reads media.", {"type": "object"}, handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="read", arguments={})]

        tool_messages, media_injections = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        assert len(tool_messages) == 1
        assert media_injections == [
            {"attachment_id": "att-1", "filename": "diagram.png", "media_type": "image/png"}
        ]

    @pytest.mark.asyncio
    async def test_non_read_media_artifacts_produce_no_injection(self, tmp_path: Path) -> None:
        def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            return tool_success(
                {"message": "image generated"},
                artifacts=[{"kind": "image", "url": "/api/x", "id": "img-1"}],
            )

        tools = ToolRegistry()
        tools.register("image_generation", "Generates images.", {"type": "object"}, handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="image_generation", arguments={})]

        tool_messages, media_injections = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        assert len(tool_messages) == 1
        assert media_injections == []


class TestUnexpectedToolCrashLogging:
    """An unexpected handler crash is logged before being folded into a result."""

    @pytest.mark.asyncio
    async def test_unexpected_tool_crash_logs_error_with_traceback(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        def crashing_handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            raise RuntimeError("handler exploded")

        tools = ToolRegistry()
        tools.register("boom", "Tool that crashes.", {"type": "object"}, crashing_handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="boom", arguments={})]

        caplog.set_level(logging.ERROR, logger="vbot.chat")
        messages, _ = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        # The crash is converted to a tool_execution_error envelope (run continues)...
        result = _decode_tool_result(messages[0].content)
        assert result["ok"] is False
        assert result["error"]["code"] == "tool_execution_error"

        # ...and logged at ERROR with the originating exception and tool name.
        error_records = [
            record
            for record in caplog.records
            if record.name == "vbot.chat"
            and record.levelno == logging.ERROR
            and "crashed unexpectedly" in record.getMessage()
        ]
        assert len(error_records) == 1
        record = error_records[0]
        assert "boom" in record.getMessage()
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], RuntimeError)


class _StubSkillSession:
    """Minimal session exposing only ``skill_context_messages`` for sync tests."""

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def skill_context_messages(self) -> list[JsonObject]:
        return [
            {"role": "user", "content": f'<skill_content name="{name}">…</skill_content>'}
            for name in self._names
        ]


def _skill_content(name: str) -> str:
    return f'<skill_content name="{name}">…</skill_content>'


class TestResolveToolCwd:
    """The cwd-build rule: project cwd when set, else the workspace fallback."""

    def test_returns_project_cwd_when_set(self) -> None:
        repo = Path("/repos/acme")

        assert _resolve_tool_cwd(repo, Path("/data/workspace-coder")) == repo

    def test_falls_back_to_workspace_without_project_cwd(self) -> None:
        workspace = Path("/data/workspace-coder")

        assert _resolve_tool_cwd(None, workspace) == workspace


class TestDispatchCwdWiring:
    """``_dispatch_tool_calls`` builds ``ToolContext.cwd`` from the project cwd."""

    @staticmethod
    def _register_cwd_probe(tools: ToolRegistry, seen: list[Path]) -> None:
        def cwd_handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            seen.append(context.effective_cwd)
            return tool_success({"cwd": str(context.effective_cwd)})

        tools.register(
            "cwd_probe",
            "Record the effective working directory for testing.",
            {"type": "object"},
            cwd_handler,
        )

    @pytest.mark.asyncio
    async def test_project_cwd_reaches_tool_context(self, tmp_path: Path) -> None:
        # A project session supplies the repo cwd, which must reach the tool so
        # file/shell tools resolve relative paths against the repo, not workspace.
        seen: list[Path] = []
        tools = ToolRegistry()
        self._register_cwd_probe(tools, seen)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        project_cwd = tmp_path / "repo"
        project_cwd.mkdir()

        await _dispatch_tool_calls(
            runtime,
            agent,
            [ToolCall(id="call-1", name="cwd_probe", arguments={})],
            session,
            run,
            nesting_depth=0,
            project_cwd=project_cwd,
        )

        assert seen == [project_cwd]

    @pytest.mark.asyncio
    async def test_without_project_cwd_tool_context_uses_workspace(self, tmp_path: Path) -> None:
        # No project cwd (identity sessions / every current caller): the tool
        # resolves against the agent workspace, preserving today's behavior.
        seen: list[Path] = []
        tools = ToolRegistry()
        self._register_cwd_probe(tools, seen)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

        await _dispatch_tool_calls(
            runtime,
            agent,
            [ToolCall(id="call-1", name="cwd_probe", arguments={})],
            session,
            run,
            nesting_depth=0,
        )

        assert seen == [agent.workspace]


class TestDispatchProjectIdWiring:
    """``_dispatch_tool_calls`` threads the owning run's project onto ToolContext."""

    @staticmethod
    def _register_project_probe(tools: ToolRegistry, seen: list[str | None]) -> None:
        def project_handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            seen.append(context.project_id)
            return tool_success({"project_id": context.project_id})

        tools.register(
            "project_probe",
            "Record the run's project id for testing.",
            {"type": "object"},
            project_handler,
        )

    @pytest.mark.asyncio
    async def test_project_id_reaches_tool_context(self, tmp_path: Path) -> None:
        # A project run threads its project_id onto every ToolContext so the
        # subagent tool can inherit it for project-scoped child spawns.
        seen: list[str | None] = []
        tools = ToolRegistry()
        self._register_project_probe(tools, seen)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

        await _dispatch_tool_calls(
            runtime,
            agent,
            [ToolCall(id="call-1", name="project_probe", arguments={})],
            session,
            run,
            nesting_depth=0,
            project_id="acme",
        )

        assert seen == ["acme"]

    @pytest.mark.asyncio
    async def test_without_project_id_tool_context_is_none(self, tmp_path: Path) -> None:
        # An identity run (no project_id) leaves ToolContext.project_id None —
        # today's behavior, exactly unchanged.
        seen: list[str | None] = []
        tools = ToolRegistry()
        self._register_project_probe(tools, seen)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

        await _dispatch_tool_calls(
            runtime,
            agent,
            [ToolCall(id="call-1", name="project_probe", arguments={})],
            session,
            run,
            nesting_depth=0,
        )

        assert seen == [None]


class TestSyncSkillContextMessages:
    def test_inserts_at_front_without_system_message(self) -> None:
        messages: list[JsonObject] = [{"role": "user", "content": "do the thing"}]
        session = cast(Any, _StubSkillSession(["debugging"]))

        _sync_skill_context_messages(messages, session)

        # No system message → skill context must land at index 0, before history.
        assert messages[0]["content"] == _skill_content("debugging")
        assert messages[1]["content"] == "do the thing"

    def test_inserts_after_system_message(self) -> None:
        messages: list[JsonObject] = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "do the thing"},
        ]
        session = cast(Any, _StubSkillSession(["debugging"]))

        _sync_skill_context_messages(messages, session)

        assert messages[0]["role"] == "system"
        assert messages[1]["content"] == _skill_content("debugging")
        assert messages[2]["content"] == "do the thing"

    def test_multiple_new_contexts_keep_activation_order(self) -> None:
        messages: list[JsonObject] = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "do the thing"},
        ]
        session = cast(Any, _StubSkillSession(["alpha", "beta"]))

        _sync_skill_context_messages(messages, session)

        # Order is preserved (not reversed by repeated insert at a fixed index).
        assert messages[1]["content"] == _skill_content("alpha")
        assert messages[2]["content"] == _skill_content("beta")
        assert messages[3]["content"] == "do the thing"

    def test_new_context_lands_behind_existing_skill_block(self) -> None:
        messages: list[JsonObject] = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": _skill_content("alpha")},
            {"role": "user", "content": "do the thing"},
        ]
        session = cast(Any, _StubSkillSession(["alpha", "beta"]))

        _sync_skill_context_messages(messages, session)

        # Existing "alpha" is not duplicated; new "beta" appends after it.
        assert messages[1]["content"] == _skill_content("alpha")
        assert messages[2]["content"] == _skill_content("beta")
        assert messages[3]["content"] == "do the thing"

    def test_no_duplicate_when_all_present(self) -> None:
        messages: list[JsonObject] = [
            {"role": "user", "content": _skill_content("alpha")},
            {"role": "user", "content": "do the thing"},
        ]
        session = cast(Any, _StubSkillSession(["alpha"]))

        _sync_skill_context_messages(messages, session)

        assert len(messages) == 2
