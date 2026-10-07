"""Server ownership of Live voice calls.

One Live call is active per server; starting another ends it. The accessor
that started a call owns it through one owner socket (protocol:
``server/live/owner.py``). The registry starts and stops calls, attaches owner
sockets, routes UI results, and keeps the final updates of an ended call
briefly for a reconnecting owner. Each call runs on its own host
(``server/live/_call.py``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from core.model_tasks.live import LiveCall, LiveCallHost
from server.events import ServerEventBus
from server.live._call import LiveCallEntry, LiveCallLimits
from server.live._context import RpcInvoker
from server.live.owner import LiveOwnerStream

JsonObject = dict[str, Any]

_LOGGER = logging.getLogger("vbot.server.live")

__all__ = [
    "LiveCallLimits",
    "LiveCallRegistry",
    "LiveRegistryClosedError",
    "LiveVoiceStarter",
]


class LiveVoiceStarter(Protocol):
    """The Live voice service surface the registry uses."""

    async def start_call(
        self,
        *,
        media: str,
        offer_sdp: str | None,
        host: LiveCallHost,
    ) -> LiveCall: ...


class LiveRegistryClosedError(Exception):
    """The server is shutting down and starts no further Live calls."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LiveCallRegistry:
    """Own the server's Live calls: one active call plus those still finishing.

    ``rpc`` dispatches one registered RPC method in-process; the call's Tools
    and Run announcements go through it so they behave like any accessor call.
    """

    def __init__(
        self,
        *,
        events: ServerEventBus,
        rpc: RpcInvoker,
        limits: LiveCallLimits | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._events = events
        self._rpc = rpc
        self._limits = limits or LiveCallLimits()
        self._clock = clock
        self._start_lock = asyncio.Lock()
        self._active: LiveCallEntry | None = None
        # Attachable entries: the active call, calls still closing, and ended
        # calls holding updates their owner has not received yet.
        self._entries: dict[str, LiveCallEntry] = {}
        self._linger: dict[str, asyncio.TimerHandle] = {}
        self._closed = False

    @property
    def active_call_id(self) -> str | None:
        """The id of the active call, if any."""
        return self._active.call_id if self._active is not None else None

    async def start(
        self,
        service: LiveVoiceStarter,
        *,
        media: str,
        offer_sdp: str | None = None,
        wake_phrases: tuple[str, ...] = (),
    ) -> LiveCall:
        """Start a call with *media* (WebRTC needs *offer_sdp*), ending the active call first.

        *wake_phrases* (validated) address other vBot Agents during the call.
        :class:`core.model_tasks.live.LiveStartRejected` propagates unchanged.
        """
        async with self._start_lock:
            if self._closed:
                raise LiveRegistryClosedError
            previous, self._active = self._active, None
            if previous is not None:
                await previous.replace()
            entry = LiveCallEntry(
                limits=self._limits,
                rpc=self._rpc,
                events=self._events,
                on_finalized=self._entry_finalized,
                started_at=self._clock(),
                after_sequence=self._events.last_sequence,
                wake_phrases=wake_phrases,
            )
            call = await service.start_call(media=media, offer_sdp=offer_sdp, host=entry)
            if self._closed:
                # Shutdown began while the provider created the call.
                entry.call = call
                await entry.replace()
                raise LiveRegistryClosedError
            self._entries[call.id] = entry
            self._active = entry
            entry.bind(call)
            return call

    def stop(self, call_id: str) -> bool:
        """Close the call gracefully in the background; ``False`` for unknown calls."""
        entry = self._entries.get(call_id)
        if entry is None or not entry.request_close():
            return False
        if self._active is entry:
            self._active = None
        return True

    def attach(self, call_id: str) -> LiveOwnerStream | None:
        """Attach an owner socket; ``None`` when no such call can be attached."""
        if self._closed:
            return None
        entry = self._entries.get(call_id)
        if entry is None:
            return None
        owner = entry.attach()
        if entry.ended:
            self._forget(entry)
        return owner

    def resolve_ui_request(
        self,
        call_id: str,
        request_id: str,
        *,
        result: JsonObject | None = None,
        error: str | None = None,
    ) -> bool:
        """Answer a UI request of *call_id*; ``False`` when nothing awaits it."""
        entry = self._entries.get(call_id)
        if entry is None:
            return False
        return entry.resolve_ui_request(request_id, result=result, error=error)

    async def aclose(self) -> None:
        """End every call at server shutdown; starts are refused afterwards."""
        self._closed = True
        self._active = None
        entries = list(self._entries.values())
        try:
            await asyncio.gather(*(entry.shutdown() for entry in entries))
        finally:
            for handle in self._linger.values():
                handle.cancel()
            self._linger.clear()
            self._entries.clear()

    def _entry_finalized(self, entry: LiveCallEntry) -> None:
        if self._active is entry:
            self._active = None
        if self._closed or not entry.has_undelivered_updates:
            self._forget(entry)
            return
        # Keep final updates attachable briefly for an owner that is reconnecting.
        self._linger[entry.call_id] = asyncio.get_running_loop().call_later(
            self._limits.reattach_grace_seconds, self._forget, entry
        )

    def _forget(self, entry: LiveCallEntry) -> None:
        call_id = entry.call_id
        if self._entries.get(call_id) is entry:
            del self._entries[call_id]
        handle = self._linger.pop(call_id, None)
        if handle is not None:
            handle.cancel()
