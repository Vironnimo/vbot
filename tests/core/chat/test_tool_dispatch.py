"""Tests for tool-call dispatch wiring through ``_dispatch_tool_calls``."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat.messages import JsonObject, ToolCall, ToolCallRejection
from core.chat.tool_dispatch import (
    ToolDispatchContext,
    _activate_triggered_skills,
    _resolve_tool_cwd,
)
from core.chat.tool_dispatch import (
    _dispatch_tool_calls as _dispatch_resolved_tool_calls,
)
from core.extensions import Deny, ExtensionRegistry, HookContext, Modify, Replace
from core.runs import TOOL_CALL_RESULT_EVENT, TOOL_CALL_STARTED_EVENT, Run, RunStatus
from core.sessions import ChatSessionManager
from core.skills import SkillRegistry
from core.tools import (
    ToolContext,
    ToolContract,
    ToolDisplay,
    ToolDisplayField,
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
    tools: dict[str, object] | None = None
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
    vbot_root = Path.cwd()


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


async def _dispatch_tool_calls(
    runtime: Any,
    agent: Any,
    tool_calls: list[ToolCall],
    session: Any,
    run: Run,
    *,
    nesting_depth: int,
    project_cwd: Path | None = None,
    project_id: str | None = None,
    skill_project_id: str | None = None,
    skill_registry: SkillRegistry | None = None,
    tool_restriction: Sequence[str] | None = None,
    base_allowed_tools: Sequence[str] | None = None,
    session_tool_grants: tuple[str, ...] = (),
    tool_contracts: Mapping[str, ToolContract] | None = None,
    tool_denial_resolver: Callable[[str], str | None] | None = None,
) -> tuple[list[Any], list[JsonObject]]:
    """Adapt runtime-shaped fixtures to the production Run-local context."""
    return await _dispatch_resolved_tool_calls(
        ToolDispatchContext(
            registry=runtime.tools,
            extension_registry=runtime.extensions,
            agent=agent,
            session=session,
            run=run,
            nesting_depth=nesting_depth,
            vbot_root=Path(runtime.system_prompts.vbot_root),
            data_root=Path(runtime.storage.data_dir),
            project_cwd=project_cwd,
            project_id=project_id,
            skill_project_id=skill_project_id,
            skill_registry=skill_registry,
            tool_restriction=tool_restriction,
            base_allowed_tools=base_allowed_tools,
            session_tool_grants=session_tool_grants,
            tool_contracts=tool_contracts or {},
            tool_denial_resolver=tool_denial_resolver,
        ),
        tool_calls,
    )


def _env_skill_registry(tmp_path: Path) -> SkillRegistry:
    skill_dir = tmp_path / "skills" / "provider-probe"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: provider-probe
description: Probe provider APIs.
metadata:
  vbot:
    requirements:
      env: OPENAI_API_KEY
---

Probe the provider.
""",
        encoding="utf-8",
    )
    return SkillRegistry.load(
        tmp_path / "skills",
        environment={"OPENAI_API_KEY": "available"},
    )


def test_triggered_env_skill_carries_bash_usage_guidance(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    registry = _env_skill_registry(tmp_path)
    agent = _StubAgent(
        id="coder",
        workspace=tmp_path / "workspace",
        allowed_skills=["provider-probe"],
    )

    _activate_triggered_skills(agent, session, "$provider-probe run a probe", registry)

    content = session.activated_skill_contents()["provider-probe"]
    assert content.index("<environment_access>") < content.index("Probe the provider.")
    assert "- `OPENAI_API_KEY`" in content
    assert "`env_keys` array of every `bash` call" in content


@pytest.mark.asyncio
async def test_dispatch_exposes_current_active_skill_env_grants(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []
    tools = ToolRegistry()

    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        seen.append(tuple(context.skill_env_keys))
        return tool_success({"status": "completed"})

    tools.register(
        "probe",
        "Probe ToolContext",
        {"type": "object", "properties": {}},
        probe,
        open_input_schema=True,
    )
    runtime, agent = _build_runtime_and_agent(tmp_path, tools)
    session = _build_session(tmp_path)
    session.register_skill_activation("provider-probe", "active content")
    registry = _env_skill_registry(tmp_path)
    run = Run(run_id="run-one", agent_id=agent.id, session_id=session.id)

    await _dispatch_tool_calls(
        runtime,
        agent,
        [ToolCall(id="call-one", name="probe", arguments={})],
        session,
        run,
        nesting_depth=0,
        skill_registry=registry,
    )

    assert seen == [("OPENAI_API_KEY",)]


@pytest.mark.asyncio
async def test_dispatch_exposes_parent_iteration_number(tmp_path: Path) -> None:
    seen: list[int] = []
    tools = ToolRegistry()

    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        seen.append(context.iteration_number)
        return tool_success({"status": "completed"})

    tools.register(
        "probe",
        "Probe ToolContext",
        {"type": "object", "properties": {}},
        probe,
        open_input_schema=True,
    )
    runtime, agent = _build_runtime_and_agent(tmp_path, tools)
    session = _build_session(tmp_path)
    run = Run(run_id="run-one", agent_id=agent.id, session_id=session.id)
    run.iteration_count = 3

    await _dispatch_tool_calls(
        runtime,
        agent,
        [ToolCall(id="call-one", name="probe", arguments={})],
        session,
        run,
        nesting_depth=0,
    )

    assert seen == [3]


@pytest.mark.asyncio
async def test_session_tool_grant_precedes_agent_and_run_dispatch_gates(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(
        "history",
        "Session history",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"ran": True}),
        session_scoped=True,
    )
    runtime, wildcard_agent = _build_runtime_and_agent(tmp_path, tools)
    agent = _StubAgent(
        id=wildcard_agent.id,
        workspace=wildcard_agent.workspace,
        allowed_tools=[],
    )
    session = _build_session(tmp_path)
    call = [ToolCall(id="history-call", name="history", arguments={})]

    unavailable, _ = await _dispatch_tool_calls(
        runtime,
        agent,
        call,
        session,
        Run(run_id="run-unavailable", agent_id=agent.id, session_id=session.id),
        nesting_depth=0,
        base_allowed_tools=("history",),
    )
    granted, _ = await _dispatch_tool_calls(
        runtime,
        agent,
        call,
        session,
        Run(run_id="run-granted", agent_id=agent.id, session_id=session.id),
        nesting_depth=0,
        base_allowed_tools=("history",),
        session_tool_grants=("history",),
    )
    restricted, _ = await _dispatch_tool_calls(
        runtime,
        agent,
        call,
        session,
        Run(run_id="run-restricted", agent_id=agent.id, session_id=session.id),
        nesting_depth=0,
        base_allowed_tools=("history",),
        session_tool_grants=("history",),
        tool_restriction=("read",),
    )

    assert _decode_tool_result(unavailable[0].content)["error"]["code"] == "history_unavailable"
    assert _decode_tool_result(granted[0].content) == tool_success({"ran": True})
    assert _decode_tool_result(restricted[0].content)["error"]["code"] == "tool_not_allowed"


@pytest.mark.asyncio
async def test_empty_additional_agent_targets_keep_subagent_dispatchable(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(
        "subagent",
        "Start a Sub-Agent",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"ran": True}),
    )
    runtime, wildcard_agent = _build_runtime_and_agent(tmp_path, tools)
    agent = _StubAgent(
        id=wildcard_agent.id,
        workspace=wildcard_agent.workspace,
        allowed_tools=["*"],
        tools={"subagent": {"allowed_agents": []}},
    )
    session = _build_session(tmp_path)

    messages, _ = await _dispatch_tool_calls(
        runtime,
        agent,
        [ToolCall(id="subagent-call", name="subagent", arguments={})],
        session,
        Run(run_id="run", agent_id=agent.id, session_id=session.id),
        nesting_depth=0,
        base_allowed_tools=("subagent",),
    )

    assert _decode_tool_result(messages[0].content) == tool_success({"ran": True})


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


@pytest.mark.asyncio
async def test_dispatch_validates_and_emits_exact_provider_cycle_contract(
    tmp_path: Path,
) -> None:
    handler_calls: list[JsonObject] = []

    def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        handler_calls.append(arguments)
        return tool_success({})

    tools = ToolRegistry()
    tools.register(
        "profiled",
        "Canonical broad Tool.",
        {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
            "additionalProperties": False,
        },
        handler,
    )
    definitions = [
        {
            "name": "profiled",
            "description": "Narrow Provider-cycle Tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["visible"],
                    }
                },
                "required": ["target"],
                "additionalProperties": False,
            },
        }
    ]
    contracts = tools.contracts_for_provider_definitions(definitions)
    runtime, agent = _build_runtime_and_agent(tmp_path, tools)
    session = _build_session(tmp_path)
    run = Run(run_id="run-profile", agent_id=agent.id, session_id=session.id)

    messages, _ = await _dispatch_tool_calls(
        runtime,
        agent,
        [ToolCall(id="profile-call", name="profiled", arguments={"target": "hidden"})],
        session,
        run,
        nesting_depth=0,
        tool_contracts=contracts,
    )

    result = _decode_tool_result(messages[0].content)
    started = next(event for event in run.events if event.type == TOOL_CALL_STARTED_EVENT)
    assert result["error"]["code"] == "invalid_arguments"
    assert handler_calls == []
    assert started.payload["schema_fingerprint"] == contracts["profiled"].schema_fingerprint


@pytest.mark.asyncio
async def test_dispatch_carries_final_ui_display_into_event_and_tool_message(
    tmp_path: Path,
) -> None:
    def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        context.add_display_count(10, "matches")
        return tool_success({"content": "matches"})

    tools = ToolRegistry()
    tools.register(
        "profiled",
        "Profiled Tool.",
        {"type": "object", "additionalProperties": True},
        handler,
        display=ToolDisplay(
            primary_candidates=(
                ToolDisplayField("description", kind="description", quote=True),
                ToolDisplayField("query", kind="query", quote=True),
            )
        ),
        open_input_schema=True,
    )
    runtime, agent = _build_runtime_and_agent(tmp_path, tools)
    session = _build_session(tmp_path)
    run = Run(run_id="run-display", agent_id=agent.id, session_id=session.id)

    messages, _ = await _dispatch_tool_calls(
        runtime,
        agent,
        [
            ToolCall(
                id="display-call",
                name="profiled",
                arguments={
                    "description": "Find every version variable",
                    "query": "VERSION_[A-Z_]+",
                },
            )
        ],
        session,
        run,
        nesting_depth=0,
    )

    started = next(event for event in run.events if event.type == TOOL_CALL_STARTED_EVENT)
    completed = next(event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT)
    assert started.payload["display"]["primary"][0]["value"] == ("Find every version variable")
    assert started.payload["display"]["facts"] == []
    assert completed.payload["display"]["facts"] == [
        {"kind": "count", "value": 10, "unit": "matches", "at_least": False}
    ]
    assert messages[0].tool_display == completed.payload["display"]


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


class TestReadMediaOutputs:
    """``read_media`` artifacts surface as rich Tool Result descriptors."""

    @pytest.mark.asyncio
    async def test_read_media_artifact_becomes_media_output(self, tmp_path: Path) -> None:
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

        tool_messages, media_outputs = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        assert len(tool_messages) == 1
        assert media_outputs == [
            {
                "tool_call_id": "call-1",
                "attachment_id": "att-1",
                "filename": "diagram.png",
                "media_type": "image/png",
            }
        ]

    @pytest.mark.asyncio
    async def test_non_read_media_artifacts_produce_no_media_output(self, tmp_path: Path) -> None:
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

        tool_messages, media_outputs = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        assert len(tool_messages) == 1
        assert media_outputs == []


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


class TestDispatchToolRestriction:
    """A per-run tool restriction narrows *dispatch* only (prompt-cache invariant)."""

    @staticmethod
    def _register_recording_tool(tools: ToolRegistry, name: str, ran: list[str]) -> None:
        def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            ran.append(name)
            return tool_success({"tool": name})

        tools.register(name, f"Recording stub for {name}.", {"type": "object"}, handler)

    @pytest.mark.asyncio
    async def test_restricted_out_tool_is_denied_and_never_runs(self, tmp_path: Path) -> None:
        # Wildcard agent: the restriction alone must gate dispatch.
        ran: list[str] = []
        tools = ToolRegistry()
        self._register_recording_tool(tools, "memory", ran)
        self._register_recording_tool(tools, "read_file", ran)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

        messages, _ = await _dispatch_tool_calls(
            runtime,
            agent,
            [
                ToolCall(id="call-mem", name="memory", arguments={}),
                ToolCall(id="call-read", name="read_file", arguments={}),
            ],
            session,
            run,
            nesting_depth=0,
            tool_restriction=("memory", "skill", "skill_manage"),
        )

        results = {
            message.tool_call_id: _decode_tool_result(message.content) for message in messages
        }
        # Only the allowed-and-restricted tool ran; the restricted-out one was denied.
        assert ran == ["memory"]
        assert results["call-mem"] == tool_success({"tool": "memory"})
        assert results["call-read"]["ok"] is False
        assert results["call-read"]["error"]["code"] == "tool_not_allowed"
        assert run.tool_call_names == {"memory", "read_file"}

    @pytest.mark.asyncio
    async def test_restriction_is_intersection_not_union(self, tmp_path: Path) -> None:
        # ``skill`` is in the restriction but NOT in the agent's effective allowlist
        # (a concrete list without it), so the intersection still denies it.
        ran: list[str] = []
        tools = ToolRegistry()
        self._register_recording_tool(tools, "skill", ran)
        self._register_recording_tool(tools, "read_file", ran)
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        agent = _StubAgent(id="coder", workspace=workspace, allowed_tools=["read_file"])
        runtime: Any = _StubRuntime(tools, tmp_path)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

        messages, _ = await _dispatch_tool_calls(
            runtime,
            agent,
            [ToolCall(id="call-skill", name="skill", arguments={})],
            session,
            run,
            nesting_depth=0,
            tool_restriction=("memory", "skill", "skill_manage"),
        )

        assert ran == []
        assert _decode_tool_result(messages[0].content)["error"]["code"] == "tool_not_allowed"

    @pytest.mark.asyncio
    async def test_no_restriction_leaves_dispatch_unchanged(self, tmp_path: Path) -> None:
        # ``tool_restriction=None`` is byte-identical to today: every allowed tool runs.
        ran: list[str] = []
        tools = ToolRegistry()
        self._register_recording_tool(tools, "read_file", ran)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

        messages, _ = await _dispatch_tool_calls(
            runtime,
            agent,
            [ToolCall(id="call-read", name="read_file", arguments={})],
            session,
            run,
            nesting_depth=0,
            tool_restriction=None,
        )

        assert ran == ["read_file"]
        assert _decode_tool_result(messages[0].content) == tool_success({"tool": "read_file"})
