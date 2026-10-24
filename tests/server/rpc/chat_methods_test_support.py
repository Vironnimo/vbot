"""Shared fixtures and fakes for chat methods behavior tests."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

from core.chat import ChatMessage, CommandDispatcher
from core.chat.messages import queue_content_is_editable
from core.runs import ActiveRunError, ChatRunManager, QueuedRunItem, Run, RunCancelledError
from core.sessions import SessionAddress
from core.sessions.errors import AccessorInputConflictError
from server.events import ServerEventBus
from server.file_delivery import FileDelivery
from tests.server.rpc_test_support import call, resource_changes

JsonObject = dict[str, Any]

__all__ = ["call", "resource_changes"]


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
        self.submissions: dict[str, Any] = {}
        self.submission_calls: list[JsonObject] = []
        self.manager: Any = None

    async def lookup_accessor_input(self, input_id: str, *, request_hash: str) -> Any:
        previous = self.submissions.get(input_id)
        if previous is not None and previous.record.request_hash != request_hash:
            raise AccessorInputConflictError("input id already names different input")
        return previous

    async def submit_accessor_input(
        self, agent_id: str, content: Any, *, input_id: str, request_hash: str, **kwargs: Any
    ) -> Any:
        self.submission_calls.append(
            {"agent_id": agent_id, "content": content, "input_id": input_id, **kwargs}
        )
        previous = await self.lookup_accessor_input(input_id, request_hash=request_hash)
        if previous is not None:
            return previous
        new_session = kwargs.pop("new_session", False)
        if new_session and kwargs.get("session_id") is None:
            kwargs["session_id"] = "new-session"
        item = None
        run: Run | None = None
        try:
            run = await self.start_run(agent_id, content, **kwargs)
        except ActiveRunError:
            item = await self.queue_run(agent_id, content, **kwargs)
            if item.future.cancelled():
                raise RunCancelledError(f"queued run cancelled: {item.item_id}") from None
            run = item.future.result() if item.future.done() else None
        record = SimpleNamespace(
            input_id=input_id,
            address=SessionAddress(kwargs.get("project_id"), agent_id, kwargs["session_id"]),
            request_hash=request_hash,
            state="run" if run is not None else "queued",
            run_id=run.id if run is not None else None,
            live=True,
        )
        result = SimpleNamespace(record=record, run=run, queued_item=item if run is None else None)
        self.submissions[input_id] = result
        return result

    async def remove_accessor_queued(self, address: SessionAddress, item_id: str) -> bool:
        return bool(
            self.manager.remove_queued(
                address.agent_id, address.session_id, item_id, project_id=address.project_id
            )
        )

    async def steer_accessor_queued(self, address: SessionAddress, item_id: str) -> QueuedRunItem:
        return cast(
            QueuedRunItem,
            self.manager.steer_queued(
                address.agent_id, address.session_id, item_id, project_id=address.project_id
            ),
        )

    async def update_accessor_queued(
        self, address: SessionAddress, item_id: str, content: Any, **kwargs: Any
    ) -> bool:
        items = getattr(self.manager, "items", None)
        if items is None:
            items = self.manager.list_queued(
                address.agent_id, address.session_id, project_id=address.project_id
            )
        item = next(item for item in items if item.item_id == item_id)
        resolved_id, executor, display = await self.build_queue_update(
            address.agent_id,
            address.session_id,
            content,
            item,
            project_id=address.project_id,
            **kwargs,
        )
        return bool(
            self.manager.update_queued(
                address.agent_id,
                resolved_id,
                item_id,
                executor,
                display,
                project_id=address.project_id,
                editable=queue_content_is_editable(content),
            )
        )

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


class CurrentSessionAgents:
    """Agent store double that records current-session pointer updates."""

    def __init__(self, current_session_id: str = "") -> None:
        self.current_session_id = current_session_id
        self.updates: list[tuple[str, str]] = []

    def get(self, agent_id: str) -> SimpleNamespace:
        return SimpleNamespace(current_session_id=self.current_session_id)

    def update(self, agent_id: str, **changes: Any) -> None:
        self.updates.append((agent_id, changes["current_session_id"]))


def chat_state(loop: _RecordingLoop | None = None, **runtime: Any) -> SimpleNamespace:
    """Minimal server state for chat RPCs: a recording loop, real Runs and event bus."""
    runtime.setdefault("agents", CurrentSessionAgents())
    state = SimpleNamespace(
        chat_loop=loop or _RecordingLoop(),
        chat_runs=ChatRunManager(),
        command_dispatcher=CommandDispatcher(ChatRunManager()),
        event_bus=ServerEventBus(),
        run_event_bridge_run_ids=OrderedDict(),
        file_delivery=FileDelivery(),
        runtime=SimpleNamespace(chat_sessions=_InlineSessionPool(), **runtime),
    )
    state.chat_loop.manager = state.chat_runs
    return state


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
