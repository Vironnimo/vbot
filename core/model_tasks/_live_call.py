"""One running Live call: wire events, delegations, Tool calls, audio, and shutdown.

The call reads the wire's normalized events on one reader task. Every
delegation (backend model) or direct Tool call (voice model, no backend model)
runs on its own task with bounded concurrency so the conversation stays live
while work runs; results return through the wire in order of completion.
Commands to the wire are serialized so chunked appends never interleave. Relay
media flows outside that lock: microphone audio goes through a bounded backlog
and one pump task, and assistant audio goes straight to the host. The call
publishes exactly one ``closed`` update when it ends.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from core.model_tasks._live_arguments import PreparedLiveCall, prepare_live_call
from core.model_tasks._live_brain import DelegationInput, LiveBrain, record_tool_call
from core.model_tasks._live_tools import LIVE_UPDATE_PREFIX, live_failure, live_result_text
from core.model_tasks._live_wire import (
    MEDIA_RELAY,
    RELAY_BYTES_PER_MS,
    JsonObject,
    LiveWire,
    WireAudio,
    WireCaption,
    WireClosed,
    WireDelegation,
    WireEvent,
    WirePlaybackClear,
    WireProblem,
    WireSendError,
    WireStarted,
    WireToolCall,
    WireUsage,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.model_tasks.live import LiveCallHost, LiveRunNotice
    from core.model_tasks.task_execution import TaskUsage

_LOGGER = get_logger(__name__)

MAX_CONCURRENT_DELEGATIONS = 4
START_TIMEOUT_SECONDS = 45.0
CLOSE_TIMEOUT_SECONDS = 15.0
DELEGATION_TIMEOUT_SECONDS = 240.0
# Public-dialect delegations carry no text; wait until the user's speech settles.
USER_QUIET_SECONDS = 0.4
USER_QUIET_MAX_WAIT_SECONDS = 2.0
_CONVERSATION_TURNS = 12
_CONVERSATION_MAX_CHARS = 4000
_RECENT_UPDATES = 5
_ANNOUNCED_RUN_IDS = 500
_ANNOUNCEMENT_EXCERPT_CHARS = 600
_CAPTION_MAX_CHARS = 1000
_TEARDOWN_TIMEOUT_SECONDS = 5.0
_ABORT_CLOSE_TIMEOUT_SECONDS = 1.0
# Microphone audio waiting for the provider socket; older audio is dropped.
_AUDIO_BACKLOG_BYTES = 2000 * RELAY_BYTES_PER_MS
_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}


class LiveCallSession:
    """Implements :class:`core.model_tasks.live.LiveCall` for one joined wire.

    Without a *brain* (direct Tools mode) the wire emits app Tool calls
    instead of delegations.
    """

    def __init__(
        self,
        *,
        wire: LiveWire,
        brain: LiveBrain | None,
        host: LiveCallHost,
        target: str,
        start_timeout: float = START_TIMEOUT_SECONDS,
        close_timeout: float = CLOSE_TIMEOUT_SECONDS,
        delegation_timeout: float = DELEGATION_TIMEOUT_SECONDS,
        user_quiet: float = USER_QUIET_SECONDS,
        user_quiet_max_wait: float = USER_QUIET_MAX_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        usage_accounting: TaskUsage | None = None,
        usage_call_id: str = "",
    ) -> None:
        self._wire = wire
        self._brain = brain
        self._host = host
        self._target = target
        self._start_timeout = start_timeout
        self._close_timeout = close_timeout
        self._delegation_timeout = delegation_timeout
        self._user_quiet = user_quiet
        self._user_quiet_max_wait = user_quiet_max_wait
        self._clock = clock
        self._usage_accounting = usage_accounting
        self._usage_call_id = usage_call_id
        self._finishing = False
        self._phase = "connecting"
        self._closing = False
        self._abort_reason: str | None = None
        self._done = asyncio.Event()
        self._reader: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._send_lock = asyncio.Lock()
        self._delegation_slots = asyncio.Semaphore(MAX_CONCURRENT_DELEGATIONS)
        self._busy = 0
        self._turns: deque[tuple[str, str]] = deque(maxlen=_CONVERSATION_TURNS)
        self._partial: dict[str, str] = {}
        self._last_user_speech_at = 0.0
        self._updates: deque[str] = deque(maxlen=_RECENT_UPDATES)
        self._announced: deque[str] = deque(maxlen=_ANNOUNCED_RUN_IDS)
        self._usage: JsonObject | None = None
        self._started_at = clock()
        self._relay = wire.media.get("type") == MEDIA_RELAY
        self._audio_backlog: deque[bytes] = deque()
        self._audio_backlog_bytes = 0
        self._audio_ready = asyncio.Event()
        self._audio_overflow_logged = False

    @property
    def id(self) -> str:
        return self._wire.call_id

    @property
    def media(self) -> JsonObject:
        return self._wire.media

    def start(self) -> None:
        """Begin reading wire events; called once by the service."""

        self._publish({"type": "state", "phase": self._phase})
        self._reader = asyncio.create_task(self._read(), name=f"live-call-reader:{self.id}")
        self._watchdog = asyncio.create_task(
            self._watch_start(), name=f"live-call-start-watchdog:{self.id}"
        )

    async def close(self) -> None:
        if not self._done.is_set() and not self._closing:
            self._closing = True
            self._set_phase("closing")
            try:
                async with asyncio.timeout(self._close_timeout):
                    if await self._send_command(self._wire.request_close):
                        await self._done.wait()
            except TimeoutError:
                _LOGGER.warning("Live call close was not confirmed in time: call_id=%s", self.id)
        if not self._done.is_set():
            await self._teardown()
        await self.wait_closed()

    async def abort(self) -> None:
        await self._abort("aborted")

    def push_audio(self, pcm: bytes) -> None:
        if (
            not self._relay
            or not pcm
            or self._phase != "live"
            or self._closing
            or self._done.is_set()
        ):
            return
        self._audio_backlog.append(pcm)
        self._audio_backlog_bytes += len(pcm)
        while self._audio_backlog_bytes > _AUDIO_BACKLOG_BYTES and len(self._audio_backlog) > 1:
            self._audio_backlog_bytes -= len(self._audio_backlog.popleft())
            if not self._audio_overflow_logged:
                self._audio_overflow_logged = True
                _LOGGER.warning(
                    "Live call microphone audio fell behind; dropping the oldest audio: call_id=%s",
                    self.id,
                )
        self._audio_ready.set()

    def announce_run(self, notice: LiveRunNotice) -> None:
        if notice.run_id in self._announced or self._done.is_set() or self._closing:
            return
        self._announced.append(notice.run_id)
        self._updates.append(_render_notice(notice))
        if self._phase != "live":
            return
        # An excerpt is untrusted Agent output; where announcements count as
        # user input, the voice model would follow instructions quoted in it.
        text = _render_notice(notice, excerpt=not self._wire.announces_as_user_input)
        self._spawn(
            self._send_command(lambda: self._wire.announce(text)),
            name=f"live-call-announce:{self.id}",
        )

    async def wait_closed(self) -> None:
        await self._done.wait()
        tasks = [task for task in (self._reader, self._watchdog, *self._tasks) if task is not None]
        current = asyncio.current_task()
        pending = [task for task in tasks if task is not current]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _read(self) -> None:
        closed: WireClosed | None = None
        try:
            async for event in self._wire.events():
                if isinstance(event, WireClosed):
                    closed = event
                    break
                if isinstance(event, WireUsage) and self._usage_accounting is not None:
                    await self._usage_accounting.update(self._usage_call_id, event.usage)
                self._handle(event)
        except Exception as exc:
            _LOGGER.warning(
                "Live call control reader failed: call_id=%s error_type=%s",
                self.id,
                type(exc).__name__,
            )
        finally:
            await self._finish(closed)

    def _handle(self, event: WireEvent) -> None:
        if isinstance(event, WireStarted):
            if self._phase == "connecting":
                self._set_phase("live")
                _LOGGER.debug("Live call media connected: call_id=%s", self.id)
                if self._relay:
                    self._spawn(self._pump_audio(), name=f"live-call-audio:{self.id}")
        elif isinstance(event, WireAudio):
            if self._phase == "live" and not self._closing:
                self._publish_audio(event.pcm)
        elif isinstance(event, WirePlaybackClear):
            self._publish({"type": "playback_clear"})
        elif isinstance(event, WireCaption):
            self._on_caption(event)
        elif isinstance(event, WireDelegation):
            if not self._closing:
                self._spawn(self._delegate(event), name=f"live-call-delegation:{self.id}")
        elif isinstance(event, WireToolCall):
            if not self._closing:
                self._spawn(self._run_tool(event), name=f"live-call-tool:{self.id}")
        elif isinstance(event, WireUsage):
            self._usage = event.usage
        elif isinstance(event, WireProblem):
            _LOGGER.warning(
                "Live provider reported an error: call_id=%s code=%s", self.id, event.code
            )

    def _on_caption(self, event: WireCaption) -> None:
        if event.role == "user":
            self._last_user_speech_at = self._clock()
        if event.final:
            self._partial.pop(event.role, None)
            if event.text:
                self._turns.append((event.role, event.text))
        else:
            self._partial[event.role] = event.text
        self._publish(
            {
                "type": "caption",
                "role": event.role,
                "text": event.text[-_CAPTION_MAX_CHARS:],
                "final": event.final,
            }
        )

    async def _delegate(self, event: WireDelegation) -> None:
        if self._brain is None:
            _LOGGER.warning("Live delegation without a backend model: call_id=%s", self.id)
            answer = "No backend model is configured, so the request was not started."
            await self._send_command(lambda: self._wire.deliver_result(event.delegation_id, answer))
            return
        brain = self._brain
        async with self._delegation_slots:
            self._set_busy(1)
            try:
                if event.request is None:
                    await self._await_user_quiet()
                delegation = DelegationInput(
                    request=event.request,
                    conversation=self._conversation_text(),
                    updates="\n".join(self._updates),
                )
                try:
                    async with asyncio.timeout(self._delegation_timeout):
                        answer = await brain.answer(delegation)
                except TimeoutError:
                    _LOGGER.warning("Live delegation timed out: call_id=%s", self.id)
                    answer = (
                        "The request took too long and was stopped. Actions it already started "
                        "may have completed; nothing was retried."
                    )
            finally:
                self._set_busy(-1)
        await self._send_command(lambda: self._wire.deliver_result(event.delegation_id, answer))

    async def _run_tool(self, event: WireToolCall) -> None:
        async with self._delegation_slots:
            self._set_busy(1)
            started = self._clock()
            try:
                prepared = prepare_live_call(event.name, event.arguments)
                result = (
                    await self._execute_tool_call(prepared)
                    if isinstance(prepared, PreparedLiveCall)
                    else prepared
                )
            finally:
                self._set_busy(-1)
        record_tool_call(
            self._host.record,
            mode="direct",
            called=event.name,
            arguments=event.arguments,
            prepared=prepared,
            result=result,
            duration=self._clock() - started,
        )
        text = live_result_text(result)
        await self._send_command(lambda: self._wire.deliver_result(event.call_id, text))

    async def _execute_tool_call(self, call: PreparedLiveCall) -> JsonObject:
        """Run one prepared direct Tool call once; failures become an error result."""

        try:
            async with asyncio.timeout(self._delegation_timeout):
                return await self._host.execute_tool(call.name, dict(call.arguments))
        except TimeoutError:
            _LOGGER.warning("Live Tool call timed out: call_id=%s tool=%s", self.id, call.name)
            return live_failure(
                "timeout",
                "The Tool call took too long and was stopped. It may have completed; do not "
                "repeat it. Call overview to see what happened.",
            )
        except Exception as exc:
            _LOGGER.warning(
                "Live Tool call failed: call_id=%s tool=%s error_type=%s",
                self.id,
                call.name,
                type(exc).__name__,
            )
            return live_failure(
                "tool_failed",
                "The Tool call failed. It may have partly completed; do not repeat it. Call "
                "overview to see what happened.",
            )

    async def _pump_audio(self) -> None:
        """Forward microphone audio in arrival order until the call ends."""

        while True:
            await self._audio_ready.wait()
            self._audio_ready.clear()
            while self._audio_backlog:
                pcm = self._audio_backlog.popleft()
                self._audio_backlog_bytes -= len(pcm)
                if self._closing:
                    continue
                try:
                    await self._wire.send_audio(pcm)
                except WireSendError:
                    self._audio_backlog.clear()
                    self._audio_backlog_bytes = 0
                    return

    async def _await_user_quiet(self) -> None:
        deadline = self._clock() + self._user_quiet_max_wait
        while self._clock() < deadline:
            quiet_for = self._clock() - self._last_user_speech_at
            if quiet_for >= self._user_quiet:
                return
            await asyncio.sleep(min(self._user_quiet - quiet_for, deadline - self._clock()))

    def _conversation_text(self) -> str:
        lines = [f"{_ROLE_LABELS.get(role, role)}: {text}" for role, text in self._turns]
        lines.extend(
            f"{_ROLE_LABELS.get(role, role)} (still speaking): {text}"
            for role, text in self._partial.items()
            if text
        )
        return "\n".join(lines)[-_CONVERSATION_MAX_CHARS:]

    async def _watch_start(self) -> None:
        await asyncio.sleep(self._start_timeout)
        if self._phase == "connecting" and not self._done.is_set():
            _LOGGER.warning("Live call media did not connect in time: call_id=%s", self.id)
            await self._abort("start_timeout")

    async def _abort(self, reason: str) -> None:
        if not self._done.is_set():
            self._abort_reason = self._abort_reason or reason
            # Ask the provider to end the call so billing stops, without waiting.
            try:
                async with asyncio.timeout(_ABORT_CLOSE_TIMEOUT_SECONDS):
                    await self._send_command(self._wire.request_close)
            except TimeoutError:
                pass
            await self._teardown()
        await self.wait_closed()

    async def _teardown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        try:
            async with asyncio.timeout(_TEARDOWN_TIMEOUT_SECONDS):
                await self._wire.aclose()
                if self._reader is not None:
                    await asyncio.shield(self._reader)
        except (TimeoutError, asyncio.CancelledError):
            if self._reader is not None and self._reader is not asyncio.current_task():
                self._reader.cancel()
        except Exception as exc:
            _LOGGER.warning(
                "Live call teardown failed: call_id=%s error_type=%s", self.id, type(exc).__name__
            )
        await self._finish(None)

    async def _finish(self, closed: WireClosed | None) -> None:
        if self._done.is_set() or self._finishing:
            return
        self._finishing = True
        for task in list(self._tasks):
            task.cancel()
        watchdog = self._watchdog
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        reason: str | None
        if self._abort_reason is not None:
            reason = self._abort_reason
        elif closed is not None and closed.confirmed:
            reason = closed.reason
        elif self._closing:
            reason = "closed"
        else:
            reason = "connection_lost"
        usage = (closed.usage if closed is not None else None) or self._usage
        failed = reason in {"connection_lost", "start_timeout"}
        try:
            if self._usage_accounting is not None:
                await self._usage_accounting.finish(
                    self._usage_call_id,
                    usage=usage,
                    status="failed" if failed else "completed",
                )
        except Exception:
            _LOGGER.error("Live call Usage could not be saved: call_id=%s", self.id, exc_info=True)
            raise
        finally:
            self._set_phase("failed" if failed else "closed")
            self._publish({"type": "closed", "reason": reason, "usage": usage})
            _LOGGER.info(
                "Live call ended: call_id=%s target=%s reason=%s duration_s=%.1f",
                self.id,
                self._target,
                reason,
                self._clock() - self._started_at,
            )
            self._done.set()

    async def _send_command(self, send: Callable[[], Awaitable[None]]) -> bool:
        if self._done.is_set():
            return False
        try:
            async with self._send_lock:
                await send()
            return True
        except WireSendError:
            return False
        except Exception as exc:
            _LOGGER.warning(
                "Live call command failed: call_id=%s error_type=%s", self.id, type(exc).__name__
            )
            return False

    def _spawn(self, coroutine: Awaitable[Any], *, name: str) -> None:
        task = asyncio.ensure_future(coroutine)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning(
                "Live call task failed: call_id=%s error_type=%s",
                self.id,
                type(task.exception()).__name__,
            )

    def _set_busy(self, delta: int) -> None:
        was_busy = self._busy > 0
        self._busy += delta
        if (self._busy > 0) != was_busy and not self._done.is_set():
            busy = self._busy > 0
            self._publish({"type": "activity", "busy": busy, "label": "working" if busy else None})

    def _set_phase(self, phase: str) -> None:
        if self._phase == phase:
            return
        self._phase = phase
        self._publish({"type": "state", "phase": phase})

    def _publish(self, update: JsonObject) -> None:
        try:
            self._host.publish(update)
        except Exception as exc:
            _LOGGER.warning(
                "Live call update delivery failed: call_id=%s error_type=%s",
                self.id,
                type(exc).__name__,
            )

    def _publish_audio(self, pcm: bytes) -> None:
        try:
            self._host.publish_audio(pcm)
        except Exception as exc:
            _LOGGER.warning(
                "Live call audio delivery failed: call_id=%s error_type=%s",
                self.id,
                type(exc).__name__,
            )


def _render_notice(notice: LiveRunNotice, *, excerpt: bool = True) -> str:
    payload: JsonObject = {"run": notice.kind, "agent": notice.agent_id}
    if notice.session_ref:
        payload["session"] = notice.session_ref
    if excerpt:
        text = notice.excerpt.strip()
        payload["result_excerpt"] = text[:_ANNOUNCEMENT_EXCERPT_CHARS]
        payload["excerpt_truncated"] = notice.truncated or len(text) > _ANNOUNCEMENT_EXCERPT_CHARS
    return f"{LIVE_UPDATE_PREFIX}: {json.dumps(payload, ensure_ascii=False)}"
