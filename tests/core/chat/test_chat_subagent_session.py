"""Chat in a Sub-Agent Session: Parent framing, reply routing, and the Sub-Agent's Tools.

A Session with a Parent link frames input from the Parent Agent as not from the
user, selects the recipient for final answers from each external input,
and offers ``message_parent``; an ordinary Session does none of that. A
Sub-Agent at the nesting limit is not offered ``subagent``.
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
    SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER,
    ChatMessage,
)
from core.runs import RunAdmission, RunCancelledError, RunKind
from core.sessions import (
    SUBAGENT_REPLY_TARGET_META_KEY,
    ChatSession,
    SessionAddress,
)
from core.subagents import SubAgentCoordinator
from core.subagents.links import link_session
from core.tools import ToolContext, ToolRegistry, tool_success
from core.tools.availability import MESSAGE_PARENT_TOOL_NAME, TOOL_ACTIVATION_SESSION_GRANT
from core.tools.model_names import SUBAGENT_TOOL_NAME
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
    SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER,
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
    calls: list[ToolContext] = []

    def message_parent(context: ToolContext, _arguments: Any) -> Any:
        calls.append(context)
        return tool_success({"status": "sent"})

    tools.register(
        MESSAGE_PARENT_TOOL_NAME,
        "Message the Parent Agent.",
        {"type": "object", "properties": {"content": {"type": "string"}}},
        message_parent,
        open_input_schema=True,
        catalog_visible=False,
        session_scoped=True,
        activation=TOOL_ACTIVATION_SESSION_GRANT,
    )
    tools.register(
        SUBAGENT_TOOL_NAME,
        "Delegate work to a Sub-Agent.",
        {"type": "object", "properties": {"content": {"type": "string"}}},
        lambda _context, _arguments: tool_success({}),
        open_input_schema=True,
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[SUBAGENT_TOOL_NAME])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.user_reply_switches = []
    runtime.tool_calls = calls
    runtime.subagent_reply_to_user = lambda address, _message_id: (
        runtime.user_reply_switches.append(address)
    )
    runtime.subagent_tool_usable = SubAgentCoordinator(runtime, None).subagent_tool_usable
    return runtime


def _create(
    runtime: Any, session_id: str, *, linked: bool, parent: SessionAddress | None = None
) -> SessionAddress:
    """Create a Session, linked to *parent* (else a Parent Session that is gone)."""
    runtime.chat_sessions.create("coder", session_id=session_id)
    address = session_address("coder", session_id)
    if linked:
        link_session(
            runtime.chat_sessions,
            address,
            subagent_id=f"sub_{session_id}",
            parent=parent or session_address("lead", "parent"),
            run_id="parent-run",
            tool_call_id=None,
            tool_call_index=None,
        )
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


async def test_parent_and_user_input_switch_the_answer_destination(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, StubAdapter(_answers(5)))
    address = _create(runtime, "one", linked=True)
    loop = build_chat_loop(runtime)

    await (await _parent_turn(runtime, loop, address, "Task")).wait()
    assert runtime.chat_sessions.metadata_value(address, SUBAGENT_REPLY_TARGET_META_KEY) == "parent"
    await (await loop.start_run("coder", "Hi", session_id="one")).wait()
    await (await loop.start_run("coder", "Again", session_id="one")).wait()
    await (await _parent_turn(runtime, loop, address, "Late")).wait()
    await (await loop.start_run("coder", "Back", session_id="one")).wait()

    history = runtime.chat_sessions.get(address).load()
    assert _framing(history) == [
        ("Task", [PARENT_AGENT_INPUT_SYSTEM_REMINDER]),
        ("Hi", [SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER]),
        ("Again", []),
        ("Late", [PARENT_AGENT_INPUT_SYSTEM_REMINDER]),
        ("Back", [SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER]),
    ]
    assert runtime.chat_sessions.metadata_value(address, SUBAGENT_REPLY_TARGET_META_KEY)
    assert runtime.user_reply_switches == [address, address]
    assert [m.subagent_reply_target for m in history if m.role == "assistant"] == [
        "parent",
        "user",
        "user",
        "parent",
        "user",
    ]


async def test_steered_input_is_framed_and_switches_the_answer_destination(
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
        ("Hi", [SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER, STEERING_SYSTEM_REMINDER]),
    ]
    assert runtime.user_reply_switches == [address]


async def test_a_run_cancelled_as_its_user_input_commits_keeps_the_answer_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, StubAdapter(_answers(1)))
    address = _create(runtime, "one", linked=True)
    loop = build_chat_loop(runtime)
    append = ChatSession.append_many_async
    cancelled: list[bool] = []

    async def cancelled_after_commit(
        self: ChatSession, messages: list[ChatMessage], **options: Any
    ) -> Any:
        committed = await append(self, messages, **options)
        if not cancelled and any(message.role == "user" for message in messages):
            cancelled.append(True)
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
        return committed

    monkeypatch.setattr(ChatSession, "append_many_async", cancelled_after_commit)
    with pytest.raises(RunCancelledError):
        await (await loop.start_run("coder", "Hi", session_id="one")).wait()
    monkeypatch.undo()

    assert cancelled == [True]
    assert runtime.chat_sessions.metadata_value(address, SUBAGENT_REPLY_TARGET_META_KEY)
    await (await loop.start_run("coder", "Again", session_id="one")).wait()
    history = runtime.chat_sessions.get(address).load()
    assert _framing(history) == [
        ("Hi", [SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER]),
        ("Again", []),
    ]


@pytest.mark.parametrize(
    "max_depth", [2, 1], ids=["below-the-nesting-limit", "at-the-limit-with-own-subagents"]
)
async def test_the_session_decides_which_subagent_tools_are_offered(
    tmp_path: Path, max_depth: int
) -> None:
    call = {"id": "call_parent", "name": MESSAGE_PARENT_TOOL_NAME, "arguments": {"content": "x"}}
    adapter = StubAdapter([{"content": None, "tool_calls": [call]}, *_answers(3)])
    runtime = _runtime(tmp_path, adapter)
    runtime.storage.subagent_settings = {"max_subagent_depth": max_depth}
    linked = _create(runtime, "linked", linked=True)
    nested = _create(runtime, "nested", linked=True, parent=linked)
    plain = _create(runtime, "plain", linked=False)
    loop = build_chat_loop(runtime)

    for session_id in ("linked", "nested", "plain"):
        await (await loop.start_run("coder", "Hi", session_id=session_id)).wait()

    linked_tools, _, nested_tools, plain_tools = [
        {tool["name"] for tool in request["kwargs"]["tools"]} for request in adapter.requests
    ]
    assert all(
        "subagent_reply_target" not in message
        for request in adapter.requests
        for message in request["messages"]
    )
    # message_parent belongs to Sub-Agent Sessions. "nested" has as many Parents
    # as the limit allows and no Sub-Agent of its own, so subagent could only
    # start one there, which is refused. "linked" can start one below the limit,
    # and at a lowered limit still message the Sub-Agent it has.
    assert linked_tools == {MESSAGE_PARENT_TOOL_NAME, SUBAGENT_TOOL_NAME}
    assert nested_tools == {MESSAGE_PARENT_TOOL_NAME}
    assert plain_tools == {SUBAGENT_TOOL_NAME}
    # Its Tool calls know the Session is a Sub-Agent Session (for Secrets).
    assert [context.subagent_session for context in runtime.tool_calls] == [True]
    # A user message in an ordinary Session does not change Sub-Agent routing.
    assert runtime.chat_sessions.metadata_value(plain, SUBAGENT_REPLY_TARGET_META_KEY) is None
    assert runtime.user_reply_switches == [linked, nested]
