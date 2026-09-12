"""Shared fixtures and fakes for chat methods behavior tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from core.chat import (
    ChatMessage,
    CommandDispatcher,
    CommandExecutionContext,
    CommandOutcome,
    ReplySurface,
)
from core.runs import RunKind


class _FakeRun:
    def __init__(self, run_id: str = "run-1") -> None:
        self.id = run_id
        self.agent_id = "builder"
        self.session_id = "s1"
        # ``_run_response`` reads ``status.value`` and ``events``; a finished run
        # with no events is enough for these address-threading assertions.
        self.status = SimpleNamespace(value="completed")
        self.run_kind = RunKind.USER
        self.created_at = "2026-08-05T18:00:00+00:00"
        self.iteration_count = 1
        self.events: list[Any] = []

    async def wait(self) -> ChatMessage:
        return ChatMessage.assistant(content="handoff text", model="openai/gpt-5.2")

    def controls(self) -> dict:
        return {"compaction": "unavailable", "background_tool_call_ids": []}


class _RecordingLoop:
    """Records the ``project_id`` each public entry was called with."""

    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.start_error: Exception | None = None

    async def start_run(self, agent_id: str, content: Any, **kwargs: Any) -> _FakeRun:
        if self.start_error is not None:
            raise self.start_error
        self.start_calls.append({"agent_id": agent_id, "content": content, **kwargs})
        return _FakeRun()

    async def queue_run(self, agent_id: str, content: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            future=asyncio.Future(),
            item_id="queued-1",
            display_content=content,
            editable=False,
            internal=False,
            run_kind=RunKind.USER,
            created_at="2026-08-21T12:00:00+00:00",
            to_dict=lambda: {
                "id": "queued-1",
                "content": content,
                "editable": False,
                "internal": False,
                "run_kind": "user",
                "created_at": "2026-08-21T12:00:00+00:00",
            },
        )


class _NoCommandDispatcher:
    """Treats every message as plain chat (no slash command recognized)."""

    def prepare(self, content: Any) -> None:
        return None


def _core_dispatcher(state: SimpleNamespace) -> CommandDispatcher:
    runtime = state.runtime
    return CommandDispatcher(
        state.chat_runs,
        agent_resolver=getattr(runtime, "agent_resolver", None),
        sessions=getattr(runtime, "chat_sessions", None),
        models=getattr(runtime, "models", None),
        projects=getattr(runtime, "projects", None),
        agents=getattr(runtime, "agents", None),
        trigger_service=getattr(runtime, "trigger_service", None),
        reflection_service=getattr(runtime, "reflection", None),
        storage=getattr(runtime, "storage", None),
        terminal_manager=getattr(runtime, "terminal_manager", None),
    )


async def _execute_core_command(
    state: SimpleNamespace,
    message: str,
    *,
    agent_id: str = "builder",
    session_id: str = "s1",
    project_id: str | None = None,
) -> CommandOutcome:
    dispatcher = _core_dispatcher(state)
    prepared = dispatcher.prepare(message)
    assert prepared is not None
    observed_changes = getattr(state, "_command_changes", None)
    return await dispatcher.execute(
        prepared,
        CommandExecutionContext(
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            reply_surface=ReplySurface.webui(),
            on_change=observed_changes.append if observed_changes is not None else None,
        ),
    )
