"""Tests for slash command dispatch."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest

from core.agents.agents import Agent, AgentStore
from core.chat import (
    ChatMessage,
    ChatRunManager,
    CommandDispatcher,
    CommandHandled,
    NotACommand,
    Run,
    RunCancelledError,
)
from core.chat.chat import ChatSessionManager
from core.chat.commands import STATUS_PLACEHOLDER, build_status_text
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities


def _make_agent(
    *,
    model: str = "openai/gpt-5.2",
    fallback_model: str = "openai/gpt-5.1",
    temperature: float = 0.3,
    thinking_effort: str = "none",
) -> Agent:
    return Agent(
        id="coder",
        name="Coder",
        model=model,
        fallback_model=fallback_model,
        workspace="workspace",
        temperature=temperature,
        thinking_effort=thinking_effort,
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


class _StubAgents:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self.update_calls: list[tuple[str, dict[str, object]]] = []

    def get(self, _agent_id: str) -> Agent:
        return self._agent

    def update(self, agent_id: str, **changes: object) -> Agent:
        self.update_calls.append((agent_id, dict(changes)))
        return self._agent


class _StubSession:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._messages = messages

    def load(self) -> list[ChatMessage]:
        return list(self._messages)


class _StubCreatedSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id


class _StubSessions:
    def __init__(
        self,
        messages: list[ChatMessage] | None = None,
        created_session_id: str = "session-new",
    ) -> None:
        self._session = _StubSession(messages or [])
        self._created_session_id = created_session_id
        self.create_calls: list[str] = []

    def get(self, _agent_id: str, _session_id: str) -> _StubSession:
        return self._session

    def create(self, agent_id: str) -> _StubCreatedSession:
        self.create_calls.append(agent_id)
        return _StubCreatedSession(self._created_session_id)


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


@pytest.mark.asyncio
async def test_dispatch_stop_with_active_run_returns_cancelled_reply() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        started.set()
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await started.wait()

    dispatcher = CommandDispatcher(manager)
    result = dispatcher.dispatch("coder", "session-one", " /STOP ")

    assert isinstance(result, CommandHandled)
    assert result.reply == "Run cancelled."
    assert run.cancel_requested is True

    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()


def test_dispatch_stop_with_no_active_run_returns_not_found_reply() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/stop")

    assert isinstance(result, CommandHandled)
    assert result.reply == "No active run to cancel."


def test_dispatch_unknown_command_returns_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/bogus")

    assert isinstance(result, NotACommand)


def test_dispatch_non_command_message_returns_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "hello")

    assert isinstance(result, NotACommand)


def test_built_in_commands_include_compact() -> None:
    assert "compact" in CommandDispatcher.BUILT_IN_COMMANDS


def test_built_in_commands_include_new() -> None:
    assert "new" in CommandDispatcher.BUILT_IN_COMMANDS


def test_dispatch_new_creates_session_and_returns_reply() -> None:
    agents = _StubAgents(_make_agent())
    sessions = _StubSessions(created_session_id="session-fresh")
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agents=cast(AgentStore, agents),
        sessions=cast(ChatSessionManager, sessions),
    )

    result = dispatcher.dispatch("coder", "session-one", "/new")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert "session-fresh" in result.reply
    assert sessions.create_calls == ["coder"]
    assert agents.update_calls == [("coder", {"current_session_id": "session-fresh"})]


@pytest.mark.asyncio
async def test_dispatch_new_blocked_with_active_run() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        started.set()
        await release.wait()
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await started.wait()

    agents = _StubAgents(_make_agent())
    sessions = _StubSessions(created_session_id="session-fresh")
    dispatcher = CommandDispatcher(
        manager,
        agents=cast(AgentStore, agents),
        sessions=cast(ChatSessionManager, sessions),
    )

    try:
        result = dispatcher.dispatch("coder", "session-one", "/new")

        assert isinstance(result, CommandHandled)
        assert result.reply is not None
        assert "after the current run finishes" in result.reply
        assert sessions.create_calls == []
        assert agents.update_calls == []
    finally:
        release.set()
        assert await run.wait() == "done"


def test_dispatch_new_without_session_manager_returns_unavailable_reply() -> None:
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agents=cast(AgentStore, _StubAgents(_make_agent())),
        sessions=None,
    )

    result = dispatcher.dispatch("coder", "session-one", "/new")

    assert isinstance(result, CommandHandled)
    assert result.reply == "Session management is not available."


def test_dispatch_status_with_no_deps_returns_degraded_reply() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/status")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert result.reply != ""
    assert f"Agent: {STATUS_PLACEHOLDER}" in result.reply
    assert "Current time:" in result.reply


def test_dispatch_status_with_full_deps_returns_reply_with_expected_fields() -> None:
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
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agents=cast(AgentStore, _StubAgents(_make_agent())),
        sessions=cast(ChatSessionManager, _StubSessions(messages)),
        models=cast(ModelRegistry, _StubModels(_make_model())),
        started_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    result = dispatcher.dispatch("coder", "session-one", "/status")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert "Agent: Coder (openai/gpt-5.2)" in result.reply
    assert "Model display name: GPT-5.2" in result.reply
    assert "Context usage: 1234 / 200000" in result.reply
    assert "Current time:" in result.reply


def test_dispatch_status_strips_pinned_suffix_before_registry_lookup() -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    messages = [
        ChatMessage.user("Status check", timestamp=session_started),
    ]
    models = _RecordingModels(_make_model(name="GPT-5.2 Registry"))
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agents=cast(AgentStore, _StubAgents(_make_agent(model="openai/gpt-5.2::primary"))),
        sessions=cast(ChatSessionManager, _StubSessions(messages)),
        models=cast(ModelRegistry, models),
        started_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    result = dispatcher.dispatch("coder", "session-one", "/status")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert "Model display name: GPT-5.2 Registry" in result.reply
    assert models.calls == [("openai", "gpt-5.2")]


def test_build_status_text_degraded_with_no_data() -> None:
    text = build_status_text(None, [], None, None)

    assert f"Agent: {STATUS_PLACEHOLDER}" in text
    assert f"Model display name: {STATUS_PLACEHOLDER}" in text
    assert f"Fallback model: {STATUS_PLACEHOLDER}" in text
    assert f"Thinking effort: {STATUS_PLACEHOLDER}" in text
    assert f"Temperature: {STATUS_PLACEHOLDER}" in text
    assert f"Context usage: {STATUS_PLACEHOLDER}" in text
    assert f"Session started: {STATUS_PLACEHOLDER}" in text
    assert f"Turn count: {STATUS_PLACEHOLDER}" in text
    assert f"App uptime: {STATUS_PLACEHOLDER}" in text
    assert "Current time:" in text


def test_build_status_text_with_full_data() -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    messages = [
        ChatMessage.user("Status check", timestamp=session_started),
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="All systems go.",
            usage={"input_tokens": 987, "output_tokens": 12, "estimated": True},
            timestamp=session_started,
        ),
    ]

    text = build_status_text(
        _make_agent(),
        messages,
        context_window=200_000,
        started_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    assert "Agent: Coder (openai/gpt-5.2)" in text
    assert "Model display name: gpt-5.2" in text
    assert "Fallback model: openai/gpt-5.1" in text
    assert "Thinking effort: none" in text
    assert "Temperature: 0.3" in text
    assert "Context usage: ~987 / 200000" in text
    assert "Session started:" in text
    assert "Turn count: 1" in text
    assert "App uptime:" in text
    assert "Current time:" in text
