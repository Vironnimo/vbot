"""Tests for chat methods dispatch."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.chat import (
    CommandDispatcher,
    CommandExecutionContext,
    CommandFeedback,
    CommandNavigation,
    CommandOutcome,
    CommandResourceChange,
    ExtensionCommandContext,
    PreparedCommand,
)
from core.runs import (
    ChatRunManager,
)
from server.events import ServerEventBus
from server.rpc import chat_methods
from server.rpc.methods import dispatch_rpc


def test_command_page_navigation_projects_without_session_destination() -> None:
    result = chat_methods._command_outcome_response(
        CommandOutcome(
            command="workflow",
            feedback=CommandFeedback(kind="notice", text="Workflow is ready."),
            navigation=CommandNavigation(
                kind="open_extension_page", extension="fixture", page="overview", route="items/one"
            ),
        )
    )

    assert result == {
        "command_handled": True,
        "reply": "Workflow is ready.",
        "output": "action",
        "data": {
            "command": "workflow",
            "navigation": {
                "kind": "open_extension_page",
                "extension": "fixture",
                "page": "overview",
                "route": "items/one",
            },
        },
    }


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
