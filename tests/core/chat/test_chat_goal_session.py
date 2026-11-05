"""Chat in a Session with a goal: the ``update_goal`` grant and the retained goal note."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, override

import pytest

from core.automation.goals import GoalService
from core.chat import ChatMessage, ReplySurface
from core.chat._message_history import effective_compaction_messages
from core.chat.commands import CommandExecutionContext
from core.chat.messages import GoalEvent
from core.runs import GOAL_EVENT_RECORDED_EVENT, RunKind
from core.sessions import SESSION_GOAL_META_KEY
from core.tools import ToolAccess, ToolRegistry, tool_success
from core.tools.availability import TOOL_ACTIVATION_SESSION_GRANT, UPDATE_GOAL_TOOL_NAME
from core.tools.create_goal import register_create_goal_tool
from core.tools.update_goal import register_update_goal_tool
from tests.core.chat.chat_loop_compaction_test_support import auto_compact, compaction_runtime
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubRuntime,
    build_chat_loop,
    session_address,
)

pytestmark = pytest.mark.asyncio

_GOAL_NOTE = 'The user set a goal for this session with /goal:\n"Ship it"\nWhen your turn ends ...'


def _tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        UPDATE_GOAL_TOOL_NAME,
        "Mark the session goal complete, blocked or paused.",
        {"type": "object", "properties": {"status": {"type": "string"}}},
        lambda _context, _arguments: tool_success({"content": "ok"}),
        open_input_schema=True,
        catalog_visible=False,
        session_scoped=True,
        activation=TOOL_ACTIVATION_SESSION_GRANT,
    )
    return tools


def _set_goal(runtime: Any, session_id: str, status: str | None) -> None:
    if status is None:
        return
    runtime.chat_sessions.mutate_metadata(
        session_address("coder", session_id),
        lambda metadata: metadata.update(
            {SESSION_GOAL_META_KEY: {"objective": "Ship it", "status": status}}
        ),
    )


@pytest.mark.parametrize(
    ("status", "offered"),
    [("active", True), ("blocked", True), ("paused", True), ("complete", False), (None, False)],
)
@pytest.mark.parametrize("creation_enabled", [False, True])
async def test_update_goal_is_offered_while_the_session_goal_is_open(
    tmp_path: Path, status: str | None, offered: bool, creation_enabled: bool
) -> None:
    adapter = StubAdapter([{"content": "Answer", "tool_calls": None}])
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["create_goal"] if creation_enabled else [],
    )
    tools = _tools()
    tools.register("create_goal", "Start goal", {"type": "object"}, lambda _c, _a: tool_success({}))
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="goal")
    _set_goal(runtime, "goal", status)
    loop = build_chat_loop(runtime)

    await (await loop.start_run("coder", "Hi", session_id="goal")).wait()

    [request] = adapter.requests
    offered_tools = {tool["name"] for tool in request["kwargs"].get("tools") or []}
    assert (UPDATE_GOAL_TOOL_NAME in offered_tools) is offered
    assert ("create_goal" in offered_tools) is creation_enabled


@pytest.mark.parametrize(
    ("agent_starts", "denied"),
    [(False, False), (False, True), (True, False)],
    ids=["user-starts-tool-unselected", "user-starts-tool-denied", "agent-starts"],
)
async def test_a_started_goal_grants_update_goal_inside_the_current_run(
    tmp_path: Path, agent_starts: bool, denied: bool
) -> None:
    class CreationDeniedAgent(StubAgent):
        @property
        @override
        def tool_access(self) -> ToolAccess:
            return ToolAccess(mode="all", denied=("create_goal",))

    responses: list[dict[str, Any]] = []
    if agent_starts:
        responses.append(
            {
                "tool_calls": [
                    {"id": "create", "name": "create_goal", "arguments": {"objective": "Ship <it>"}}
                ]
            }
        )
    responses.extend(
        [
            {
                "tool_calls": [
                    {"id": "complete", "name": "update_goal", "arguments": {"status": "complete"}}
                ]
            },
            {"content": "Finished", "tool_calls": None},
        ]
    )
    adapter = StubAdapter(responses)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=(CreationDeniedAgent if denied else StubAgent)(
            id="coder",
            model="openai/gpt-5.2",
            allowed_tools=["create_goal"] if agent_starts else [],
        ),
        adapter=adapter,
        tools=ToolRegistry(),
    )
    session = runtime.chat_sessions.create("coder", session_id="goal")
    loop = build_chat_loop(runtime)
    goals = GoalService(
        sessions=runtime.chat_sessions, run_manager=runtime.chat_run_manager, chat_loop=loop
    )
    register_create_goal_tool(runtime.tools, goals)
    register_update_goal_tool(runtime.tools, goals)
    changes: list[tuple[Any, bool]] = []
    goals.add_changed_callback(lambda address, history: changes.append((address, history)))
    try:
        if agent_starts:
            run = await loop.start_run("coder", "Ship <it>", session_id="goal")
            await run.wait()
        else:
            outcome = await goals.execute_command(
                CommandExecutionContext(
                    agent_id="coder",
                    session_id="goal",
                    project_id=None,
                    reply_surface=ReplySurface.webui(),
                    command_text="/goal Ship <it>",
                ),
                "Ship <it>",
            )
            run = outcome.runs[0].run
            await run.wait()
        goal = await goals.goal(session.address)
        assert goal is not None and goal["status"] == "complete"
        first = {t["name"] for t in adapter.requests[0]["kwargs"]["tools"]}
        assert ("create_goal" in first) is agent_starts
        assert ("update_goal" in first) is not agent_starts
        if agent_starts:
            # New Tools are announced in history while the epoch's Tool list stays pinned.
            assert "blocked_reason" in str(adapter.requests[1]["messages"])
        notes = [m for m in session.load() if m.goal_event and m.goal_event.event == "set"]
        assert len(notes) == 1 and '"Ship \\u003cit\\u003e"' in str(notes[0].content)
        events = await runtime.timelines.events(run)
        recorded = [event for event in events if event.type == GOAL_EVENT_RECORDED_EVENT]
        if agent_starts:
            assert changes[0] == (session.address, True)
            assert notes[0].run_id is None
        else:
            assert [event.payload["id"] for event in recorded] == [notes[0].id]
    finally:
        await goals.aclose()


@pytest.mark.parametrize("human_steering", [False, True])
async def test_goal_creation_uses_delivered_user_input_instead_of_run_origin(
    tmp_path: Path, human_steering: bool
) -> None:
    class SteeringAdapter(StubAdapter):
        @override
        async def send(
            self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
        ) -> dict[str, Any]:
            if human_steering and not self.requests:
                item = await loop.queue_run("coder", "Finish the report", session_id="goal")
                runtime.chat_run_manager.steer_queued(
                    "coder", "goal", item.item_id, project_id=None
                )
            return await super().send(messages, model_id=model_id, **kwargs)

    responses: list[dict[str, Any]] = [
        {"tool_calls": [{"id": "probe", "name": "probe", "arguments": {}}]},
        {
            "tool_calls": [
                {
                    "id": "create",
                    "name": "create_goal",
                    "arguments": {"objective": "Finish the report"},
                }
            ]
        },
    ]
    if human_steering:
        responses.append(
            {
                "tool_calls": [
                    {"id": "finish", "name": "update_goal", "arguments": {"status": "complete"}}
                ]
            }
        )
    responses.append({"content": "Finished"})
    adapter = SteeringAdapter(responses)
    tools = ToolRegistry()
    tools.register("probe", "Probe", {"type": "object"}, lambda _c, _a: tool_success({}))
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["create_goal", "probe"]),
        adapter=adapter,
        tools=tools,
    )
    session = runtime.chat_sessions.create("coder", session_id="goal")
    loop = build_chat_loop(runtime)
    goals = GoalService(
        sessions=runtime.chat_sessions, run_manager=runtime.chat_run_manager, chat_loop=loop
    )
    register_create_goal_tool(tools, goals)
    register_update_goal_tool(tools, goals)
    try:
        run = await loop.start_run(
            "coder", "Background work", session_id="goal", internal=True, run_kind=RunKind.SYSTEM
        )
        await run.wait()
        assert run.run_kind == RunKind.SYSTEM and run.internal
        result = next(
            json.loads(str(m.content))
            for m in session.load()
            if m.role == "tool" and m.name == "create_goal"
        )
        assert result["ok"] is human_steering
        if human_steering:
            goal = await goals.goal(session.address)
            assert goal is not None and goal["status"] == "complete"
        else:
            assert result["error"]["code"] == "goal_creation_not_allowed"
            assert await goals.goal(session.address) is None
    finally:
        await goals.aclose()


@pytest.mark.parametrize(("status", "retained"), [("active", True), ("complete", False)])
@pytest.mark.parametrize("typed_note", [False, True], ids=["existing-prefix", "goal-event"])
async def test_compaction_keeps_the_note_of_an_open_goal(
    tmp_path: Path, status: str, retained: bool, typed_note: bool
) -> None:
    runtime = compaction_runtime(tmp_path, tools=_tools())
    session = runtime.chat_sessions.create("coder", session_id="goal")
    _set_goal(runtime, "goal", status)
    note = ChatMessage.note(
        'Session goal: "Ship it"' if typed_note else _GOAL_NOTE,
        goal_event=GoalEvent("set", objective="Ship it", max_turns=30) if typed_note else None,
    )
    session.append_many(
        [
            note,
            ChatMessage.user("Work"),
            ChatMessage.assistant(model="openai/gpt-5.2", content="Done"),
        ]
    )
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Summary", projection=[], compacted_token_count=10
    )
    loop = build_chat_loop(
        runtime, compaction_service=StubCompactionService(should_auto=True, checkpoint=checkpoint)
    )

    await auto_compact(loop, runtime.agents.get("coder"), session, usage={"input_tokens": 90})

    effective = effective_compaction_messages(session.load_active())
    assert any(message.id == note.id for message in effective) is retained
