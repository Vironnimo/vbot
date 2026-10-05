"""Chat in a Sub-Agent Session: Parent framing, the user's takeover, and the Sub-Agent's Tools.

A Session with a Parent link frames input from the Parent Agent as not from the
user, records the user's first own message there as a takeover exactly once,
and offers ``message_parent``; an ordinary Session does none of that.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, override

import pytest

from core.chat._queued_input import STEERING_SYSTEM_REMINDER
from core.chat.messages import (
    PARENT_AGENT_INPUT_SYSTEM_REMINDER,
    PARENT_AGENT_STEERING_SYSTEM_REMINDER,
    SUBAGENT_TAKEN_OVER_SYSTEM_REMINDER,
    ChatMessage,
)
from core.runs import RunAdmission, RunKind
from core.sessions import (
    SUBAGENT_PARENT_META_KEY,
    SUBAGENT_SESSION_META_KEY,
    SUBAGENT_TAKEN_OVER_AT_META_KEY,
    SessionAddress,
)
from core.tools import ToolRegistry, tool_success
from core.tools.availability import MESSAGE_PARENT_TOOL_NAME, TOOL_ACTIVATION_SESSION_GRANT
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)

pytestmark = pytest.mark.asyncio

FRAMING = {
    PARENT_AGENT_INPUT_SYSTEM_REMINDER,
    PARENT_AGENT_STEERING_SYSTEM_REMINDER,
    SUBAGENT_TAKEN_OVER_SYSTEM_REMINDER,
    STEERING_SYSTEM_REMINDER,
}


class PausedAdapter(StubAdapter):
    """Hold the first request until released."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def send(
        self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        if not self.requests:
            self.entered.set()
            await self.release.wait()
        return await super().send(messages, model_id=model_id, **kwargs)


def _answers(count: int) -> list[Any]:
    return [{"content": f"Answer {index}", "tool_calls": None} for index in range(count)]


def _runtime(tmp_path: Path, adapter: StubAdapter) -> Any:
    tools = ToolRegistry()
    tools.register(
        MESSAGE_PARENT_TOOL_NAME,
        "Message the Parent Agent.",
        {"type": "object", "properties": {"content": {"type": "string"}}},
        lambda _context, _arguments: tool_success({"status": "sent"}),
        open_input_schema=True,
        catalog_visible=False,
        session_scoped=True,
        activation=TOOL_ACTIVATION_SESSION_GRANT,
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.takeovers = []
    runtime.subagent_taken_over = runtime.takeovers.append
    return runtime


def _create(runtime: Any, session_id: str, *, linked: bool) -> SessionAddress:
    runtime.chat_sessions.create("coder", session_id=session_id)
    address = session_address("coder", session_id)
    if linked:

        def link(metadata: dict[str, Any]) -> None:
            metadata[SUBAGENT_SESSION_META_KEY] = True
            metadata[SUBAGENT_PARENT_META_KEY] = {
                "id": "sub_aaaaaaaaaaaa",
                "agent_id": "lead",
                "session_id": "parent",
                "run_id": "parent-run",
                "tool_call_id": None,
                "tool_call_index": None,
                "project_id": None,
            }

        runtime.chat_sessions.mutate_metadata(address, link)
    return address


def _framing(history: list[ChatMessage]) -> list[tuple[str, list[str]]]:
    """Each User message with the framing reminders written directly before it."""
    framed: list[tuple[str, list[str]]] = []
    notes: list[str] = []
    for message in history:
        if message.role == "note" and message.content in FRAMING:
            notes.append(str(message.content))
        elif message.role == "user":
            framed.append((str(message.content), notes))
            notes = []
    return framed


async def _parent_turn(runtime: Any, loop: Any, address: SessionAddress, content: str) -> Any:
    return await runtime.chat_run_manager.start(
        address,
        loop.run_executor(content, parent_agent_input=True),
        admission=RunAdmission(run_kind=RunKind.SUBAGENT),
    )


async def test_parent_turns_are_framed_and_the_first_user_turn_takes_over_once(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, StubAdapter(_answers(4)))
    address = _create(runtime, "one", linked=True)
    loop = build_chat_loop(runtime)

    await (await _parent_turn(runtime, loop, address, "Task")).wait()
    assert runtime.chat_sessions.metadata_value(address, SUBAGENT_TAKEN_OVER_AT_META_KEY) is None
    await (await loop.start_run("coder", "Hi", session_id="one")).wait()
    await (await loop.start_run("coder", "Again", session_id="one")).wait()
    await (await _parent_turn(runtime, loop, address, "Late")).wait()

    history = runtime.chat_sessions.get(address).load()
    assert _framing(history) == [
        ("Task", [PARENT_AGENT_INPUT_SYSTEM_REMINDER]),
        ("Hi", [SUBAGENT_TAKEN_OVER_SYSTEM_REMINDER]),
        ("Again", []),
        ("Late", [PARENT_AGENT_INPUT_SYSTEM_REMINDER]),
    ]
    assert runtime.chat_sessions.metadata_value(address, SUBAGENT_TAKEN_OVER_AT_META_KEY)
    assert runtime.takeovers == [address]


async def test_steered_input_is_framed_and_a_steered_user_message_takes_over(
    tmp_path: Path,
) -> None:
    adapter = PausedAdapter(_answers(2))
    runtime = _runtime(tmp_path, adapter)
    address = _create(runtime, "one", linked=True)
    loop = build_chat_loop(runtime)
    manager = runtime.chat_run_manager
    run = await _parent_turn(runtime, loop, address, "Task")
    await asyncio.wait_for(adapter.entered.wait(), 5)

    parent = await manager.enqueue(
        address,
        loop.run_executor("More", parent_agent_input=True),
        display_content="More",
        steerable=True,
        admission=RunAdmission(run_kind=RunKind.SUBAGENT),
    )
    user = await loop.queue_run("coder", "Hi", session_id="one")
    for item in (parent, user):
        manager.steer_queued("coder", "one", item.item_id, project_id=None)
    adapter.release.set()
    await asyncio.wait_for(run.wait(), 10)

    history = runtime.chat_sessions.get(address).load()
    assert _framing(history) == [
        ("Task", [PARENT_AGENT_INPUT_SYSTEM_REMINDER]),
        ("More", [PARENT_AGENT_STEERING_SYSTEM_REMINDER]),
        ("Hi", [SUBAGENT_TAKEN_OVER_SYSTEM_REMINDER, STEERING_SYSTEM_REMINDER]),
    ]
    assert runtime.takeovers == [address]


async def test_message_parent_is_offered_only_in_a_subagent_session(tmp_path: Path) -> None:
    adapter = StubAdapter(_answers(2))
    runtime = _runtime(tmp_path, adapter)
    _create(runtime, "linked", linked=True)
    plain = _create(runtime, "plain", linked=False)
    loop = build_chat_loop(runtime)

    await (await loop.start_run("coder", "Hi", session_id="linked")).wait()
    await (await loop.start_run("coder", "Hi", session_id="plain")).wait()

    offered = [
        {tool["name"] for tool in request["kwargs"]["tools"]} for request in adapter.requests
    ]
    assert MESSAGE_PARENT_TOOL_NAME in offered[0]
    assert MESSAGE_PARENT_TOOL_NAME not in offered[1]
    # A user message in an ordinary Session is no takeover.
    assert runtime.chat_sessions.metadata_value(plain, SUBAGENT_TAKEN_OVER_AT_META_KEY) is None
    assert len(runtime.takeovers) == 1
