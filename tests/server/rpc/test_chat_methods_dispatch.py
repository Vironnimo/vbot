"""Tests for chat RPC dispatch: history pagination, commands, and queue handling."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.chat import (
    ChatMessage,
    ChatSessionManager,
    CommandDispatcher,
    CommandExecutionContext,
    CommandFeedback,
    CommandOutcome,
    CommandResourceChange,
    ExtensionCommandContext,
    PreparedCommand,
    ReplySurface,
)
from core.chat.content_blocks import TextBlock
from core.chat.continuation import CONTINUATION_RECORD_VERSION
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    QueuedRunItem,
    Run,
)
from core.tools.tools import tool_success
from server.events import ServerEventBus
from server.rpc import chat_methods, event_bridge
from server.rpc.methods import dispatch_rpc


class HistoryAgentStore:
    def get(self, _agent_id: str) -> SimpleNamespace:
        return SimpleNamespace(current_session_id="session-one")


def _history_state(tmp_path: Path) -> tuple[SimpleNamespace, ChatSessionManager]:
    chat_sessions = ChatSessionManager(tmp_path)
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            agents=HistoryAgentStore(),
            chat_sessions=chat_sessions,
        ),
        chat_runs=ChatRunManager(),
    )
    return state, chat_sessions


def _history_message(index: int) -> ChatMessage:
    message = ChatMessage.user(f"Message {index}")
    return replace(message, id=f"message-{index:03d}")


class CommandOutcomeDispatcher(CommandDispatcher):
    def __init__(self, reply: str) -> None:
        super().__init__(ChatRunManager())
        self._reply = reply
        self.calls: list[tuple[str, str, str]] = []

    async def execute(
        self, prepared: PreparedCommand, context: CommandExecutionContext
    ) -> CommandOutcome:
        self.calls.append((context.agent_id, context.session_id, f"/{prepared.name}"))
        return CommandOutcome(
            command=prepared.name,
            feedback=CommandFeedback(kind="notice", text=self._reply),
        )


class QueueManagerStub:
    def __init__(
        self,
        *,
        items: list[QueuedRunItem] | None = None,
        remove_result: bool = True,
        update_result: bool = True,
    ) -> None:
        self._items = list(items or [])
        self._remove_result = remove_result
        self._update_result = update_result
        self.list_calls: list[tuple[str, str, str | None]] = []
        self.remove_calls: list[tuple[str, str, str, str | None]] = []
        self.update_calls: list[tuple[str, str, str, Any, str, str | None, bool | None]] = []

    def list_queued(
        self, agent_id: str, session_id: str, *, project_id: str | None
    ) -> list[QueuedRunItem]:
        self.list_calls.append((agent_id, session_id, project_id))
        return list(self._items)

    def remove_queued(
        self, agent_id: str, session_id: str, item_id: str, *, project_id: str | None
    ) -> bool:
        self.remove_calls.append((agent_id, session_id, item_id, project_id))
        return self._remove_result

    def update_queued(
        self,
        agent_id: str,
        session_id: str,
        item_id: str,
        new_executor: Any,
        new_display_content: str,
        *,
        project_id: str | None,
        editable: bool | None = None,
    ) -> bool:
        self.update_calls.append(
            (
                agent_id,
                session_id,
                item_id,
                new_executor,
                new_display_content,
                project_id,
                editable,
            )
        )
        return self._update_result


def test_transport_layers_do_not_own_command_workflows() -> None:
    root = Path(__file__).parents[3]
    chat_source = (root / "server" / "rpc" / "chat_methods.py").read_text(encoding="utf-8")
    channel_source = (root / "core" / "channels" / "engine.py").read_text(encoding="utf-8")
    combined = f"{chat_source}\n{channel_source}"

    for forbidden in (
        "Command" + "Action",
        "Command" + "Handled",
        "Dispatch" + "Result",
        "_handle_command_" + "action",
        "unsupported command " + "action",
    ):
        assert forbidden not in combined
    for server_owned_workflow in (
        "HANDOFF_FRAGMENT_NAME",
        "LEARN_FRAGMENT_NAME",
        "AGENT_TAKEOVER_NOTE",
        "_build_handoff_prompt",
        "_build_learn_prompt",
        "_session_move_block_reason",
    ):
        assert server_owned_workflow not in chat_source
    for command in (
        "compact",
        "handoff",
        "learn",
        "reflect",
        "new",
        "rename",
        "model",
    ):
        assert f'case "{command}"' not in combined


def _make_queued_item(
    *, item_id: str, content: str, internal: bool = False, editable: bool = True
) -> QueuedRunItem:
    async def _executor(_run: Run) -> None:
        return None

    return QueuedRunItem(
        item_id=item_id,
        display_content=content,
        executor=_executor,
        internal=internal,
        future=asyncio.get_running_loop().create_future(),
        editable=editable,
        created_at="2026-05-22T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_chat_history_hides_subagent_batch_completion_note(tmp_path: Path) -> None:
    # Arrange
    chat_sessions = ChatSessionManager(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    session.add_note("Sub-agent batch completed.\n\nResults:\n- worker/sub-session: Done")
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Continuing"))
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            agents=HistoryAgentStore(),
            chat_sessions=chat_sessions,
        ),
        chat_runs=ChatRunManager(),
    )

    # Act
    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent"}},
    )

    # Assert
    assert response["ok"] is True
    assert [message["role"] for message in response["result"]["messages"]] == ["assistant"]


@pytest.mark.asyncio
async def test_chat_history_hides_internal_continuation_checkpoint(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    session.append_continuation_records(
        [
            {
                "version": CONTINUATION_RECORD_VERSION,
                "type": "run_started",
                "run_id": "run-one",
                "timestamp": "2026-07-11T12:00:00+00:00",
                "checkpoint_id": "checkpoint-one",
                "origin_run_id": "run-one",
                "request": "work",
            },
            {
                "version": CONTINUATION_RECORD_VERSION,
                "type": "run_interrupted",
                "run_id": "run-one",
                "timestamp": "2026-07-11T12:00:01+00:00",
                "cause": "network",
            },
        ]
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "parent", "session_id": "session-one"},
        },
    )

    assert response["ok"] is True
    assert "continuation" not in response["result"]


@pytest.mark.asyncio
async def test_chat_history_projects_durable_background_bash_statuses(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    session.append(
        ChatMessage.tool(
            tool_call_id="bash-one",
            name="bash",
            content=json.dumps(
                tool_success(
                    {
                        "process_id": "process-one",
                        "status": "running",
                        "delivery": "automatic",
                    }
                )
            ),
        )
    )
    session.add_note(
        "Automatic completion delivery\n\n"
        "### Bash process — failed\n"
        "Process ID: process-one\n"
        "Command: npm test"
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "parent", "session_id": "session-one"},
        },
    )

    assert response["ok"] is True
    assert response["result"]["background_bash_statuses"] == {"process-one": "failed"}
    assert all(message["role"] != "note" for message in response["result"]["messages"])


@pytest.mark.asyncio
async def test_chat_history_limit_returns_newest_visible_messages(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    for index in range(1, 6):
        session.append(_history_message(index))

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent", "limit": 2}},
    )

    assert response["ok"] is True
    result = response["result"]
    assert [message["id"] for message in result["messages"]] == [
        "message-004",
        "message-005",
    ]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_chat_history_expands_limit_to_complete_oldest_run_segment(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    timing = {
        "started_at": "2026-07-24T10:00:00+00:00",
        "completed_at": "2026-07-24T10:00:01+00:00",
        "duration_ms": 1000,
    }
    messages = [
        replace(ChatMessage.user("first"), id="first-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="first result"),
            id="first-assistant",
        ),
        replace(
            ChatMessage.run_summary(
                run_id="run-one",
                status="completed",
                timing=timing,
                iteration_count=1,
            ),
            id="first-summary",
        ),
        replace(ChatMessage.user("second"), id="second-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="second result"),
            id="second-assistant",
        ),
        replace(
            ChatMessage.run_summary(
                run_id="run-two",
                status="completed",
                timing=timing,
                iteration_count=1,
            ),
            id="second-summary",
        ),
    ]
    for message in messages:
        session.append(message)

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent", "limit": 2}},
    )

    assert response["ok"] is True
    result = response["result"]
    assert [message["id"] for message in result["messages"]] == [
        "second-user",
        "second-assistant",
        "second-summary",
    ]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_chat_history_keeps_the_active_tail_segment_together(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    timing = {
        "started_at": "2026-07-24T10:00:00+00:00",
        "completed_at": "2026-07-24T10:00:01+00:00",
        "duration_ms": 1000,
    }
    messages = [
        replace(ChatMessage.user("completed"), id="completed-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="done"),
            id="completed-assistant",
        ),
        replace(
            ChatMessage.run_summary(
                run_id="run-one",
                status="completed",
                timing=timing,
                iteration_count=1,
            ),
            id="completed-summary",
        ),
        replace(ChatMessage.user("active"), id="active-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="partial"),
            id="active-assistant",
        ),
    ]
    for message in messages:
        session.append(message)

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent", "limit": 1}},
    )

    assert response["ok"] is True
    result = response["result"]
    assert [message["id"] for message in result["messages"]] == [
        "active-user",
        "active-assistant",
    ]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_subagent_inspect_dispatches_exact_qualified_work_address() -> None:
    class InspectStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, str | None]] = []

        def inspect(
            self,
            agent_id: str,
            session_id: str,
            work_id: str,
            *,
            project_id: str | None = None,
        ) -> dict[str, Any]:
            self.calls.append((agent_id, session_id, work_id, project_id))
            return {
                "id": work_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "run_id": "child-run",
                "status": "completed",
                "result": "done",
            }

    subagents = InspectStub()
    state = SimpleNamespace(runtime=SimpleNamespace(subagents=subagents))

    response = await dispatch_rpc(
        state,
        {
            "method": "subagent.inspect",
            "params": {
                "id": "sub-work-one",
                "agent_id": "worker@project-one",
                "session_id": "child-session",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["result"] == "done"
    assert subagents.calls == [("worker", "child-session", "sub-work-one", "project-one")]


@pytest.mark.asyncio
async def test_chat_history_before_returns_older_visible_page(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    for index in range(1, 7):
        session.append(_history_message(index))

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {
                "agent_id": "parent",
                "limit": 2,
                "before": "message-005",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert [message["id"] for message in result["messages"]] == [
        "message-003",
        "message-004",
    ]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_chat_history_rejects_unknown_before_message(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    session.append(_history_message(1))

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "parent", "before": "message-missing"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_chat_history_rejects_limit_above_maximum(tmp_path: Path) -> None:
    state, _chat_sessions = _history_state(tmp_path)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "parent", "limit": chat_methods.MAX_CHAT_HISTORY_LIMIT + 1},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_chat_commands_returns_combined_command_and_skill_items() -> None:
    skills = [
        SimpleNamespace(name="debugging", description="Debug failures."),
        SimpleNamespace(name="alpha", description="Alpha helper."),
        SimpleNamespace(name="workflow", description="Workflow Skill."),
    ]
    command_dispatcher = CommandDispatcher(ChatRunManager())
    command_dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Start the workflow.",
        handler=lambda _context, _argument: CommandOutcome(command="workflow"),
    )
    state = SimpleNamespace(
        command_dispatcher=command_dispatcher,
        runtime=SimpleNamespace(
            skills=SimpleNamespace(
                list_all=lambda: skills,
            )
        ),
    )

    response = await dispatch_rpc(state, {"method": "chat.commands", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "items": [
                {
                    "name": "agent",
                    "description": (
                        "Move this session to another agent; no argument lists the directory."
                    ),
                    "type": "command",
                    "argument": "optional",
                    "output": "action",
                },
                {
                    "name": "compact",
                    "description": "Compact the current session's context immediately.",
                    "type": "command",
                    "argument": "optional",
                    "output": "toast",
                },
                {
                    "name": "handoff",
                    "description": (
                        "Write a handoff and start a new session (optionally for another agent)."
                    ),
                    "type": "command",
                    "argument": "optional",
                    "output": "action",
                },
                {
                    "name": "help",
                    "description": "Show available slash commands.",
                    "type": "command",
                    "argument": "none",
                    "output": "transient",
                },
                {
                    "name": "learn",
                    "description": (
                        "Author a reusable skill into your own home from a source "
                        "(folder, URL, or text)."
                    ),
                    "type": "command",
                    "argument": "optional",
                    "output": "action",
                },
                {
                    "name": "model",
                    "description": (
                        "Show, set, or reset this session's model (/model reset to clear)."
                    ),
                    "type": "command",
                    "argument": "optional",
                    "output": "action",
                },
                {
                    "name": "new",
                    "description": "Start a new session for the current agent.",
                    "type": "command",
                    "argument": "none",
                    "output": "action",
                },
                {
                    "name": "reflect",
                    "description": (
                        "Review this session in a fork and save durable memory and skill updates."
                    ),
                    "type": "command",
                    "argument": "optional",
                    "output": "action",
                },
                {
                    "name": "rename",
                    "description": "Rename this session; no argument clears the name.",
                    "type": "command",
                    "argument": "optional",
                    "output": "toast",
                },
                {
                    "name": "status",
                    "description": "Show current session and runtime status.",
                    "type": "command",
                    "argument": "none",
                    "output": "transient",
                },
                {
                    "name": "stop",
                    "description": "Cancel the active run for this session.",
                    "type": "command",
                    "argument": "none",
                    "output": "toast",
                },
                {
                    "name": "workflow",
                    "description": "Start the workflow.",
                    "type": "command",
                    "argument": "optional",
                    "output": "toast",
                },
                {
                    "name": "alpha",
                    "description": "Alpha helper.",
                    "type": "skill",
                },
                {
                    "name": "debugging",
                    "description": "Debug failures.",
                    "type": "skill",
                },
                {
                    "name": "workflow",
                    "description": "Workflow Skill.",
                    "type": "skill",
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_chat_stream_slash_command_returns_handled_result_without_starting_run() -> None:
    streaming_chat_loop = SimpleNamespace(start_run=AsyncMock())
    command_dispatcher = CommandOutcomeDispatcher(reply="Run cancelled.")
    state = SimpleNamespace(
        command_dispatcher=command_dispatcher,
        streaming_chat_loop=streaming_chat_loop,
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "/stop",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["command_handled"] is True
    assert response["result"]["output"] == "toast"
    assert response["result"]["reply"]
    assert command_dispatcher.calls == [("agent-1", "session-1", "/stop")]
    streaming_chat_loop.start_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_stream_projects_extension_command_through_generic_path() -> None:
    streaming_chat_loop = SimpleNamespace(start_run=AsyncMock())
    command_dispatcher = CommandDispatcher(ChatRunManager())
    observed: list[tuple[str, str | None, str]] = []

    def handler(
        context: ExtensionCommandContext,
        argument: str | None,
    ) -> CommandOutcome:
        observed.append((context.session_id, argument, context.reply_surface.kind))
        return CommandOutcome(
            command="workflow",
            feedback=CommandFeedback(kind="detail", text="Workflow ready."),
            resource_changes=(CommandResourceChange(kind="commands"),),
        )

    command_dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Start the workflow.",
        handler=handler,
    )
    state = SimpleNamespace(
        command_dispatcher=command_dispatcher,
        streaming_chat_loop=streaming_chat_loop,
        event_bus=ServerEventBus(),
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "/workflow review",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "command_handled": True,
            "reply": "Workflow ready.",
            "output": "transient",
        },
    }
    assert observed == [("session-1", "review", "webui")]
    assert state.event_bus.events[-1]["payload"] == {"kind": "commands"}
    streaming_chat_loop.start_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_stream_returns_queued_response_when_session_is_busy() -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    streaming_chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("session already has an active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    state = SimpleNamespace(
        streaming_chat_loop=streaming_chat_loop,
        command_dispatcher=CommandDispatcher(ChatRunManager()),
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "Queued message",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "queued": True,
            "item": queued_item.to_dict(),
        },
    }
    streaming_chat_loop.start_run.assert_awaited_once_with(
        "agent-1",
        "Queued message",
        session_id="session-1",
        reply_surface=ReplySurface.webui(),
        project_id=None,
    )
    streaming_chat_loop.queue_run.assert_awaited_once_with(
        "agent-1",
        "Queued message",
        session_id="session-1",
        reply_surface=ReplySurface.webui(),
        project_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "loop_attribute"),
    (("chat.send", "chat_loop"), ("chat.stream", "streaming_chat_loop")),
)
async def test_chat_enqueue_cancellation_returns_error_envelope(
    method: str,
    loop_attribute: str,
) -> None:
    queued_item = _make_queued_item(item_id="queue-cancelled", content="Cancelled message")
    queued_item.future.cancel()
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("session already has an active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    state = SimpleNamespace(
        command_dispatcher=CommandDispatcher(ChatRunManager()),
        **{loop_attribute: chat_loop},
    )

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "Cancelled message",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "run_cancelled"
    assert "queue-cancelled" in response["error"]["message"]


@pytest.mark.asyncio
async def test_chat_send_busy_queue_bridges_started_run_to_event_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("session already has an active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    bridged_runs: list[Run] = []
    monkeypatch.setattr(
        event_bridge,
        "_bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )
    state = SimpleNamespace(
        chat_loop=chat_loop,
        command_dispatcher=CommandDispatcher(ChatRunManager()),
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "Queued message",
            },
        },
    )

    dequeued_run = Run(
        run_id="run-queued-send",
        agent_id="agent-1",
        session_id="session-1",
    )
    queued_item.future.set_result(dequeued_run)
    await asyncio.sleep(0)

    assert response["ok"] is True
    assert response["result"]["queued"] is True
    assert bridged_runs == [dequeued_run]


@pytest.mark.asyncio
async def test_chat_stream_busy_queue_bridges_started_run_to_event_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    streaming_chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("session already has an active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    bridged_runs: list[Run] = []
    monkeypatch.setattr(
        event_bridge,
        "_bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )
    state = SimpleNamespace(
        streaming_chat_loop=streaming_chat_loop,
        command_dispatcher=CommandDispatcher(ChatRunManager()),
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "Queued message",
            },
        },
    )

    dequeued_run = Run(
        run_id="run-queued-stream",
        agent_id="agent-1",
        session_id="session-1",
    )
    queued_item.future.set_result(dequeued_run)
    await asyncio.sleep(0)

    assert response["ok"] is True
    assert response["result"]["queued"] is True
    assert bridged_runs == [dequeued_run]


@pytest.mark.asyncio
async def test_chat_queue_list_returns_queued_items(monkeypatch: pytest.MonkeyPatch) -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    queue_manager = QueueManagerStub(items=[queued_item])
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_list",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "items": [queued_item.to_dict()],
        },
    }
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]


@pytest.mark.asyncio
async def test_chat_queue_list_hides_internal_items(monkeypatch: pytest.MonkeyPatch) -> None:
    public_item = _make_queued_item(item_id="queue-public", content="Visible")
    internal_item = _make_queued_item(item_id="queue-internal", content="Hidden", internal=True)
    queue_manager = QueueManagerStub(items=[public_item, internal_item])
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_list",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "items": [public_item.to_dict()],
        },
    }
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]


@pytest.mark.asyncio
async def test_chat_queue_remove_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-1", content="Queued message")],
        remove_result=True,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_remove",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-1",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "ok": True,
        },
    }
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert queue_manager.remove_calls == [("agent-1", "session-1", "queue-1", None)]


@pytest.mark.asyncio
async def test_chat_queue_remove_returns_error_for_unknown_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-1", content="Queued message")],
        remove_result=False,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_remove",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-404",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "queue_item_not_found"
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert queue_manager.remove_calls == []


@pytest.mark.asyncio
async def test_chat_queue_remove_returns_not_found_for_internal_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-internal", content="Hidden", internal=True)],
        remove_result=True,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_remove",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-internal",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "queue_item_not_found"
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert queue_manager.remove_calls == []


@pytest.mark.asyncio
async def test_chat_queue_update_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-1", content="Queued message")],
        update_result=True,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

    captured: dict[str, Any] = {}
    fake_executor = object()

    def fake_build_streaming_queue_update(
        _state: Any,
        agent_id: str,
        session_id: str,
        content: str | list[TextBlock],
        queued_item: QueuedRunItem,
        *,
        input_origin: str | None = None,
        project_id: str | None = None,
    ) -> tuple[str, Any, str]:
        captured["agent_id"] = agent_id
        captured["session_id"] = session_id
        captured["content"] = content
        captured["queued_item_id"] = queued_item.item_id
        captured["input_origin"] = input_origin
        captured["project_id"] = project_id
        return session_id, fake_executor, "Updated queued message"

    monkeypatch.setattr(
        chat_methods,
        "_build_streaming_queue_update",
        fake_build_streaming_queue_update,
    )

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_update",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-1",
                "content": [{"type": "text", "text": "Edited queued text"}],
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "ok": True,
        },
    }
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert captured == {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "content": [TextBlock(type="text", text="Edited queued text")],
        "queued_item_id": "queue-1",
        "input_origin": None,
        "project_id": None,
    }
    assert queue_manager.update_calls == [
        (
            "agent-1",
            "session-1",
            "queue-1",
            fake_executor,
            "Updated queued message",
            None,
            False,
        )
    ]


@pytest.mark.asyncio
async def test_chat_queue_update_returns_not_found_for_internal_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-internal", content="Hidden", internal=True)],
        update_result=True,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

    build_called = False

    def fail_if_called(*_args: Any, **_kwargs: Any) -> tuple[str, Any, str]:
        nonlocal build_called
        build_called = True
        return "session-1", object(), "should-not-build"

    monkeypatch.setattr(chat_methods, "_build_streaming_queue_update", fail_if_called)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_update",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-internal",
                "content": "Edited queued text",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "queue_item_not_found"
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert build_called is False
    assert queue_manager.update_calls == []
