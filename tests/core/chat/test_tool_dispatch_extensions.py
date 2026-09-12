"""Tests for tool dispatch extensions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat.messages import JsonObject, ToolCall, ToolCallRejection
from core.extensions import Deny, ExtensionRegistry, HookContext, Modify, Replace
from core.runs import TOOL_CALL_STARTED_EVENT, Run
from core.tools import (
    ToolContext,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from tests.core.chat.tool_dispatch_test_support import (
    _build_runtime_and_agent,
    _build_session,
    _decode_tool_result,
    _dispatch_tool_calls,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


def _started_event_arguments(run: Run) -> JsonObject:
    """Return the arguments recorded on the run's tool_call_started event."""
    for event in run.events:
        if event.type == TOOL_CALL_STARTED_EVENT:
            return cast(JsonObject, event.payload["tool_call"]["arguments"])
    raise AssertionError("no tool_call_started event was emitted")


class TestExtensionDecisionWiring:
    """The tool_call decision model wired through ``_dispatch_tool_calls``."""

    @pytest.mark.asyncio
    async def test_rejected_call_bypasses_hooks_and_handler(self, tmp_path: Path) -> None:
        executed: list[str] = []
        hook_calls: list[str] = []
        tools = ToolRegistry()

        def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            executed.append(context.tool_call_id)
            return tool_success({"ran": True})

        tools.register(
            "echo",
            "Echo input.",
            {"type": "object"},
            handler,
        )
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        registry = ExtensionRegistry()
        registry.install_handler(
            "observer",
            "tool_call",
            lambda _context, **_payload: hook_calls.append("tool_call"),
        )
        registry.install_handler(
            "observer",
            "tool_result",
            lambda _context, **_payload: hook_calls.append("tool_result"),
        )
        runtime.extensions = registry
        session = _build_session(tmp_path)
        run = Run(run_id="run-one", agent_id=agent.id, session_id=session.id)

        messages, _ = await _dispatch_tool_calls(
            runtime,
            agent,
            [
                ToolCall(
                    id="call-bad",
                    name="echo",
                    arguments={},
                    rejection=ToolCallRejection(
                        code="malformed_tool_arguments",
                        message="Arguments were malformed.",
                        fingerprint="sha256",
                    ),
                )
            ],
            session,
            run,
            nesting_depth=0,
        )

        assert executed == []
        assert hook_calls == []
        assert _decode_tool_result(messages[0].content) == tool_failure(
            "malformed_tool_arguments",
            "Arguments were malformed.",
            retryable=False,
        )

    @pytest.mark.asyncio
    async def test_invalid_recovered_call_does_not_block_valid_sequence_sibling(
        self,
        tmp_path: Path,
    ) -> None:
        executed: list[str] = []
        tools = ToolRegistry()

        def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            executed.append(context.tool_call_id)
            return tool_success({"ran": context.tool_call_id})

        tools.register(
            "echo",
            "Echo input.",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            handler,
        )
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-one", agent_id=agent.id, session_id=session.id)

        messages, _ = await _dispatch_tool_calls(
            runtime,
            agent,
            [
                ToolCall(
                    id="call-invalid",
                    name="echo",
                    arguments={},
                    argument_sequence_index=0,
                    argument_sequence_length=2,
                ),
                ToolCall(
                    id="call-valid",
                    name="echo",
                    arguments={"value": "ok"},
                    argument_sequence_index=1,
                    argument_sequence_length=2,
                ),
            ],
            session,
            run,
            nesting_depth=0,
        )

        results = [_decode_tool_result(message.content) for message in messages]
        assert results[0]["error"]["code"] == "invalid_arguments"
        assert results[1] == tool_success({"ran": "call-valid"})
        assert executed == ["call-valid"]

    @pytest.mark.asyncio
    async def test_recovered_sequence_uses_normal_parallel_policy(self, tmp_path: Path) -> None:
        active_count = 0
        max_active_count = 0
        both_calls_started = asyncio.Event()

        async def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            nonlocal active_count, max_active_count
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            if active_count == 2:
                both_calls_started.set()
            try:
                await asyncio.wait_for(both_calls_started.wait(), timeout=1)
                return tool_success({"ran": context.tool_call_id})
            finally:
                active_count -= 1

        tools = ToolRegistry()
        tools.register(
            "echo",
            "Echo input.",
            {"type": "object", "additionalProperties": False},
            handler,
        )
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-one", agent_id=agent.id, session_id=session.id)

        messages, _ = await _dispatch_tool_calls(
            runtime,
            agent,
            [
                ToolCall(
                    id="call-first",
                    name="echo",
                    arguments={},
                    argument_sequence_index=0,
                    argument_sequence_length=2,
                ),
                ToolCall(
                    id="call-second",
                    name="echo",
                    arguments={},
                    argument_sequence_index=1,
                    argument_sequence_length=2,
                ),
            ],
            session,
            run,
            nesting_depth=0,
        )

        assert max_active_count == 2
        assert [_decode_tool_result(message.content) for message in messages] == [
            tool_success({"ran": "call-first"}),
            tool_success({"ran": "call-second"}),
        ]

    @pytest.mark.asyncio
    async def test_tool_hooks_serialize_without_serializing_tool_handlers(
        self, tmp_path: Path
    ) -> None:
        active_tools = 0
        max_active_tools = 0
        active_hooks = 0
        max_active_hooks = 0
        both_tools_started = asyncio.Event()

        async def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            nonlocal active_tools, max_active_tools
            active_tools += 1
            max_active_tools = max(max_active_tools, active_tools)
            if active_tools == 2:
                both_tools_started.set()
            await asyncio.wait_for(both_tools_started.wait(), timeout=1)
            active_tools -= 1
            return tool_success({"ran": True})

        async def hook(_context: HookContext, **_payload: Any) -> None:
            nonlocal active_hooks, max_active_hooks
            active_hooks += 1
            max_active_hooks = max(max_active_hooks, active_hooks)
            await asyncio.sleep(0.01)
            active_hooks -= 1

        tools = ToolRegistry()
        tools.register(
            "safe_read",
            "Parallel-safe test tool.",
            {"type": "object", "additionalProperties": False},
            handler,
            parallel_safe=True,
        )
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        registry = ExtensionRegistry()
        registry.install_handler("observer", "tool_call", hook)
        registry.install_handler("observer", "tool_result", hook)
        runtime.extensions = registry
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

        await _dispatch_tool_calls(
            runtime,
            agent,
            [
                ToolCall(id="call-1", name="safe_read", arguments={}),
                ToolCall(id="call-2", name="safe_read", arguments={}),
            ],
            session,
            run,
            nesting_depth=0,
        )

        assert max_active_tools == 2
        assert max_active_hooks == 1

    @pytest.mark.asyncio
    async def test_runtime_denial_precedes_handlers_and_extension_hooks(
        self, tmp_path: Path
    ) -> None:
        handler_calls: list[str] = []
        hook_calls: list[str] = []
        denial = (
            "Tool access denied: the current sender is a group member. "
            "Group members may use only web_search and web_fetch."
        )

        def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            handler_calls.append(context.tool_call_id)
            return tool_success({"ran": True})

        def hook(_context: HookContext, **_payload: Any) -> None:
            hook_calls.append("called")

        tools = ToolRegistry()
        tools.register(
            "guarded",
            "Guarded tool.",
            {"type": "object"},
            handler,
            parallel_safe=True,
        )
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        registry = ExtensionRegistry()
        registry.install_handler("observer", "tool_call", hook)
        registry.install_handler("observer", "tool_result", hook)
        runtime.extensions = registry
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

        messages, _ = await _dispatch_tool_calls(
            runtime,
            agent,
            [
                ToolCall(id="call-1", name="guarded", arguments={}),
                ToolCall(id="call-2", name="guarded", arguments={}),
            ],
            session,
            run,
            nesting_depth=0,
            tool_denial_resolver=lambda _tool_name: denial,
        )

        assert handler_calls == []
        assert hook_calls == []
        assert [_decode_tool_result(message.content) for message in messages] == [
            {
                "ok": False,
                "error": {"code": "tool_not_allowed", "message": denial},
                "data": None,
                "artifacts": [],
            },
            {
                "ok": False,
                "error": {"code": "tool_not_allowed", "message": denial},
                "data": None,
                "artifacts": [],
            },
        ]
        assert "Do not retry" not in denial

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
