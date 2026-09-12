"""Tests for chat methods model title."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.projects import ModelConfigurationError
from core.sessions import (
    SessionAddress,
)
from server.events import ServerEventBus
from tests.server.rpc.chat_methods_test_support import (
    _execute_core_command,
)


# ---------------------------------------------------------------------------
# /model set: identity vs project routing + usable-model validation.
# ---------------------------------------------------------------------------
class _RecordingAgents:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def update(self, agent_id: str, **changes: Any) -> Any:
        self.updates.append((agent_id, changes))
        return SimpleNamespace(id=agent_id, **changes)


class _RecordingProjects:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str, str, Any]] = []
        self.clear_calls: list[tuple[str, str, str]] = []

    def set_override(self, project_id: str, agent_id: str, field: str, value: Any) -> Any:
        self.set_calls.append((project_id, agent_id, field, value))
        return SimpleNamespace(project_id=project_id)

    def clear_override(self, project_id: str, agent_id: str, field: str) -> Any:
        self.clear_calls.append((project_id, agent_id, field))
        return SimpleNamespace(project_id=project_id)


class _ModelResolver:
    """Model-validation stub: only the configured set is usable."""

    def __init__(self, configured: set[str]) -> None:
        self._configured = configured

    def require_model_configured(self, model: str) -> None:
        if model not in self._configured:
            raise ModelConfigurationError(f"model is not configured: {model}")


def _make_model_state(
    *, configured: set[str], agents: _RecordingAgents, projects: _RecordingProjects, models: Any
) -> SimpleNamespace:
    runtime = SimpleNamespace(
        agent_resolver=_ModelResolver(configured),
        agents=agents,
        projects=projects,
        models=models,
    )
    return SimpleNamespace(runtime=runtime, chat_runs=SimpleNamespace())


@pytest.mark.asyncio
async def test_set_model_identity_updates_agent_model() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured={"openai/gpt-5"}, agents=agents, projects=projects, models=SimpleNamespace()
    )

    result = await _execute_core_command(
        state, "/model openai/gpt-5", agent_id="coder", project_id=None
    )

    # Identity session writes the agent's own model; the project store is untouched.
    assert agents.updates == [("coder", {"model": "openai/gpt-5"})]
    assert projects.set_calls == []
    assert result.facts == {"agent_id": "coder", "model": "openai/gpt-5"}
    assert result.feedback is not None
    assert result.feedback.kind == "notice"


@pytest.mark.asyncio
async def test_set_model_identity_reset_clears_model() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured=set(), agents=agents, projects=projects, models=SimpleNamespace()
    )

    result = await _execute_core_command(state, "/model reset", agent_id="coder", project_id=None)

    # reset writes an empty model (falls to the global default) and skips validation.
    assert agents.updates == [("coder", {"model": ""})]
    assert result.facts["model"] == ""


@pytest.mark.asyncio
async def test_set_model_project_writes_override() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured={"openai/gpt-mini"}, agents=agents, projects=projects, models=SimpleNamespace()
    )

    await _execute_core_command(state, "/model openai/gpt-mini", project_id="vbot")

    # Project session writes a per-agent model override; the identity store is untouched.
    assert projects.set_calls == [("vbot", "builder", "model", "openai/gpt-mini")]
    assert agents.updates == []


# ---------------------------------------------------------------------------
# /rename command -> rename_session action
# ---------------------------------------------------------------------------
class _RecordingTitleSessions:
    def __init__(self) -> None:
        self.renamed: list[tuple[str, str, str, str | None]] = []

    def set_title(self, address: SessionAddress, title: str) -> str | None:
        self.renamed.append((address.agent_id, address.session_id, title, address.project_id))
        normalized = " ".join(title.split())
        return normalized or None


def _make_rename_state(sessions: _RecordingTitleSessions) -> SimpleNamespace:
    runtime = SimpleNamespace(chat_sessions=sessions)
    return SimpleNamespace(
        runtime=runtime,
        chat_runs=SimpleNamespace(),
        event_bus=ServerEventBus(),
    )


@pytest.mark.asyncio
async def test_rename_command_sets_title_with_toast() -> None:
    sessions = _RecordingTitleSessions()
    state = _make_rename_state(sessions)

    result = await _execute_core_command(
        state, "/rename Release planning", agent_id="coder", project_id=None
    )

    assert sessions.renamed == [("coder", "s1", "Release planning", None)]
    assert result.feedback is not None
    assert result.feedback.kind == "notice"
    assert result.facts == {"session_id": "s1", "title": "Release planning"}


@pytest.mark.asyncio
async def test_rename_command_without_argument_clears() -> None:
    sessions = _RecordingTitleSessions()
    state = _make_rename_state(sessions)

    result = await _execute_core_command(state, "/rename", agent_id="coder", project_id=None)

    # No argument clears: the handler passes "" and reports the cleared name.
    assert sessions.renamed == [("coder", "s1", "", None)]
    assert result.facts["title"] is None
    assert result.feedback is not None


@pytest.mark.asyncio
async def test_rename_command_project_session_scopes_to_project() -> None:
    sessions = _RecordingTitleSessions()
    state = _make_rename_state(sessions)

    await _execute_core_command(state, "/rename Docs", project_id="vbot")

    assert sessions.renamed == [("builder", "s1", "Docs", "vbot")]


@pytest.mark.asyncio
async def test_set_model_project_reset_clears_override() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured=set(), agents=agents, projects=projects, models=SimpleNamespace()
    )

    # The reset token is case-insensitive.
    await _execute_core_command(state, "/model RESET", project_id="vbot")

    assert projects.clear_calls == [("vbot", "builder", "model")]
    assert projects.set_calls == []


@pytest.mark.asyncio
async def test_set_model_rejects_unusable_model() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured={"openai/gpt-5"}, agents=agents, projects=projects, models=SimpleNamespace()
    )

    with pytest.raises(ModelConfigurationError):
        await _execute_core_command(state, "/model openai/ghost", agent_id="coder")

    assert agents.updates == []  # nothing is written when the model is rejected


@pytest.mark.asyncio
async def test_set_model_rejects_forbidden_pinned_connection() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()

    class _Model:
        connections = ["api-key"]

        def allows_connection(self, connection_id: str) -> bool:
            return connection_id in self.connections

    models = SimpleNamespace(get=lambda _provider, _model: _Model())
    state = _make_model_state(
        configured={"openai/gpt-5::subscription"},
        agents=agents,
        projects=projects,
        models=models,
    )

    state.runtime.agent_resolver = _ModelResolver(set())
    with pytest.raises(ModelConfigurationError):
        await _execute_core_command(
            state, "/model openai/gpt-5::subscription", agent_id="coder", project_id=None
        )

    assert agents.updates == []
