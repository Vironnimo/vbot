"""Slash commands sent for a new Session: which answer without one, which create it, which wait.

A message for a new Session names no Session yet. ``execute_for_new_session``
runs a command as its spec's ``new_session_mode`` declares, and only a command
that creates the Session and uses it leaves a Session behind.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    CommandDispatcher,
    CommandFeedback,
    CommandOutcome,
    ExtensionCommandContext,
    NewSessionCommandContext,
    ReplySurface,
)
from core.chat.messages import ChatMessage
from core.projects import AgentOverrides, ModelConfigurationError
from core.runs import ChatRunManager
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.chat.commands_test_support import _make_agent, _prepared

UNUSABLE_MODEL = "openai/unusable"


class _Resolver:
    """Resolves ``coder`` with the overrides a new Session starts with; one Model is unusable."""

    def __init__(self) -> None:
        self.agent = _make_agent()

    async def resolve_agent_async(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        session_id: str | None = None,
        new_session_overrides: AgentOverrides | None = None,
    ) -> Any:
        overrides = new_session_overrides or AgentOverrides()
        if overrides.model == UNUSABLE_MODEL:
            raise ModelConfigurationError(
                f"model is not usable in this instance: {overrides.model}"
            )
        return replace(self.agent, **overrides.agent_changes())

    def effective_config(
        self, project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        return {"model": {"value": self.agent.model, "source": "agent"}}


@pytest.fixture
def sessions(tmp_path: Path, current_session_store_template: Path) -> Iterator[ChatSessionManager]:
    for name in ("data-store.json", "sessions.db"):
        shutil.copy2(current_session_store_template / name, tmp_path)
    manager = ChatSessionManager(tmp_path)
    yield manager
    manager.close()


@pytest.fixture
def dispatcher(sessions: ChatSessionManager) -> CommandDispatcher:
    return CommandDispatcher(
        ChatRunManager(persistence=sessions),
        agent_resolver=cast(Any, _Resolver()),
        sessions=sessions,
    )


def _context(**overrides: Any) -> NewSessionCommandContext:
    return NewSessionCommandContext(
        agent_id="coder",
        project_id=None,
        reply_surface=ReplySurface.webui(),
        agent_overrides=AgentOverrides(**overrides),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "overrides", "reply"),
    [
        pytest.param("/help", {}, "/status", id="help"),
        pytest.param("/status", {}, "openai/gpt-5.2", id="status"),
        # The Model the new Session would start with, an override included.
        pytest.param("/model", {"model": "openai/gpt-mini"}, "openai/gpt-mini", id="model"),
        pytest.param("/new", {}, "already a new session", id="new"),
    ],
)
async def test_session_less_commands_answer_without_creating_a_session(
    dispatcher: CommandDispatcher,
    sessions: ChatSessionManager,
    message: str,
    overrides: dict[str, Any],
    reply: str,
) -> None:
    result = await dispatcher.execute_for_new_session(
        _prepared(dispatcher, message), _context(**overrides)
    )

    assert result.session_id is None
    assert result.outcome.feedback is not None
    assert reply in result.outcome.feedback.text
    assert sessions.list("coder") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["/agent planner", "/compact", "/handoff", "/reflect", "/rename Release", "/stop"],
)
async def test_commands_that_work_on_a_session_wait_for_its_first_message(
    dispatcher: CommandDispatcher, sessions: ChatSessionManager, message: str
) -> None:
    prepared = _prepared(dispatcher, message)

    result = await dispatcher.execute_for_new_session(prepared, _context())

    assert result.session_id is None
    assert result.outcome.feedback == CommandFeedback(
        kind="notice",
        text=f"/{prepared.name} works on an existing session. "
        "Send a message first to start this one.",
    )
    assert sessions.list("coder") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("uses_session", [True, False], ids=["used", "unused"])
async def test_a_creating_command_keeps_its_session_only_when_it_used_it(
    dispatcher: CommandDispatcher, sessions: ChatSessionManager, uses_session: bool
) -> None:
    observed: list[str] = []

    def handler(context: ExtensionCommandContext, argument: str | None) -> CommandOutcome:
        observed.append(context.session_id)
        if uses_session:
            address = SessionAddress(context.project_id, context.agent_id, context.session_id)
            sessions.get(address).append(ChatMessage.user("Workflow brief"))
        return CommandOutcome(
            command="workflow", feedback=CommandFeedback(kind="notice", text="Started.")
        )

    dispatcher.register_extension_command(
        "fixture", name="workflow", description="Start the workflow.", handler=handler
    )

    result = await dispatcher.execute_for_new_session(
        _prepared(dispatcher, "/workflow"), _context(model="openai/gpt-mini")
    )

    # The handler ran in a Session created with the new Session's overrides.
    [session_id] = observed
    if uses_session:
        assert result.session_id == session_id
        address = SessionAddress(None, "coder", session_id)
        assert sessions.get_metadata(address)["agent_overrides"] == {"model": "openai/gpt-mini"}
        assert result.outcome.resource_changes[0].kind == "sessions"
        assert result.outcome.resource_changes[0].scope == {
            "agent_id": "coder",
            "session_id": session_id,
        }
    else:
        # A Session the command left without History is removed and not named.
        assert result.session_id is None
        assert sessions.list("coder") == []
        assert result.outcome.resource_changes == ()


@pytest.mark.asyncio
async def test_a_creating_command_with_an_unusable_model_creates_nothing(
    dispatcher: CommandDispatcher, sessions: ChatSessionManager
) -> None:
    with pytest.raises(ModelConfigurationError):
        await dispatcher.execute_for_new_session(
            _prepared(dispatcher, "/learn"), _context(model=UNUSABLE_MODEL)
        )

    assert sessions.list("coder") == []
