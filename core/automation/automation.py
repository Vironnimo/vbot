"""Automation primitives for programmatic chat run triggering."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, cast

from core.chat import ActiveRunError, ChatLoop, ChatRunManager, Run
from core.chat.runs import TERMINAL_EVENT_TYPES
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime

QueuedTrigger = tuple[str, asyncio.Future[Run]]
SessionKey = tuple[str, str]

_LOGGER = get_logger("automation")


class TriggerService:
    """Start programmatic chat runs and queue triggers behind active runs."""

    def __init__(
        self,
        chat_loop: ChatLoop,
        chat_run_manager: ChatRunManager,
        runtime: Runtime,
    ) -> None:
        self._chat_loop = chat_loop
        self._chat_run_manager = chat_run_manager
        self._runtime = runtime
        self._queues: dict[SessionKey, deque[QueuedTrigger]] = {}
        self._subscriber_tasks: dict[SessionKey, asyncio.Task[None]] = {}

    async def trigger_run(
        self,
        agent_id: str,
        message: str,
        session_id: str | None = None,
    ) -> Run:
        """Start a run immediately, or queue it until the target session is idle."""
        if session_id is None:
            session = self._runtime.chat_sessions.create(agent_id)
            return await self._chat_loop.start_run(agent_id, message, session_id=session.id)

        try:
            return await self._chat_loop.start_run(agent_id, message, session_id=session_id)
        except ActiveRunError:
            active_run = self._chat_run_manager.active_run(
                agent_id=agent_id,
                session_id=session_id,
            )
            if active_run is None:
                return await self._chat_loop.start_run(agent_id, message, session_id=session_id)
            return await self._queue_trigger(agent_id, session_id, message, active_run)

    async def _queue_trigger(
        self,
        agent_id: str,
        session_id: str,
        message: str,
        active_run: Run,
    ) -> Run:
        key = (agent_id, session_id)
        queue = self._queues.setdefault(key, deque())
        start_subscriber = not queue
        future = cast(asyncio.Future[Run], asyncio.get_running_loop().create_future())
        queue.append((message, future))

        if start_subscriber:
            self._start_subscriber(agent_id, session_id, active_run)

        _LOGGER.info(
            "Queued trigger for agent=%s session=%s queue_size=%s",
            agent_id,
            session_id,
            len(queue),
        )
        return await future

    def _start_subscriber(self, agent_id: str, session_id: str, active_run: Run) -> None:
        key = (agent_id, session_id)
        task = asyncio.create_task(self._subscribe_and_drain(agent_id, session_id, active_run))
        self._subscriber_tasks[key] = task
        task.add_done_callback(lambda completed: self._log_subscriber_result(key, completed))

    async def _subscribe_and_drain(
        self,
        agent_id: str,
        session_id: str,
        active_run: Run,
    ) -> None:
        """Wait for active runs to finish, then start queued triggers in FIFO order."""
        key = (agent_id, session_id)
        current_run = active_run

        try:
            while True:
                await self._wait_for_terminal_event(current_run)
                next_run = await self._start_next_queued_run(key)
                if next_run is None:
                    return
                current_run = next_run
        except asyncio.CancelledError:
            self._cancel_queued_triggers(key)
            raise
        except Exception as error:
            self._fail_queued_triggers(key, error)
            raise
        finally:
            if self._subscriber_tasks.get(key) is asyncio.current_task():
                self._subscriber_tasks.pop(key, None)

    async def _wait_for_terminal_event(self, run: Run) -> None:
        async for event in run.subscribe():
            if event.type in TERMINAL_EVENT_TYPES:
                return

    async def _start_next_queued_run(self, key: SessionKey) -> Run | None:
        queue = self._queues.get(key)
        if not queue:
            self._queues.pop(key, None)
            return None

        message, future = queue[0]

        try:
            run = await self._chat_loop.start_run(key[0], message, session_id=key[1])
        except ActiveRunError as error:
            active_run = self._chat_run_manager.active_run(agent_id=key[0], session_id=key[1])
            if active_run is not None:
                return active_run
            queue.popleft()
            if not queue:
                self._queues.pop(key, None)
            if not future.done():
                future.set_exception(error)
            return await self._start_next_queued_run(key)
        except Exception as error:
            queue.popleft()
            if not queue:
                self._queues.pop(key, None)
            _LOGGER.error(
                "Failed to start queued trigger for agent=%s session=%s: %s",
                key[0],
                key[1],
                error,
                exc_info=True,
            )
            if not future.done():
                future.set_exception(error)
            return await self._start_next_queued_run(key)

        queue.popleft()
        if not queue:
            self._queues.pop(key, None)
        if not future.done():
            future.set_result(run)
        return run if self._queues.get(key) else None

    def _fail_queued_triggers(self, key: SessionKey, error: BaseException) -> None:
        queue = self._queues.pop(key, deque())
        for _message, future in queue:
            if not future.done():
                future.set_exception(error)

    def _cancel_queued_triggers(self, key: SessionKey) -> None:
        queue = self._queues.pop(key, deque())
        for _message, future in queue:
            if not future.done():
                future.cancel()

    def _log_subscriber_result(
        self,
        key: SessionKey,
        task: asyncio.Task[None],
    ) -> None:
        if task.cancelled():
            self._cancel_queued_triggers(key)
            if self._subscriber_tasks.get(key) is task:
                self._subscriber_tasks.pop(key, None)
            return
        error = task.exception()
        if error is None:
            return
        _LOGGER.error(
            "Trigger queue subscriber failed for agent=%s session=%s: %s",
            key[0],
            key[1],
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
