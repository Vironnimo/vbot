"""The server side of one Live call: the host the call runs on.

A :class:`LiveCallEntry` is the call's :class:`core.model_tasks.live.LiveCallHost`:
it runs the call's Live Tools, feeds it finished Runs, keeps the
owner socket's updates, answers UI requests through the owner, and ends the
call when its owner does not attach or return, when the voice model hangs up,
or when nobody uses it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections import deque
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.model_tasks.live import LiveCall, PlaybackReceipt
from core.utils.ids import new_id
from server.events import ServerEventBus
from server.live._context import UI_BUSY, UI_TIMEOUT, UI_UNAVAILABLE, LiveUiError, RpcInvoker
from server.live._feed import LiveRunFeed
from server.live._tools import LiveToolExecutor
from server.live.owner import (
    LIVE_AUDIO_FRAME_MAX_BYTES,
    LIVE_CONTEXT_FRAME_MAX_CHARS,
    LIVE_SOCKET_CLOSE_ENDED,
    LIVE_SOCKET_CLOSE_LAGGED,
    LIVE_SOCKET_CLOSE_REPLACED,
    LiveOwnerStream,
)

JsonObject = dict[str, Any]

_LOGGER = logging.getLogger("vbot.server.live")

CLOSED_REASON_REPLACED = "replaced"
# Why vBot ended a call: the voice model hung up, or nobody used the call.
CLOSED_REASON_HUNG_UP = "hung_up"
CLOSED_REASON_IDLE = "idle"
_NOTIFICATION_FAILED = "notification_failed"

_DISPOSABLE_UPDATES = frozenset({"caption", "heartbeat", "action"})
_SNAPSHOT_UPDATES = frozenset(
    {"state", "sessions", "connection", "activity", "idle", "expiry", "playback_clear"}
)
# Updates that show someone speaks; they keep a call from idling.
_SPEECH_UPDATES = frozenset({"caption", "playback_clear"})


@dataclass(frozen=True)
class LiveCallLimits:
    """Timeouts and bounds of Live call ownership."""

    attach_timeout_seconds: float = 15.0
    reattach_grace_seconds: float = 10.0
    ui_request_timeout_seconds: float = 20.0
    update_buffer_limit: int = 200
    owner_queue_limit: int = 1_000
    # One minute of PCM16 mono 24 kHz audio.
    owner_audio_limit_bytes: int = 60 * 48_000
    shutdown_close_timeout_seconds: float = 3.0
    abort_timeout_seconds: float = 4.0
    # A live call without speech, Tool calls, or delegated work ends after
    # this long; ``idle_warning_seconds`` before, the owner is warned.
    idle_seconds: float = 600.0
    idle_warning_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.update_buffer_limit < 2:
            raise ValueError("update_buffer_limit must hold a UI request and the final update")
        if self.owner_queue_limit < self.update_buffer_limit:
            raise ValueError("owner_queue_limit must hold the whole update buffer")
        if not 0 < self.idle_warning_seconds < self.idle_seconds:
            raise ValueError("idle_warning_seconds must fall within idle_seconds")


@dataclass(frozen=True)
class _PendingUiRequest:
    frame: JsonObject
    future: asyncio.Future[JsonObject]


class LiveCallEntry:
    """The server side of one call: its host, owner socket, UI requests and feed."""

    def __init__(
        self,
        *,
        limits: LiveCallLimits,
        rpc: RpcInvoker,
        events: ServerEventBus,
        on_finalized: Callable[[LiveCallEntry], None],
        started_at: datetime,
        after_sequence: int,
        wake_phrases: tuple[str, ...] = (),
    ) -> None:
        self._limits = limits
        self._wake_phrases = wake_phrases
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
        self._closed_update: JsonObject | None = None
        self._finalized = False
        self._buffer: deque[JsonObject] = deque()
        self._owner: LiveOwnerStream | None = None
        self._owner_attached = False
        self._malformed_audio_logged = False
        self._app_context: JsonObject | None = None
        self._ui_requests: dict[str, _PendingUiRequest] = {}
        self._executor = LiveToolExecutor(
            rpc=rpc,
            ui=self.ui_request,
            app_context=lambda: self._app_context,
            is_active=self._is_active,
            started_at=started_at,
            end_call=self.end_soon,
            report=self.publish,
        )
        self._ending = False
        # Why vBot ended the call, when it did (hung up, idle).
        self._end_reason: str | None = None
        self._idle_watch: asyncio.Task[None] | None = None
        self._active_at = 0.0
        self._busy = False
        self._idle_warned = False
        self._feed: LiveRunFeed | None = None
        self._timer: asyncio.Task[None] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def call_id(self) -> str:
        return self.call.id if self.call is not None else ""

    @property
    def log_id(self) -> str:
        """The call's vBot-owned log id; ``call_id`` may be the Provider's."""
        return self.call.log_id if self.call is not None else ""

    @property
    def can_attach(self) -> bool:
        return (
            self._closed_update is None
            or bool(self._buffer)
            or (self._owner is not None and not self._owner.finished)
        )

    def _is_active(self) -> bool:
        return self.active

    # -- LiveCallHost -----------------------------------------------------

    @property
    def wake_phrases(self) -> tuple[str, ...]:
        """The phrases that address other vBot Agents during the call."""
        return self._wake_phrases

    async def run_live_tool(self, name: str, arguments: Any) -> JsonObject:
        """Run one Live Tool call as a Model wrote it; the executor runs them in turn."""
        self._mark_active()
        try:
            return await self._executor.run(name, arguments)
        finally:
            self._mark_active()

    def known_refs(self) -> str:
        """The refs Tool results named so far, one labeled line each."""
        return self._executor.known_refs()

    async def current_state(self) -> str:
        """The full overview right now, for a delegation's input."""
        return await self._executor.current_state()

    def publish(self, update: JsonObject) -> None:
        """Deliver one call update to the owner, or buffer it until one attaches."""
        if self._closed_update is not None:
            return
        kind = update.get("type")
        if kind == "closed":
            self.active = False
            self._cancel_timer()
            self._stop_idle_watch()
            for request in self._ui_requests.values():
                self._discard_ui_request(request.frame)
            if self._end_reason is not None and update.get("reason") != CLOSED_REASON_REPLACED:
                update = {**update, "reason": self._end_reason}
            self._closed_update = update
        elif kind == "state" and update.get("phase") == "live":
            self._start_idle_watch()
        elif kind == "activity":
            self._busy = update.get("busy") is True
            self._mark_active()
        elif kind in _SPEECH_UPDATES:
            self._mark_active()
        self._deliver(update)
        if self._closed_update is not None and self._owner is not None:
            self._owner.end(LIVE_SOCKET_CLOSE_ENDED)

    def publish_audio(self, pcm: bytes, *, generation: int = 1, start_samples: int = 0) -> None:
        """Send assistant audio to the attached owner; dropped while none is attached."""
        owner = self._owner
        if owner is None or self._closed_update is not None or not pcm:
            return
        if not owner.send(struct.pack("<II", generation, start_samples) + pcm):
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
        self._watcher = self._spawn(self._watch(call), "watch")
        self._arm_timer(self._limits.attach_timeout_seconds, "did not attach")

    def request_close(self) -> bool:
        """Close gracefully in the background; ``False`` once the call ended."""
        call = self.call
        if self.ended or call is None:
            return False
        if not self._closing:
            self._closing = True
            self.active = False
            self._spawn(self._close(call), "close")
        return True

    def end_soon(self) -> None:
        """Close gracefully right away, once the Tool call has returned."""
        if self._ending or self._closing or self.ended:
            return
        self._ending = True
        self._spawn(self._end(), "end")

    async def _end(self) -> None:
        _LOGGER.info("Live call ended by the voice model (call=%s)", self.log_id)
        self._end_reason = self._end_reason or CLOSED_REASON_HUNG_UP
        self.request_close()

    # -- idle hangup ----------------------------------------------------------

    def _mark_active(self) -> None:
        """Speech or work happened: the idle period starts over."""
        self._active_at = time.monotonic()
        if self._idle_warned and self._closed_update is None:
            self._idle_warned = False
            self._deliver({"type": "idle", "ends_in": None})

    def _start_idle_watch(self) -> None:
        if self._idle_watch is None and not self._closing and not self.ended:
            self._mark_active()
            self._idle_watch = self._spawn(self._watch_idle(), "idle-watch")

    def _stop_idle_watch(self) -> None:
        watch, self._idle_watch = self._idle_watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()

    async def _watch_idle(self) -> None:
        """End a live call nobody uses; warn the owner shortly before."""
        limits = self._limits
        while not self._closing and not self.ended:
            if self._busy:
                self._mark_active()
            idle_for = time.monotonic() - self._active_at
            warn_after = limits.idle_seconds - limits.idle_warning_seconds
            if idle_for < warn_after:
                await asyncio.sleep(warn_after - idle_for)
            elif idle_for < limits.idle_seconds:
                if not self._idle_warned:
                    self._idle_warned = True
                    ends_in = round(limits.idle_seconds - idle_for, 1)
                    self._deliver({"type": "idle", "ends_in": ends_in})
                await asyncio.sleep(limits.idle_seconds - idle_for)
            else:
                _LOGGER.info("Live call ended after idling (call=%s)", self.log_id)
                self._idle_watch = None
                self._end_reason = self._end_reason or CLOSED_REASON_IDLE
                self.request_close()
                return

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
                _LOGGER.warning("Live call did not close during shutdown (call=%s)", call.log_id)
                await self._abort()
            except Exception:
                _LOGGER.exception("Live call close failed during shutdown (call=%s)", call.log_id)
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
            _LOGGER.exception("Live call close failed; aborting (call=%s)", call.log_id)
            await self._abort()

    async def _abort(self) -> None:
        call = self.call
        if call is None:
            return
        try:
            await asyncio.wait_for(call.abort(), self._limits.abort_timeout_seconds)
        except TimeoutError:
            _LOGGER.warning("Live call abort timed out (call=%s)", call.log_id)
        except Exception:
            _LOGGER.exception("Live call abort failed (call=%s)", call.log_id)

    async def _watch(self, call: LiveCall) -> None:
        try:
            await call.wait_closed()
        except Exception:
            _LOGGER.exception("Live call ended with an error (call=%s)", call.log_id)
        await self._finalize()

    async def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        self.ended = True
        self.active = False
        self._cancel_timer()
        for request in self._ui_requests.values():
            if not request.future.done():
                request.future.set_exception(LiveUiError(UI_UNAVAILABLE))
        if self._closed_update is None:
            self.publish({"type": "closed", "reason": None, "usage": None})
        # Ending, the final update and the registry's release happen in one step:
        # an owner socket attaching meanwhile would otherwise find an ended call
        # whose ``closed`` update was not published yet.
        self._on_finalized(self)
        if self._feed is not None:
            await self._feed.aclose()

    def _report_notification_failure(self) -> None:
        self.publish({"type": "error", "code": _NOTIFICATION_FAILED, "fatal": False})

    # -- owner socket -----------------------------------------------------

    def attach(self) -> LiveOwnerStream:
        """Make a new socket the owner; a previous owner socket is replaced."""
        previous = self._owner
        if previous is not None:
            previous.end(LIVE_SOCKET_CLOSE_REPLACED)
            self._requeue_owner(previous)
            if self.call is not None and self._closed_update is None:
                self.call.reset_playback()
        owner = LiveOwnerStream(
            self, self._limits.owner_queue_limit, self._limits.owner_audio_limit_bytes
        )
        call = self.call if self._closed_update is None else None
        capacity = self._limits.update_buffer_limit
        if call is not None and call.media["type"] == "relay":
            # The new socket also needs the current playback generation.
            capacity = min(capacity, self._limits.owner_queue_limit - 1)
        self._compact_buffer(capacity)
        while self._buffer:
            owner.send(self._buffer.popleft())
        self._owner = owner
        self._owner_attached = True
        self._cancel_timer()
        if call is not None:
            call.sync_playback()
        if self._closed_update is not None:
            owner.end(LIVE_SOCKET_CLOSE_ENDED)
        return owner

    def detach(self, owner: LiveOwnerStream) -> None:
        """Forget a closed owner socket and allow a bounded reconnect.

        Updates it had not taken wait for the next owner socket.
        """
        if self._owner is not owner:
            return
        self._owner = None
        self._requeue_owner(owner)
        if self.ended:
            self._on_finalized(self)
            return
        if self.call is not None:
            self.call.reset_playback()
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
                    "Live owner sent a malformed audio frame; dropping it (call=%s bytes=%d)",
                    self.log_id,
                    len(pcm),
                )
            return
        call.push_audio(pcm)

    def receive_text(self, owner: LiveOwnerStream, text: str) -> None:
        """Keep what the current owner reports the app shows; ``stay`` keeps an idle call."""
        if owner is not self._owner or self.ended or len(text) > LIVE_CONTEXT_FRAME_MAX_CHARS:
            return
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        if frame.get("type") == "context":
            self._app_context = _app_context(frame)
        elif frame.get("type") == "stay":
            self._mark_active()
        elif frame.get("type") == "playback":
            receipt = PlaybackReceipt.from_owner_frame(frame)
            if receipt is not None and self.call is not None:
                self.call.report_playback(receipt)

    def _deliver(self, frame: JsonObject) -> None:
        owner = self._owner
        if owner is not None:
            if owner.send(frame):
                return
            self._owner_lagged(owner)
        self._buffer.append(frame)
        self._trim_buffer()

    def _requeue(self, updates: list[JsonObject]) -> None:
        """Put updates a socket did not take before those buffered since."""
        if updates:
            self._buffer.extendleft(reversed(updates))
            self._trim_buffer()

    def _requeue_owner(self, owner: LiveOwnerStream) -> None:
        updates = owner.take_undelivered()
        terminal = self._closed_update
        if terminal is not None and not owner.finished and terminal not in updates:
            # The stream may have taken the final frame before its send failed.
            updates.append(terminal)
        self._requeue(updates)

    def _trim_buffer(self) -> None:
        if len(self._buffer) > self._limits.update_buffer_limit:
            self._compact_buffer(self._limits.update_buffer_limit)

    def _compact_buffer(self, capacity: int) -> None:
        """Keep recent updates in their original order, reserving reliable delivery."""
        ranked: list[tuple[int, int]] = []
        seen: set[tuple[str, str]] = set()
        for index, update in reversed(list(enumerate(self._buffer))):
            kind = update["type"]
            key = None
            if kind in {"ui_request", "closed"}:
                priority = 3
            elif kind in _SNAPSHOT_UPDATES:
                priority = 2
                key = (kind, "")
            elif kind == "error" and update.get("fatal") is not True:
                priority = 1
                key = (kind, str(update.get("code")))
            else:
                priority = 0 if kind in _DISPOSABLE_UPDATES else 2
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            ranked.append((priority, index))
        keep = {index for _, index in sorted(ranked, reverse=True)[:capacity]}
        self._buffer = deque(update for index, update in enumerate(self._buffer) if index in keep)

    def _discard_ui_request(self, request: JsonObject) -> None:
        if self._owner is not None:
            self._owner.discard_update(request)
        with suppress(ValueError):
            self._buffer.remove(request)

    def _owner_lagged(self, owner: LiveOwnerStream) -> None:
        _LOGGER.warning("Live owner socket fell behind (call=%s)", self.log_id)
        self._owner = None
        owner.end(LIVE_SOCKET_CLOSE_LAGGED)
        self._requeue(owner.take_undelivered())
        if self.call is not None:
            self.call.reset_playback()
        self._arm_timer(self._limits.reattach_grace_seconds, "did not return")

    def _arm_timer(self, seconds: float, reason: str) -> None:
        self._cancel_timer()
        if self.call is None or self.ended or self._closed_update is not None:
            return
        self._timer = self._spawn(self._expire(seconds, reason), "timer")

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    async def _expire(self, seconds: float, reason: str) -> None:
        await asyncio.sleep(seconds)
        # From here on an attaching socket no longer cancels this abort.
        self._timer = None
        _LOGGER.warning("Live call owner %s; ending call (call=%s)", reason, self.log_id)
        self.active = False
        await self._abort()

    # -- UI requests ------------------------------------------------------

    async def ui_request(self, action: str, args: JsonObject) -> JsonObject:
        """Ask the owning accessor to read or change its display.

        While the owner reconnects, the request waits for it within the UI
        request timeout; before any owner attached, there is nobody to ask.
        """
        if self.ended or self._closed_update is not None or not self._owner_attached:
            raise LiveUiError(UI_UNAVAILABLE)
        if len(self._ui_requests) >= self._limits.update_buffer_limit - 1:
            raise LiveUiError(UI_BUSY)
        request_id = new_id("ui", claim=lambda candidate: candidate not in self._ui_requests)
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        request = {"type": "ui_request", "request_id": request_id, "action": action, "args": args}
        self._ui_requests[request_id] = _PendingUiRequest(request, future)
        try:
            self._deliver(request)
            return await asyncio.wait_for(future, self._limits.ui_request_timeout_seconds)
        except TimeoutError as exc:
            raise LiveUiError(UI_TIMEOUT, uncertain=True) from exc
        finally:
            self._ui_requests.pop(request_id, None)
            self._discard_ui_request(request)

    def resolve_ui_request(
        self, request_id: str, *, result: JsonObject | None, error: str | None
    ) -> bool:
        """Complete a pending UI request; ``False`` when it is unknown or already done."""
        request = self._ui_requests.get(request_id)
        if request is None or request.future.done():
            return False
        if error is not None:
            request.future.set_exception(LiveUiError(error))
        else:
            request.future.set_result(result if result is not None else {})
        return True

    def _spawn(self, coroutine: Coroutine[Any, Any, None], purpose: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=f"live-call:{self.log_id}:{purpose}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


def _app_context(frame: JsonObject) -> JsonObject:
    """The known fields of a context report, as text or ``None``."""

    def text(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    session = frame.get("chat_session")
    chat_session = (
        {"agent_id": session["agent_id"], "session_id": session["session_id"]}
        if isinstance(session, dict)
        and text(session.get("agent_id"))
        and text(session.get("session_id"))
        else None
    )
    return {
        "view": text(frame.get("view")),
        "selected_agent_id": text(frame.get("selected_agent_id")),
        "selected_project_id": text(frame.get("selected_project_id")),
        "chat_session": chat_session,
    }
