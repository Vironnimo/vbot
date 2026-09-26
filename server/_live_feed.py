"""Feed finished vBot Runs from the server event bus into one Live call."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.model_tasks.live import LiveRunNotice
from core.projects import format_agent_address
from core.runs import RUN_AGENT_ACTIVITY_FIELD
from server._live_context import RpcInvoker
from server.events import (
    RUN_COMPLETED_SERVER_EVENT,
    RUN_FAILED_SERVER_EVENT,
    RUN_INTERRUPTED_SERVER_EVENT,
    ServerEventBus,
)
from server.rpc.errors import RpcError

JsonObject = dict[str, Any]

_LOGGER = logging.getLogger("vbot.server.live")

# The announced outcome per terminal Run event; cancelled Runs stay silent.
_ANNOUNCED_OUTCOMES = {
    RUN_COMPLETED_SERVER_EVENT: "completed",
    RUN_FAILED_SERVER_EVENT: "failed",
    RUN_INTERRUPTED_SERVER_EVENT: "interrupted",
}
_SEEN_RUN_LIMIT = 2_000
_PENDING_LIMIT = 100
_RESUBSCRIBE_DELAY_SECONDS = 0.1


@dataclass(frozen=True)
class _PendingNotice:
    kind: str
    run_id: str
    agent_id: str
    session_id: str


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class LiveRunFeed:
    """Announce Runs that finish while a Live call is live, once each.

    The feed follows the shared event bus from the call's start, skips Runs
    finished before the call and Runs that do not contribute to Agent
    activity, and loads each excerpt through the canonical ``chat.run_result``
    RPC so newer replies in the same Session never replace it.
    ``describe_session`` gives each announced Session the call's ref for it.
    """

    def __init__(
        self,
        *,
        events: ServerEventBus,
        rpc: RpcInvoker,
        announce: Callable[[LiveRunNotice], None],
        describe_session: Callable[[str, str], str],
        report_failure: Callable[[], None],
        started_at: datetime,
        after_sequence: int,
    ) -> None:
        self._events = events
        self._rpc = rpc
        self._announce = announce
        self._describe_session = describe_session
        self._report_failure = report_failure
        self._started_at = started_at
        self._after_sequence = after_sequence
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pending: asyncio.Queue[_PendingNotice] = asyncio.Queue(maxsize=_PENDING_LIMIT)
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        """Start following the bus; call once from the Event Loop."""
        self._tasks = [
            asyncio.create_task(self._follow()),
            asyncio.create_task(self._deliver()),
        ]

    async def aclose(self) -> None:
        """Stop following and discard notices not yet announced."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def _follow(self) -> None:
        after_sequence = self._after_sequence
        while True:
            async with aclosing(self._events.subscribe(after_sequence=after_sequence)) as events:
                async for event in events:
                    sequence = event.get("sequence")
                    if isinstance(sequence, int):
                        after_sequence = sequence
                    self._consider(event)
            # The bus evicted this subscriber after it lagged; resume after the
            # last event seen. Runs that left retention meanwhile stay silent.
            await asyncio.sleep(_RESUBSCRIBE_DELAY_SECONDS)

    def _consider(self, event: JsonObject) -> None:
        kind = _ANNOUNCED_OUTCOMES.get(str(event.get("type")))
        payload = event.get("payload")
        if kind is None or not isinstance(payload, dict):
            return
        if payload.get(RUN_AGENT_ACTIVITY_FIELD) is False:
            return
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in self._seen:
            return
        finished_at = _parse_timestamp(payload.get("run_event_timestamp") or event.get("timestamp"))
        if finished_at is not None and finished_at < self._started_at:
            self._remember(run_id)
            return
        agent_id = payload.get("agent_id")
        project_id = payload.get("project_id")
        session_id = payload.get("session_id")
        if not isinstance(agent_id, str) or not agent_id:
            return
        if not isinstance(session_id, str) or not session_id:
            return
        self._remember(run_id)
        address = format_agent_address(
            agent_id, project_id if isinstance(project_id, str) and project_id else None
        )
        try:
            self._pending.put_nowait(_PendingNotice(kind, run_id, address, session_id))
        except asyncio.QueueFull:
            _LOGGER.warning("Live Run announcement dropped; backlog full (run_id=%s)", run_id)

    def _remember(self, run_id: str) -> None:
        self._seen[run_id] = None
        while len(self._seen) > _SEEN_RUN_LIMIT:
            self._seen.popitem(last=False)

    async def _deliver(self) -> None:
        while True:
            pending = await self._pending.get()
            try:
                result = await self._rpc(
                    "chat.run_result",
                    {
                        "agent_id": pending.agent_id,
                        "session_id": pending.session_id,
                        "run_id": pending.run_id,
                    },
                )
            except RpcError as exc:
                _LOGGER.warning(
                    "Live Run announcement unavailable (run_id=%s code=%s)",
                    pending.run_id,
                    exc.code,
                )
                self._report_failure()
                continue
            except Exception:
                _LOGGER.exception(
                    "Live Run announcement failed unexpectedly (run_id=%s)", pending.run_id
                )
                self._report_failure()
                continue
            notice = LiveRunNotice(
                kind=pending.kind,
                run_id=pending.run_id,
                agent_id=pending.agent_id,
                session_id=pending.session_id,
                session_ref=self._describe_session(pending.agent_id, pending.session_id),
                excerpt=str(result.get("content") or ""),
                truncated=bool(result.get("truncated")),
            )
            try:
                self._announce(notice)
            except Exception:
                _LOGGER.exception("Live Run announcement rejected (run_id=%s)", pending.run_id)
