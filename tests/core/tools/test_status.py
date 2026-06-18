"""Tests for the built-in status tool."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import core.tools.status as status_tool_module
from core.agents.agents import Agent, AgentStore
from core.chat import ChatSessionError, CommandDispatcher, CommandHandled
from core.chat.chat import ChatMessage
from core.chat.commands import STATUS_PLACEHOLDER, build_status_text
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.projects import AgentResolutionError, AgentResolver, ConfigAgent
from core.runs import ChatRunManager, Run
from core.sessions import ChatSessionManager
from core.tools import ToolContext, ToolRegistry
from core.tools.status import STATUS_TOOL_NAME, register_status_tool


def _make_agent(*, model: str = "openai/gpt-5.2") -> Agent:
    return Agent(
        id="coder",
        name="Coder",
        model=model,
        fallback_model="openai/gpt-5.1",
        workspace="workspace",
        temperature=0.3,
        thinking_effort="none",
        allowed_tools=["*"],
        allowed_skills=["*"],
        created_at="2026-05-18T10:00:00+00:00",
        updated_at="2026-05-18T10:00:00+00:00",
    )


def _make_model(*, model_id: str = "gpt-5.2", name: str = "GPT-5.2") -> Model:
    return Model(
        model_id=model_id,
        name=name,
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=200_000,
        max_output_tokens=8_192,
    )


def _context(tmp_path: Path, *, project_id: str | None = None) -> ToolContext:
    return ToolContext(
        agent_id="coder",
        session_id="session-one",
        run_id="run-one",
        tool_call_id="call-one",
        tool_name=STATUS_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        app_root=tmp_path,
        data_root=tmp_path,
        project_id=project_id,
    )


async def _dispatch(
    registry: ToolRegistry,
    tmp_path: Path,
    arguments: dict[str, object] | None = None,
    *,
    project_id: str | None = None,
) -> dict[str, object]:
    return await registry.dispatch(
        _context(tmp_path, project_id=project_id),
        arguments or {},
        [STATUS_TOOL_NAME],
    )


class _StubResolver:
    """Resolver stub that returns a fixed agent regardless of project/agent id.

    Records the ``(project_id, agent_id)`` it was asked to resolve so a test can
    assert the handler threads ``context.project_id`` through.
    """

    def __init__(self, agent: Agent | ConfigAgent) -> None:
        self._agent = agent
        self.calls: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Agent | ConfigAgent:
        self.calls.append((project_id, agent_id))
        return self._agent


class _NotFoundResolver:
    def resolve_agent(self, _project_id: str | None, _agent_id: str) -> Agent:
        raise AgentResolutionError("Agent not found")


class _StubCommandAgents:
    """Bare AgentStore stub for the /status command path (no resolver seam)."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def get(self, _agent_id: str) -> Agent:
        return self._agent


class _StubSession:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._messages = messages

    def load(self) -> list[ChatMessage]:
        return list(self._messages)


class _StubSessions:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._session = _StubSession(messages)
        self.calls: list[tuple[str, str, str | None]] = []

    def get(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> _StubSession:
        self.calls.append((agent_id, session_id, project_id))
        return self._session


class _NotFoundSessions:
    def get(
        self, _agent_id: str, session_id: str, _project_id: str | None = None
    ) -> _StubSession:
        raise ChatSessionError(f"session does not exist: {session_id}")


class _StubModels:
    def __init__(self, model: Model) -> None:
        self._model = model

    def get(self, _provider_id: str, _model_id: str) -> Model:
        return self._model


class _RecordingModels:
    def __init__(self, model: Model) -> None:
        self._model = model
        self.calls: list[tuple[str, str]] = []

    def get(self, provider_id: str, model_id: str) -> Model:
        self.calls.append((provider_id, model_id))
        if provider_id != "openai" or model_id != "gpt-5.2":
            raise KeyError(model_id)
        return self._model


def test_status_tool_registered_with_correct_name() -> None:
    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, _StubResolver(_make_agent())),
        cast(ChatSessionManager, _StubSessions([])),
        cast(ModelRegistry, _StubModels(_make_model())),
        ChatRunManager(),
        None,
    )

    tool = registry.get(STATUS_TOOL_NAME)
    assert tool.name == STATUS_TOOL_NAME
    assert "Use session_id to check another session for this agent" in tool.description
    assert "Returns activity running/idle" not in tool.description
    assert list(tool.parameters["properties"]) == ["session_id", "agent_id"]


def test_status_tool_returns_text_with_full_deps(tmp_path: Path) -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    messages = [
        ChatMessage.user("Status check", timestamp=session_started),
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="All systems go.",
            usage={"input_tokens": 1234, "output_tokens": 42},
            timestamp=session_started,
        ),
    ]

    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, _StubResolver(_make_agent())),
        cast(ChatSessionManager, _StubSessions(messages)),
        cast(ModelRegistry, _StubModels(_make_model())),
        ChatRunManager(),
        datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    result = asyncio.run(_dispatch(registry, tmp_path))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    text = cast(str, data["text"])
    assert text
    assert "Agent: Coder (openai/gpt-5.2)" in text
    assert "Model display name: GPT-5.2" in text
    assert "Activity: idle" in text
    assert f"Run created at: {STATUS_PLACEHOLDER}" in text
    assert f"Run updated at: {STATUS_PLACEHOLDER}" in text
    assert data["activity"] == "idle"
    assert data["agent_id"] == "coder"
    assert data["session_id"] == "session-one"
    assert data["run_id"] is None
    assert data["created_at"] is None
    assert data["updated_at"] is None


def test_status_tool_returns_failure_when_agent_not_found(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, _NotFoundResolver()),
        cast(ChatSessionManager, _StubSessions([])),
        cast(ModelRegistry, _StubModels(_make_model())),
        ChatRunManager(),
        None,
    )

    result = asyncio.run(_dispatch(registry, tmp_path))

    assert result["ok"] is False
    error = cast(dict[str, str], result["error"])
    assert error["code"] == "agent_not_found"


def test_status_tool_returns_failure_when_session_not_found(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, _StubResolver(_make_agent())),
        cast(ChatSessionManager, _NotFoundSessions()),
        cast(ModelRegistry, _StubModels(_make_model())),
        ChatRunManager(),
        None,
    )

    result = asyncio.run(_dispatch(registry, tmp_path, {"session_id": "missing"}))

    assert result["ok"] is False
    error = cast(dict[str, str], result["error"])
    assert error["code"] == "session_not_found"


def test_status_tool_rejects_agent_id_without_session_id(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, _StubResolver(_make_agent())),
        cast(ChatSessionManager, _StubSessions([])),
        cast(ModelRegistry, _StubModels(_make_model())),
        ChatRunManager(),
        None,
    )

    result = asyncio.run(_dispatch(registry, tmp_path, {"agent_id": "coder"}))

    assert result["ok"] is False
    error = cast(dict[str, str], result["error"])
    assert error["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_status_tool_reports_running_target_session(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    chat_runs = ChatRunManager()

    async def execute(_run: Run) -> str:
        started.set()
        await release.wait()
        return "done"

    run = await chat_runs.start(
        agent_id="reviewer",
        session_id="session-two",
        executor=execute,
    )
    await started.wait()
    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, _StubResolver(_make_agent())),
        cast(ChatSessionManager, _StubSessions([])),
        cast(ModelRegistry, _StubModels(_make_model())),
        chat_runs,
        None,
    )

    result = await registry.dispatch(
        _context(tmp_path),
        {"agent_id": "reviewer", "session_id": "session-two"},
        [STATUS_TOOL_NAME],
    )
    expected_updated_at = run.updated_at
    release.set()
    await run.wait()

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    text = cast(str, data["text"])
    assert "Activity: running" in text
    assert f"Run created at: {run.created_at}" in text
    assert f"Run updated at: {expected_updated_at}" in text
    assert data["activity"] == "running"
    assert data["agent_id"] == "reviewer"
    assert data["session_id"] == "session-two"
    assert data["run_id"] == run.id
    assert data["created_at"] == run.created_at
    assert data["updated_at"] == expected_updated_at


def test_status_tool_matches_status_command_for_registry_display(tmp_path: Path) -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    started_at = datetime(2026, 5, 18, 9, 0, tzinfo=UTC)
    messages = [
        ChatMessage.user("Status check", timestamp=session_started),
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="All systems go.",
            usage={"input_tokens": 1234, "output_tokens": 42},
            timestamp=session_started,
        ),
    ]
    agent = _make_agent()
    # The /status command path keeps the bare AgentStore (its resolution is not
    # part of the run-path resolver switch); the status tool now takes the
    # resolver. Both wrap the same agent so the two renderings stay comparable.
    command_agents = cast(AgentStore, _StubCommandAgents(agent))
    resolver = cast(AgentResolver, _StubResolver(agent))
    sessions = cast(ChatSessionManager, _StubSessions(messages))
    models = cast(ModelRegistry, _StubModels(_make_model(name="GPT-5.2 Registry")))

    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agents=command_agents,
        sessions=sessions,
        models=models,
        started_at=started_at,
    )
    command_result = dispatcher.dispatch("coder", "session-one", "/status")
    assert isinstance(command_result, CommandHandled)
    assert command_result.reply is not None

    registry = ToolRegistry()
    chat_runs = ChatRunManager()
    register_status_tool(registry, resolver, sessions, models, chat_runs, started_at)
    tool_result = asyncio.run(_dispatch(registry, tmp_path))

    assert tool_result["ok"] is True
    data = cast(dict[str, Any], tool_result["data"])
    text = cast(str, data["text"])

    def _without_live_time_lines(status_text: str) -> list[str]:
        return [
            line
            for line in status_text.splitlines()
            if not line.startswith(("Session started:", "App uptime:", "Current time:"))
        ]

    assert _without_live_time_lines(text) == _without_live_time_lines(command_result.reply)
    assert "Model display name: GPT-5.2 Registry" in text


def test_status_tool_strips_pinned_suffix_before_registry_lookup(tmp_path: Path) -> None:
    recording_models = _RecordingModels(_make_model(name="GPT-5.2 Registry"))
    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, _StubResolver(_make_agent(model="openai/gpt-5.2::primary"))),
        cast(ChatSessionManager, _StubSessions([])),
        cast(ModelRegistry, recording_models),
        ChatRunManager(),
        None,
    )

    result = asyncio.run(_dispatch(registry, tmp_path))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    text = cast(str, data["text"])
    assert "Model display name: GPT-5.2 Registry" in text
    assert recording_models.calls == [("openai", "gpt-5.2")]


def test_status_tool_splits_selected_and_actual_thinking_effort(tmp_path: Path) -> None:
    """The tool reports the selection and the ladder-snapped wire effort separately."""
    agent = Agent(
        id="coder",
        name="Coder",
        model="openai/gpt-5.2",
        fallback_model="openai/gpt-5.1",
        workspace="workspace",
        temperature=0.3,
        thinking_effort="max",
        allowed_tools=["*"],
        allowed_skills=["*"],
        created_at="2026-05-18T10:00:00+00:00",
        updated_at="2026-05-18T10:00:00+00:00",
    )
    model = Model(
        model_id="gpt-5.2",
        name="GPT-5.2",
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True,
                control="levels",
                levels=("low", "medium", "high"),
            ),
        ),
        context_window=200_000,
        max_output_tokens=8_192,
    )

    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, _StubResolver(agent)),
        cast(ChatSessionManager, _StubSessions([])),
        cast(ModelRegistry, _StubModels(model)),
        ChatRunManager(),
        None,
    )

    result = asyncio.run(_dispatch(registry, tmp_path))

    data = cast(dict[str, Any], result["data"])
    text = cast(str, data["text"])
    assert "Selected thinking effort: max" in text
    assert "Actual model thinking effort: high" in text


def _make_config_agent() -> ConfigAgent:
    """A resolved project config-agent profile, as the resolver would return."""
    return ConfigAgent(
        id="orchestrator",
        name="Orchestrator",
        model="openai/gpt-5.2",
        temperature=None,
        allowed_tools=["*"],
        allowed_skills=["*"],
        body="You orchestrate the team.",
        source_path=Path("/repo/.opencode/agents/orchestrator.md"),
        source_format="opencode",
    )


def test_status_tool_resolves_project_agent_profile(tmp_path: Path) -> None:
    """For a project run, /status shows the resolved config-agent profile.

    The handler must thread ``context.project_id`` into the resolver (so the
    project Team's config agent is shown, not an identity-store agent) and into
    the session lookup (so the project-anchored session is found).
    """
    resolver = _StubResolver(_make_config_agent())
    sessions = _StubSessions(
        [
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content="Ready.",
                usage={"input_tokens": 10, "output_tokens": 2},
                timestamp=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
            ),
        ]
    )

    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, resolver),
        cast(ChatSessionManager, sessions),
        cast(ModelRegistry, _StubModels(_make_model())),
        ChatRunManager(),
        None,
    )

    result = asyncio.run(_dispatch(registry, tmp_path, project_id="vbot"))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    text = cast(str, data["text"])
    assert "Agent: Orchestrator (openai/gpt-5.2)" in text
    # The project id reached both the resolver and the project-anchored session.
    assert resolver.calls == [("vbot", "coder")]
    assert sessions.calls == [("coder", "session-one", "vbot")]


def test_status_tool_identity_run_resolves_without_project(tmp_path: Path) -> None:
    """An identity run resolves with ``project_id=None`` — unchanged behavior."""
    resolver = _StubResolver(_make_agent())
    sessions = _StubSessions([])

    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, resolver),
        cast(ChatSessionManager, sessions),
        cast(ModelRegistry, _StubModels(_make_model())),
        ChatRunManager(),
        None,
    )

    result = asyncio.run(_dispatch(registry, tmp_path))

    assert result["ok"] is True
    assert resolver.calls == [(None, "coder")]
    assert sessions.calls == [("coder", "session-one", None)]


def test_build_status_text_is_single_source_of_truth() -> None:
    assert status_tool_module.build_status_reply.__module__ == build_status_text.__module__
