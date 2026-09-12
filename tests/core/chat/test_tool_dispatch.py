"""Tests for tool dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat._skill_activation import _activate_triggered_skills
from core.chat.messages import ChatMessage, JsonObject, ToolCall
from core.chat.tool_dispatch import (
    _resolve_tool_cwd,
)
from core.runs import TOOL_CALL_RESULT_EVENT, TOOL_CALL_STARTED_EVENT, Run
from core.skills import SkillRegistry
from core.tools import (
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolRegistry,
    tool_success,
)
from tests.core.chat.tool_dispatch_test_support import (
    _build_runtime_and_agent,
    _build_session,
    _decode_tool_result,
    _dispatch_tool_calls,
    _StubAgent,
    _StubRuntime,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


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
async def test_dispatch_revokes_skill_env_grants_after_compaction(tmp_path: Path) -> None:
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
    session.activate_skill_context("provider-probe", {"activation_content": "active content"})
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted",
            projection=[ChatMessage.user("Tail")],
            compacted_token_count=10,
        )
    )
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
    session.register_skill_activation("provider-probe", "reloaded content")
    await _dispatch_tool_calls(
        runtime,
        agent,
        [ToolCall(id="call-two", name="probe", arguments={})],
        session,
        run,
        nesting_depth=0,
        skill_registry=registry,
    )

    assert seen == [(), ("OPENAI_API_KEY",)]


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


@pytest.mark.asyncio
async def test_delegated_tools_receive_run_restrictions_and_live_denials(tmp_path: Path) -> None:
    observed: list[ToolContext] = []

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        observed.append(context)
        return tool_success({"ran": True})

    def deny_remote(name: str) -> str | None:
        return "test-owned-denial" if name == "remote" else None

    tools = ToolRegistry()
    tools.register("connection", "Test-owned connection.", {"type": "object"}, handler)
    runtime, agent = _build_runtime_and_agent(tmp_path, tools)
    session = _build_session(tmp_path)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _dispatch_tool_calls(
        runtime,
        agent,
        [ToolCall(id="call-1", name="connection", arguments={})],
        session,
        run,
        nesting_depth=0,
        tool_restriction=("connection",),
        tool_denial_resolver=deny_remote,
    )

    assert len(observed) == 1
    assert observed[0].tool_restriction == ("connection",)
    assert observed[0].tool_denial_resolver is deny_remote
    assert observed[0].tool_denial_resolver("remote") == "test-owned-denial"
