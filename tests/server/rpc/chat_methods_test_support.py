"""Shared fixtures and fakes for chat methods behavior tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from core.chat import (
    ChatMessage,
    CommandDispatcher,
    CommandExecutionContext,
    CommandOutcome,
    ReplySurface,
)
from core.runs import ActiveRunError, ChatRunManager, QueuedRunItem, Run, RunKind
from server.events import ServerEventBus
from tests.server.rpc_test_support import call, resource_changes

JsonObject = dict[str, Any]

__all__ = ["call", "resource_changes"]


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


def finished_run(
    run_id: str = "run-1",
    *,
    agent_id: str = "builder",
    session_id: str = "s1",
    project_id: str | None = None,
    content: str = "Done",
) -> Run:
    """A real Run that already completed with one assistant answer."""
    run = Run(run_id=run_id, agent_id=agent_id, session_id=session_id, project_id=project_id)
    run.mark_completed(ChatMessage.assistant(content=content, model="openai/gpt-5.2"))
    return run


async def _unused_executor(_run: Run) -> None:
    return None


class _RecordingLoop:
    """Chat loop double: records every submission it receives.

    ``busy`` makes ``start_run`` report an active Run so the RPC falls back to
    the Queue; ``queued_run`` completes the queued item's future before the RPC
    sees it (the busy-to-idle race), and ``cancel_queued`` cancels it instead.
    """

    def __init__(
        self,
        *,
        busy: bool = False,
        queued_run: Run | None = None,
        cancel_queued: bool = False,
        resolved_session_id: str | None = None,
    ) -> None:
        self.start_calls: list[JsonObject] = []
        self.queue_calls: list[JsonObject] = []
        self.edit_calls: list[JsonObject] = []
        self.build_calls: list[JsonObject] = []
        self.start_error: Exception | None = (
            ActiveRunError("session already has an active run") if busy else None
        )
        self.queued_items: list[QueuedRunItem] = []
        self._queued_run = queued_run
        self._cancel_queued = cancel_queued
        self._resolved_session_id = resolved_session_id

    async def start_run(self, agent_id: str, content: Any, **kwargs: Any) -> Run:
        if self.start_error is not None:
            raise self.start_error
        self.start_calls.append({"agent_id": agent_id, "content": content, **kwargs})
        return finished_run(
            agent_id=agent_id,
            session_id=kwargs["session_id"],
            project_id=kwargs.get("project_id"),
        )

    async def queue_run(self, agent_id: str, content: Any, **kwargs: Any) -> QueuedRunItem:
        self.queue_calls.append({"agent_id": agent_id, "content": content, **kwargs})
        item = QueuedRunItem(
            item_id=f"queued-{len(self.queue_calls)}",
            display_content=content if isinstance(content, str) else "[attachment]",
            executor=_unused_executor,
            internal=False,
            future=asyncio.get_running_loop().create_future(),
            editable=isinstance(content, str),
            created_at="2026-08-21T12:00:00+00:00",
        )
        if self._queued_run is not None:
            item.future.set_result(self._queued_run)
        elif self._cancel_queued:
            item.future.cancel()
        self.queued_items.append(item)
        return item

    async def edit_run(self, agent_id: str, content: str, **kwargs: Any) -> Run:
        if self.start_error is not None:
            raise self.start_error
        self.edit_calls.append({"agent_id": agent_id, "content": content, **kwargs})
        return finished_run(
            "run-edit",
            agent_id=agent_id,
            session_id=kwargs["session_id"],
            project_id=kwargs.get("project_id"),
        )

    async def build_queue_update(
        self,
        agent_id: str,
        session_id: str,
        content: Any,
        queued_item: QueuedRunItem,
        **kwargs: Any,
    ) -> tuple[str, Any, str]:
        self.build_calls.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "content": content,
                "queued_item": queued_item,
                **kwargs,
            }
        )
        return self._resolved_session_id or session_id, _unused_executor, "Edited preview"


class _InlineSessionPool:
    """Stands in for the Session database's pool: runs the unit inline."""

    async def run_async(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)


class _NoCommandDispatcher:
    """Treats every message as plain chat (no slash command recognized)."""

    def prepare(self, content: Any) -> None:
        return None


def chat_state(
    loop: _RecordingLoop | None = None,
    *,
    streaming_loop: _RecordingLoop | None = None,
    **runtime: Any,
) -> SimpleNamespace:
    """Minimal server state for chat RPCs: recording loops, real Runs and event bus."""
    chat_loop = loop or _RecordingLoop()
    return SimpleNamespace(
        chat_loop=chat_loop,
        streaming_chat_loop=streaming_loop or chat_loop,
        chat_runs=ChatRunManager(),
        command_dispatcher=CommandDispatcher(ChatRunManager()),
        event_bus=ServerEventBus(),
        runtime=SimpleNamespace(chat_sessions=_InlineSessionPool(), **runtime),
    )


async def bridged_run_ids(state: Any) -> set[str]:
    """Ids of the Runs whose timeline reached the server event bus."""
    # The bridge replays a finished Run from a background task in a few loop turns.
    for _ in range(10):
        await asyncio.sleep(0)
    return {
        event["payload"]["run_id"]
        for event in state.event_bus.events
        if "run_id" in event["payload"]
    }


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
