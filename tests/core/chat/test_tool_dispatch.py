"""Tests for tool-call dispatch wiring through ``_dispatch_tool_calls``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat.messages import JsonObject, ToolCall
from core.chat.tool_dispatch import _dispatch_tool_calls
from core.runs import Run, RunStatus
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
    _app_dir = Path.cwd()


def _build_session(tmp_path: Path, agent_id: str = "coder", session_id: str = "session-one") -> Any:
    manager = ChatSessionManager(tmp_path)
    return manager.create(agent_id, session_id=session_id)


def _build_runtime_and_agent(
    tmp_path: Path, tools: ToolRegistry
) -> tuple[_StubRuntime, _StubAgent]:
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
        messages = await dispatch_task

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
        messages = await _dispatch_tool_calls(
            runtime,
            agent,
            second_tool_calls,
            session,
            run,
            nesting_depth=0,
        )
        assert _decode_tool_result(messages[0].content) == tool_success({"reused": True})


async def _wait_for_registry_entry(run: Run, tool_call_id: str, *, timeout: float) -> None:
    """Poll until the per-tool-call cancel registry has an entry for *tool_call_id*."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if tool_call_id in run._tool_cancel_callbacks:  # noqa: SLF001
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"per-tool-call cancel callback for {tool_call_id!r} was never registered")
