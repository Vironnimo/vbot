"""Shared fixtures and fakes for commands behavior tests."""

from __future__ import annotations

import asyncio
from typing import Any

from core.agents.agents import Agent
from core.chat import (
    CommandDispatcher,
    CommandExecutionContext,
    CommandOutcome,
    PreparedCommand,
    ReplySurface,
)
from core.tools.availability import ToolAccess


def _prepared(dispatcher: CommandDispatcher, message: str) -> PreparedCommand:
    prepared = dispatcher.prepare(message)
    assert prepared is not None
    return prepared


async def _execute(
    dispatcher: CommandDispatcher,
    message: str,
    *,
    agent_id: str = "coder",
    session_id: str = "session-one",
    project_id: str | None = None,
) -> CommandOutcome:
    return await dispatcher.execute(
        _prepared(dispatcher, message),
        CommandExecutionContext(
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            reply_surface=ReplySurface.webui(),
        ),
    )


def _execute_sync(
    dispatcher: CommandDispatcher,
    message: str,
    *,
    agent_id: str = "coder",
    session_id: str = "session-one",
    project_id: str | None = None,
) -> CommandOutcome:
    return asyncio.run(
        _execute(
            dispatcher,
            message,
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
        )
    )


def _make_agent(
    *,
    model: str = "openai/gpt-5.2",
    fallback_models: list[str] | None = None,
    temperature: float | None = 0.3,
    thinking_effort: str | None = "none",
) -> Agent:
    return Agent(
        id="coder",
        name="Coder",
        model=model,
        fallback_models=(fallback_models if fallback_models is not None else ["openai/gpt-5.1"]),
        workspace="workspace",
        temperature=temperature,
        thinking_effort=thinking_effort,
        tool_access=ToolAccess(mode="all"),
        allowed_skills=["*"],
        tools={},
        created_at="2026-05-18T10:00:00+00:00",
        updated_at="2026-05-18T10:00:00+00:00",
    )


_UNSET: Any = object()


class _StubResolver:
    """Resolver stub returning a fixed agent, recording the resolve target.

    Mirrors the run-path seam ``/status`` now uses, so a test can assert the
    dispatcher threads the session's ``project_id`` through to the resolver. It
    also answers ``effective_config`` — the provenance seam ``/model`` reads — with
    a configurable model ``{value, source}`` so a test can drive each origin
    wording by choosing the winning tier.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        resolve_error: Exception | None = None,
        model_value: Any = _UNSET,
        model_source: Any = _UNSET,
        effective_error: Exception | None = None,
    ) -> None:
        self._agent = agent
        self._resolve_error = resolve_error
        # Default the effective model to the agent's own model / "agent" source so a
        # test that only cares about the value need not spell out the tier. A sentinel
        # distinguishes "not passed" from an explicit ``None`` (a fallen-through tier).
        self._model_value = agent.model if model_value is _UNSET else model_value
        self._model_source = "agent" if model_source is _UNSET else model_source
        self._effective_error = effective_error
        self.calls: list[tuple[str | None, str]] = []
        self.effective_calls: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Agent:
        self.calls.append((project_id, agent_id))
        if self._resolve_error is not None:
            raise self._resolve_error
        return self._agent

    def effective_config(self, project_id: str | None, agent_id: str) -> dict[str, dict[str, Any]]:
        self.effective_calls.append((project_id, agent_id))
        if self._effective_error is not None:
            raise self._effective_error
        return {"model": {"value": self._model_value, "source": self._model_source}}


class _StubProject:
    def __init__(self, project_id: str, display_name: str) -> None:
        self.project_id = project_id
        self.display_name = display_name


class _StubProjects:
    """Project store stub resolving a single project by id."""

    def __init__(self, project: _StubProject) -> None:
        self._project = project

    def get(self, project_id: str) -> _StubProject:
        if project_id != self._project.project_id:
            raise KeyError(project_id)
        return self._project

    def list(self) -> list[_StubProject]:
        return [self._project]
