"""Tests for the built-in status tool."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import core.tools.status as status_tool_module
from core.agents.agents import Agent, AgentNotFoundError, AgentStore
from core.chat import ChatRunManager, CommandDispatcher, CommandHandled
from core.chat.chat import ChatMessage, ChatSessionManager
from core.chat.commands import STATUS_PLACEHOLDER, build_status_text
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
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


def _context(tmp_path: Path) -> ToolContext:
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
    )


async def _dispatch(
    registry: ToolRegistry,
    tmp_path: Path,
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    return await registry.dispatch(
        _context(tmp_path),
        arguments or {},
        [STATUS_TOOL_NAME],
    )


class _StubAgents:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def get(self, _agent_id: str) -> Agent:
        return self._agent


class _NotFoundAgents:
    def get(self, _agent_id: str) -> Agent:
        raise AgentNotFoundError("Agent not found")


class _StubSession:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._messages = messages

    def load(self) -> list[ChatMessage]:
        return list(self._messages)


class _StubSessions:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._session = _StubSession(messages)

    def get(self, _agent_id: str, _session_id: str) -> _StubSession:
        return self._session


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
        cast(AgentStore, _StubAgents(_make_agent())),
        cast(ChatSessionManager, _StubSessions([])),
        cast(ModelRegistry, _StubModels(_make_model())),
        None,
    )

    tool = registry.get(STATUS_TOOL_NAME)
    assert tool.name == STATUS_TOOL_NAME


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
        cast(AgentStore, _StubAgents(_make_agent())),
        cast(ChatSessionManager, _StubSessions(messages)),
        cast(ModelRegistry, _StubModels(_make_model())),
        datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    result = asyncio.run(_dispatch(registry, tmp_path))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    text = cast(str, data["text"])
    assert text
    assert "Agent: Coder (openai/gpt-5.2)" in text
    assert "Model display name: GPT-5.2" in text


def test_status_tool_degrades_gracefully_when_agent_not_found(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentStore, _NotFoundAgents()),
        cast(ChatSessionManager, _StubSessions([])),
        cast(ModelRegistry, _StubModels(_make_model())),
        None,
    )

    result = asyncio.run(_dispatch(registry, tmp_path))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    text = cast(str, data["text"])
    assert f"Agent: {STATUS_PLACEHOLDER}" in text


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
    agents = cast(AgentStore, _StubAgents(_make_agent()))
    sessions = cast(ChatSessionManager, _StubSessions(messages))
    models = cast(ModelRegistry, _StubModels(_make_model(name="GPT-5.2 Registry")))

    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agents=agents,
        sessions=sessions,
        models=models,
        started_at=started_at,
    )
    command_result = dispatcher.dispatch("coder", "session-one", "/status")
    assert isinstance(command_result, CommandHandled)
    assert command_result.reply is not None

    registry = ToolRegistry()
    register_status_tool(registry, agents, sessions, models, started_at)
    tool_result = asyncio.run(_dispatch(registry, tmp_path))

    assert tool_result["ok"] is True
    data = cast(dict[str, Any], tool_result["data"])
    text = cast(str, data["text"])
    assert text == command_result.reply
    assert "Model display name: GPT-5.2 Registry" in text


def test_status_tool_strips_pinned_suffix_before_registry_lookup(tmp_path: Path) -> None:
    recording_models = _RecordingModels(_make_model(name="GPT-5.2 Registry"))
    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentStore, _StubAgents(_make_agent(model="openai/gpt-5.2::primary"))),
        cast(ChatSessionManager, _StubSessions([])),
        cast(ModelRegistry, recording_models),
        None,
    )

    result = asyncio.run(_dispatch(registry, tmp_path))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    text = cast(str, data["text"])
    assert "Model display name: GPT-5.2 Registry" in text
    assert recording_models.calls == [("openai", "gpt-5.2")]


def test_build_status_text_is_single_source_of_truth() -> None:
    assert status_tool_module.build_status_text is build_status_text
