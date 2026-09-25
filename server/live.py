"""Server ownership of Live voice calls.

One Live call is active per server; starting another ends it. The accessor that
started a call owns its media and display: it attaches one owner socket at
``/ws/live/{call_id}`` and answers UI requests through ``live.ui_result``. The
registry buffers call updates until the owner attaches, ends a call whose owner
never attaches or does not return, runs the call's Live Tools, and feeds vBot
Runs that finish during the call to it.

Owner socket frames, server to accessor:

* JSON text: call updates as the call publishes them (``state``, ``caption``,
  ``activity``, ``playback_clear``, ``error``, ``closed``); a call replaced by
  a newer start gets ``{"type": "closed", "reason": "replaced", "usage": null}``,
  and a call that ends without its own ``closed`` update gets one with
  ``reason: null``;
* JSON text: ``{"type": "ui_request", "request_id", "action", "args"}`` - see
  ``server/_live_tools.py`` for ``context``, ``open`` and ``terminal_view``;
* JSON text: ``{"type": "heartbeat", "timestamp"}`` while otherwise idle;
* binary, relay calls only: assistant audio as raw PCM in the call's
  ``media.audio`` format. Audio is never buffered: it is dropped while no
  owner is attached.

Accessor to server: binary frames of a relay call are microphone PCM in the
same format (even length, at most 64 KiB each); malformed binary frames and
all text frames are ignored.

Close codes: 1000 after the ``closed`` update, 1008 for an unknown or already
forgotten call, 1013 when the owner fell behind, 4000 when a newer owner socket
replaced this one (do not reconnect). After any other close the owner may
reconnect within the reattach grace; the call ends if it does not.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from core.model_tasks.live import LiveCall, LiveCallHost
from core.utils.ids import new_id
from server._live_context import UI_TIMEOUT, UI_UNAVAILABLE, LiveUiError, RpcInvoker
from server._live_feed import LiveRunFeed
from server._live_tools import LiveToolExecutor
from server.events import ServerEventBus

JsonObject = dict[str, Any]

_LOGGER = logging.getLogger("vbot.server.live")

LIVE_SOCKET_CLOSE_ENDED = 1000
LIVE_SOCKET_CLOSE_UNKNOWN_CALL = 1008
LIVE_SOCKET_CLOSE_LAGGED = 1013
LIVE_SOCKET_CLOSE_REPLACED = 4000

LIVE_AUDIO_FRAME_MAX_BYTES = 64 * 1024

CLOSED_REASON_REPLACED = "replaced"
_NOTIFICATION_FAILED = "notification_failed"


@dataclass(frozen=True)
class LiveCallLimits:
    """Timeouts and bounds of Live call ownership."""

    attach_timeout_seconds: float = 15.0
    reattach_grace_seconds: float = 10.0
    ui_request_timeout_seconds: float = 20.0
    update_buffer_limit: int = 200
    owner_queue_limit: int = 1_000
    shutdown_close_timeout_seconds: float = 5.0
    abort_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.owner_queue_limit < self.update_buffer_limit:
            raise ValueError("owner_queue_limit must hold the whole update buffer")


class LiveVoiceStarter(Protocol):
    """The Live voice service surface the registry uses."""

    async def start_call(
        self, *, media: str, offer_sdp: str | None, host: LiveCallHost
    ) -> LiveCall: ...


class LiveRegistryClosedError(Exception):
    """The server is shutting down and starts no further Live calls."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


OwnerFrame = JsonObject | bytes


class LiveOwnerStream:
    """Frames for one attached owner socket, in delivery order.

    JSON objects go out as text frames and ``bytes`` as binary audio frames.
    """

    def __init__(self, entry: _LiveCallEntry, limit: int) -> None:
        self._entry = entry
        self._limit = limit
        self._frames: deque[OwnerFrame] = deque()
        self._wakeup = asyncio.Event()
        self._close_code: int | None = None
        self._finished = False

    @property
    def close_code(self) -> int | None:
        """The code to close the socket with once the stream ended, else ``None``."""
        return self._close_code

    @property
    def finished(self) -> bool:
        """Whether :meth:`frames` yielded every frame and ended; close the socket then."""
        return self._finished

    def send(self, frame: OwnerFrame) -> bool:
        """Queue one frame; ``False`` when the stream ended or fell behind."""
        if self._close_code is not None or len(self._frames) >= self._limit:
            return False
        self._frames.append(frame)
        self._wakeup.set()
        return True

    def end(self, code: int) -> None:
        """End the stream after the queued frames; the first code wins."""
        if self._close_code is None:
            self._close_code = code
            self._wakeup.set()

    async def frames(self) -> AsyncGenerator[OwnerFrame, None]:
        """Yield queued frames until the stream ends."""
        while True:
            while self._frames:
                yield self._frames.popleft()
            if self._close_code is not None:
                self._finished = True
                return
            self._wakeup.clear()
            await self._wakeup.wait()

    def receive_audio(self, pcm: bytes) -> None:
        """Hand one inbound binary frame to the call as microphone audio."""
        self._entry.receive_audio(self, pcm)

    def detach(self) -> None:
        """Release ownership after the socket closed."""
        self._entry.detach(self)


class _LiveCallEntry:
    """The server side of one call: its host, owner socket, UI requests and feed."""

    def __init__(
        self,
        *,
        limits: LiveCallLimits,
        rpc: RpcInvoker,
        events: ServerEventBus,
        on_finalized: Callable[[_LiveCallEntry], None],
        started_at: datetime,
        after_sequence: int,
    ) -> None:
        self._limits = limits
        self._rpc = rpc
        self._events = events
        self._on_finalized = on_finalized
        self._started_at = started_at
        self._after_sequence = after_sequence
        self.call: LiveCall | None = None
        # Accepts further Tool effects and UI requests; false once stopping.
        self.active = True
        self.ended = False
        self._closing = False
        self._closed_published = False
        self._finalized = False
        self._buffer: deque[JsonObject] = deque(maxlen=limits.update_buffer_limit)
        self._owner: LiveOwnerStream | None = None
        self._malformed_audio_logged = False
        self._ui_requests: dict[str, asyncio.Future[JsonObject]] = {}
        self._tool_lock = asyncio.Lock()
        self._executor = LiveToolExecutor(
            rpc=rpc, ui=self.ui_request, is_active=self._is_active, started_at=started_at
        )
        self._feed: LiveRunFeed | None = None
        self._timer: asyncio.Task[None] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def call_id(self) -> str:
        return self.call.id if self.call is not None else ""

    @property
    def has_undelivered_updates(self) -> bool:
        return bool(self._buffer)

    def _is_active(self) -> bool:
        return self.active

    # -- LiveCallHost -----------------------------------------------------

    async def execute_tool(self, name: str, arguments: JsonObject) -> JsonObject:
        """Run one prepared Live Tool call; executions of one call never overlap."""
        async with self._tool_lock:
            return await self._executor.execute(name, arguments)

    def publish(self, update: JsonObject) -> None:
        """Deliver one call update to the owner, or buffer it until one attaches."""
        if self._closed_published:
            return
        if update.get("type") == "closed":
            self._closed_published = True
            self.active = False
            self._cancel_timer()
        self._deliver(update)
        if self._closed_published and self._owner is not None:
            self._owner.end(LIVE_SOCKET_CLOSE_ENDED)

    def publish_audio(self, pcm: bytes) -> None:
        """Send assistant audio to the attached owner; dropped while none is attached."""
        owner = self._owner
        if owner is None or self._closed_published or not pcm:
            return
        if not owner.send(pcm):
            self._owner_lagged(owner)

    # -- lifecycle --------------------------------------------------------

    def bind(self, call: LiveCall) -> None:
        """Start ownership of the provider call created for this host."""
        self.call = call
        self._feed = LiveRunFeed(
            events=self._events,
            rpc=self._rpc,
            announce=call.announce_run,
            describe_session=self._executor.session_ref,
            report_failure=self._report_notification_failure,
            started_at=self._started_at,
            after_sequence=self._after_sequence,
        )
        self._feed.start()
        self._watcher = self._spawn(self._watch(call))
        self._arm_timer(self._limits.attach_timeout_seconds, "did not attach")

    def request_close(self) -> bool:
        """Close gracefully in the background; ``False`` once the call ended."""
        call = self.call
        if self.ended or call is None:
            return False
        if not self._closing:
            self._closing = True
            self.active = False
            self._spawn(self._close(call))
        return True

    async def replace(self) -> None:
        """End the call immediately because a newer call starts."""
        self.active = False
        self._closing = True
        self.publish({"type": "closed", "reason": CLOSED_REASON_REPLACED, "usage": None})
        await self._abort()

    async def shutdown(self) -> None:
        """Close within the shutdown bound, abort otherwise, and release everything."""
        self.active = False
        call = self.call
        if call is not None and not self.ended:
            self._closing = True
            try:
                await asyncio.wait_for(call.close(), self._limits.shutdown_close_timeout_seconds)
            except TimeoutError:
                _LOGGER.warning("Live call did not close during shutdown (call_id=%s)", call.id)
                await self._abort()
            except Exception:
                _LOGGER.exception("Live call close failed during shutdown (call_id=%s)", call.id)
                await self._abort()
        watcher = self._watcher
        if watcher is not None and not watcher.done():
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(watcher), 1.0)
        remaining = [task for task in self._tasks if not task.done()]
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        # Publishes the final ``closed`` update, which ends the owner socket.
        await self._finalize()

    async def _close(self, call: LiveCall) -> None:
        try:
            await call.close()
        except Exception:
            _LOGGER.exception("Live call close failed; aborting (call_id=%s)", call.id)
            await self._abort()

    async def _abort(self) -> None:
        call = self.call
        if call is None:
            return
        try:
            await asyncio.wait_for(call.abort(), self._limits.abort_timeout_seconds)
        except TimeoutError:
            _LOGGER.warning("Live call abort timed out (call_id=%s)", call.id)
        except Exception:
            _LOGGER.exception("Live call abort failed (call_id=%s)", call.id)

    async def _watch(self, call: LiveCall) -> None:
        try:
            await call.wait_closed()
        except Exception:
            _LOGGER.exception("Live call ended with an error (call_id=%s)", call.id)
        await self._finalize()

    async def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        self.ended = True
        self.active = False
        self._cancel_timer()
        if self._feed is not None:
            await self._feed.aclose()
        for future in self._ui_requests.values():
            if not future.done():
                future.set_exception(LiveUiError(UI_UNAVAILABLE))
        if not self._closed_published:
            self.publish({"type": "closed", "reason": None, "usage": None})
        self._on_finalized(self)

    def _report_notification_failure(self) -> None:
        self.publish({"type": "error", "code": _NOTIFICATION_FAILED, "fatal": False})

    # -- owner socket -----------------------------------------------------

    def attach(self) -> LiveOwnerStream:
        """Make a new socket the owner; a previous owner socket is replaced."""
        previous = self._owner
        if previous is not None:
            previous.end(LIVE_SOCKET_CLOSE_REPLACED)
        owner = LiveOwnerStream(self, self._limits.owner_queue_limit)
        while self._buffer:
            owner.send(self._buffer.popleft())
        self._owner = owner
        self._cancel_timer()
        if self.ended or self._closed_published:
            owner.end(LIVE_SOCKET_CLOSE_ENDED)
        return owner

    def detach(self, owner: LiveOwnerStream) -> None:
        """Forget a closed owner socket and allow a bounded reconnect."""
        if self._owner is not owner:
            return
        self._owner = None
        self._arm_timer(self._limits.reattach_grace_seconds, "did not return")

    def receive_audio(self, owner: LiveOwnerStream, pcm: bytes) -> None:
        """Forward microphone audio from the current owner socket to the call."""
        call = self.call
        if owner is not self._owner or call is None or self.ended:
            return
        if not pcm or len(pcm) % 2 or len(pcm) > LIVE_AUDIO_FRAME_MAX_BYTES:
            if not self._malformed_audio_logged:
                self._malformed_audio_logged = True
                _LOGGER.warning(
                    "Live owner sent a malformed audio frame; dropping it (call_id=%s bytes=%d)",
                    self.call_id,
                    len(pcm),
                )
            return
        call.push_audio(pcm)

    def _deliver(self, frame: JsonObject) -> None:
        owner = self._owner
        if owner is not None:
            if owner.send(frame):
                return
            self._owner_lagged(owner)
        self._buffer.append(frame)

    def _owner_lagged(self, owner: LiveOwnerStream) -> None:
        _LOGGER.warning("Live owner socket fell behind (call_id=%s)", self.call_id)
        self._owner = None
        owner.end(LIVE_SOCKET_CLOSE_LAGGED)
        self._arm_timer(self._limits.reattach_grace_seconds, "did not return")

    def _arm_timer(self, seconds: float, reason: str) -> None:
        self._cancel_timer()
        if self.call is None or self.ended or self._closed_published:
            return
        self._timer = self._spawn(self._expire(seconds, reason))

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    async def _expire(self, seconds: float, reason: str) -> None:
        await asyncio.sleep(seconds)
        # From here on an attaching socket no longer cancels this abort.
        self._timer = None
        _LOGGER.warning("Live call owner %s; ending call (call_id=%s)", reason, self.call_id)
        self.active = False
        await self._abort()

    # -- UI requests ------------------------------------------------------

    async def ui_request(self, action: str, args: JsonObject) -> JsonObject:
        """Ask the owning accessor to read or change its display."""
        if self._owner is None:
            raise LiveUiError(UI_UNAVAILABLE)
        request_id = new_id("ui", claim=lambda candidate: candidate not in self._ui_requests)
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._ui_requests[request_id] = future
        try:
            self._deliver(
                {"type": "ui_request", "request_id": request_id, "action": action, "args": args}
            )
            return await asyncio.wait_for(future, self._limits.ui_request_timeout_seconds)
        except TimeoutError as exc:
            raise LiveUiError(UI_TIMEOUT, uncertain=True) from exc
        finally:
            self._ui_requests.pop(request_id, None)

    def resolve_ui_request(
        self, request_id: str, *, result: JsonObject | None, error: str | None
    ) -> bool:
        """Complete a pending UI request; ``False`` when it is unknown or already done."""
        future = self._ui_requests.get(request_id)
        if future is None or future.done():
            return False
        if error is not None:
            future.set_exception(LiveUiError(error))
        else:
            future.set_result(result if result is not None else {})
        return True

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


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
        self._active: _LiveCallEntry | None = None
        # Attachable entries: the active call, calls still closing, and ended
        # calls holding updates their owner has not received yet.
        self._entries: dict[str, _LiveCallEntry] = {}
        self._linger: dict[str, asyncio.TimerHandle] = {}
        self._closed = False

    @property
    def active_call_id(self) -> str | None:
        """The id of the active call, if any."""
        return self._active.call_id if self._active is not None else None

    async def start(
        self, service: LiveVoiceStarter, *, media: str, offer_sdp: str | None = None
    ) -> LiveCall:
        """Start a call with *media* (WebRTC needs *offer_sdp*), ending the active call first.

        :class:`core.model_tasks.live.LiveStartRejected` propagates unchanged.
        """
        async with self._start_lock:
            if self._closed:
                raise LiveRegistryClosedError
            previous, self._active = self._active, None
            if previous is not None:
                await previous.replace()
            entry = _LiveCallEntry(
                limits=self._limits,
                rpc=self._rpc,
                events=self._events,
                on_finalized=self._entry_finalized,
                started_at=self._clock(),
                after_sequence=self._events.last_sequence,
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
        await asyncio.gather(*(entry.shutdown() for entry in entries))
        for handle in self._linger.values():
            handle.cancel()
        self._linger.clear()
        self._entries.clear()

    def _entry_finalized(self, entry: _LiveCallEntry) -> None:
        if self._active is entry:
            self._active = None
        if self._closed or not entry.has_undelivered_updates:
            self._forget(entry)
            return
        # Keep final updates attachable briefly for an owner that is reconnecting.
        self._linger[entry.call_id] = asyncio.get_running_loop().call_later(
            self._limits.reattach_grace_seconds, self._forget, entry
        )

    def _forget(self, entry: _LiveCallEntry) -> None:
        call_id = entry.call_id
        if self._entries.get(call_id) is entry:
            del self._entries[call_id]
        handle = self._linger.pop(call_id, None)
        if handle is not None:
            handle.cancel()
