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
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from core.model_tasks._live_brain import DelegationInput, LiveBrain
from core.model_tasks._live_results import LIVE_UPDATE_PREFIX, live_failure, live_result_text
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
from core.utils.ids import new_id
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.model_tasks.live import LiveCallHost, LiveRunNotice
    from core.model_tasks.task_execution import TaskUsage

_LOGGER = get_logger(__name__)

MAX_CONCURRENT_DELEGATIONS = 4
START_TIMEOUT_SECONDS = 45.0
CLOSE_TIMEOUT_SECONDS = 8.0
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
_TEARDOWN_TIMEOUT_SECONDS = 3.0
_ABORT_CLOSE_TIMEOUT_SECONDS = 1.0
# A provider close this close to the session limit is the limit (``expired``),
# also when the provider reports it with its own reason or none at all.
_EXPIRY_TOLERANCE_SECONDS = 60.0
_EXPIRED_REASONS = frozenset({"max_duration"})
# Run notices that arrive before the call is live; spoken once it is.
_PENDING_NOTICES = 5
# Agent-facing: the voice model reads these when vBot could not answer.
_DELEGATION_FAILED = (
    "vBot could not process this request because of an internal error. Actions it already "
    "started may have completed; nothing was retried."
)
_TOOL_FAILED = (
    "The Tool call failed because of an internal error. It may have partly completed; do not "
    "repeat it. Look up what happened before you try anything else."
)
_TOOL_TIMED_OUT = (
    "The Tool call took too long and was stopped. It may have completed; do not repeat it. "
    "Look up what happened before you try anything else."
)
# Microphone audio waiting for the provider socket; older audio is dropped.
_AUDIO_BACKLOG_BYTES = 2000 * RELAY_BYTES_PER_MS
_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}


class LiveCallSession:
    """Implements :class:`core.model_tasks.live.LiveCall` for one joined wire.

    Without a *brain* (direct Tools mode) the wire emits app Tool calls
    instead of delegations. ``id`` is the wire's call id, which a Provider may
    own; logs and task names use the vBot-owned *log_id* instead.
    """

    def __init__(
        self,
        *,
        wire: LiveWire,
        brain: LiveBrain | None,
        host: LiveCallHost,
        target: str,
        log_id: str | None = None,
        start_timeout: float = START_TIMEOUT_SECONDS,
        close_timeout: float = CLOSE_TIMEOUT_SECONDS,
        delegation_timeout: float = DELEGATION_TIMEOUT_SECONDS,
        user_quiet: float = USER_QUIET_SECONDS,
        user_quiet_max_wait: float = USER_QUIET_MAX_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        usage_accounting: TaskUsage | None = None,
        usage_call_id: str = "",
    ) -> None:
        self._wire = wire
        self._brain = brain
        self._host = host
        self._target = target
        self._log_id = log_id or new_id("live")
        self._start_timeout = start_timeout
        self._close_timeout = close_timeout
        self._delegation_timeout = delegation_timeout
        self._user_quiet = user_quiet
        self._user_quiet_max_wait = user_quiet_max_wait
        self._clock = clock
        self._wall_clock = wall_clock
        # When the provider ends the session at its limit, on ``clock``.
        self._expires_at: float | None = None
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
        # Delegations run one at a time, so each one starts from the state the
        # previous one left and two never act on the same things at once.
        self._delegation_turn = asyncio.Lock()
        self._busy = 0
        self._turns: deque[tuple[str, str]] = deque(maxlen=_CONVERSATION_TURNS)
        self._partial: dict[str, str] = {}
        self._last_user_speech_at = 0.0
        self._updates: deque[str] = deque(maxlen=_RECENT_UPDATES)
        self._announced: deque[str] = deque(maxlen=_ANNOUNCED_RUN_IDS)
        self._usage: JsonObject | None = None
        self._usage_dirty = False
        self._usage_writer: asyncio.Task[None] | None = None
        self._usage_failure_logged = False
        self._unspoken: deque[LiveRunNotice] = deque(maxlen=_PENDING_NOTICES)
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
    def log_id(self) -> str:
        return self._log_id

    @property
    def media(self) -> JsonObject:
        return self._wire.media

    def start(self) -> None:
        """Begin reading wire events; called once by the service."""

        self._publish({"type": "state", "phase": self._phase})
        self._reader = asyncio.create_task(self._read(), name=f"live-call-reader:{self._log_id}")
        self._watchdog = asyncio.create_task(
            self._watch_start(), name=f"live-call-start-watchdog:{self._log_id}"
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
                _LOGGER.warning("Live call close was not confirmed in time (call=%s)", self._log_id)
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
                    "Live call microphone audio fell behind; dropping the oldest audio (call=%s)",
                    self._log_id,
                )
        self._audio_ready.set()

    def announce_run(self, notice: LiveRunNotice) -> None:
        if notice.run_id in self._announced or self._done.is_set() or self._closing:
            return
        self._announced.append(notice.run_id)
        self._updates.append(_render_notice(notice))
        if self._phase == "live":
            self._speak(notice)
        else:
            self._unspoken.append(notice)

    def _speak(self, notice: LiveRunNotice) -> None:
        # An excerpt is untrusted Agent output; where announcements count as
        # user input, the voice model would follow instructions quoted in it.
        text = _render_notice(notice, excerpt=not self._wire.announces_as_user_input)
        self._spawn(
            self._send_command(lambda: self._wire.announce(text)),
            name=f"live-call-announce:{self._log_id}",
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
                self._handle(event)
        except Exception as exc:
            _LOGGER.warning(
                "Live call control reader failed (call=%s error_type=%s)",
                self._log_id,
                type(exc).__name__,
            )
        finally:
            await self._finish(closed)

    def _handle(self, event: WireEvent) -> None:
        if isinstance(event, WireStarted):
            if self._phase == "connecting":
                self._set_phase("live")
                _LOGGER.debug("Live call media connected (call=%s)", self._log_id)
                if event.expires_at is not None:
                    seconds = max(0.0, event.expires_at - self._wall_clock())
                    self._expires_at = self._clock() + seconds
                    self._publish({"type": "expiry", "seconds": round(seconds, 1)})
                if self._relay:
                    self._spawn(self._pump_audio(), name=f"live-call-audio:{self._log_id}")
                while self._unspoken:
                    self._speak(self._unspoken.popleft())
        elif isinstance(event, WireAudio):
            if self._phase == "live" and not self._closing:
                self._publish_audio(event.pcm)
        elif isinstance(event, WirePlaybackClear):
            self._publish({"type": "playback_clear"})
        elif isinstance(event, WireCaption):
            self._on_caption(event)
        elif isinstance(event, WireDelegation):
            if not self._closing:
                self._spawn(self._delegate(event), name=f"live-call-delegation:{self._log_id}")
        elif isinstance(event, WireToolCall):
            if not self._closing:
                self._spawn(self._run_tool(event), name=f"live-call-tool:{self._log_id}")
        elif isinstance(event, WireUsage):
            self._usage = event.usage
            self._save_usage()
        elif isinstance(event, WireProblem):
            _LOGGER.warning(
                "Live provider reported an error (call=%s code=%s)", self._log_id, event.code
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
        """Answer one delegation; the voice model always gets a result."""
        try:
            answer = await self._delegation_answer(event)
        except Exception as exc:
            _LOGGER.warning(
                "Live delegation failed unexpectedly (call=%s error_type=%s)",
                self._log_id,
                type(exc).__name__,
            )
            answer = _DELEGATION_FAILED
        await self._send_command(lambda: self._wire.deliver_result(event.delegation_id, answer))

    async def _delegation_answer(self, event: WireDelegation) -> str:
        if self._brain is None:
            _LOGGER.warning("Live delegation without a backend model (call=%s)", self._log_id)
            return "No backend model is configured, so the request was not started."
        brain = self._brain
        self._set_busy(1)
        try:
            async with self._delegation_turn:
                if event.request is None:
                    await self._await_user_quiet()
                delegation = DelegationInput(
                    request=event.request,
                    conversation=self._conversation_text(),
                    updates="\n".join(self._updates),
                    refs=self._host.known_refs(),
                    state=await self._current_state(),
                )
                try:
                    async with asyncio.timeout(self._delegation_timeout):
                        answer = await brain.answer(delegation)
                except TimeoutError:
                    _LOGGER.warning("Live delegation timed out (call=%s)", self._log_id)
                    answer = (
                        "The request took too long and was stopped. Actions it already started "
                        "may have completed; nothing was retried."
                    )
        finally:
            self._set_busy(-1)
        return answer

    async def _current_state(self) -> str:
        """What vBot shows now, for a delegation; empty when the host cannot tell."""
        try:
            return await self._host.current_state()
        except Exception as exc:
            _LOGGER.warning(
                "Live state for a delegation failed (call=%s error_type=%s)",
                self._log_id,
                type(exc).__name__,
            )
            return ""

    async def _run_tool(self, event: WireToolCall) -> None:
        """Run one direct Tool call; the voice model always gets a result."""
        try:
            text = await self._tool_result_text(event)
        except Exception as exc:
            _LOGGER.warning(
                "Live Tool call failed unexpectedly (call=%s error_type=%s)",
                self._log_id,
                type(exc).__name__,
            )
            text = live_result_text(live_failure("tool_failed", _TOOL_FAILED))
        await self._send_command(lambda: self._wire.deliver_result(event.call_id, text))

    async def _tool_result_text(self, event: WireToolCall) -> str:
        """Run one direct Tool call once through the host; a timeout stops it."""

        async with self._delegation_slots:
            self._set_busy(1)
            try:
                async with asyncio.timeout(self._delegation_timeout):
                    run = await self._host.run_tool(event.name, event.arguments)
            except TimeoutError:
                _LOGGER.warning("Live Tool call timed out (call=%s)", self._log_id)
                return live_result_text(live_failure("timeout", _TOOL_TIMED_OUT))
            finally:
                self._set_busy(-1)
        return live_result_text(run.result)

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
            # The ended line reports the start timeout; this is detail.
            _LOGGER.debug("Live call media did not connect in time (call=%s)", self._log_id)
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
        cancelled = False
        try:
            async with asyncio.timeout(_TEARDOWN_TIMEOUT_SECONDS):
                await self._wire.aclose()
                if self._reader is not None:
                    await asyncio.shield(self._reader)
        except TimeoutError, asyncio.CancelledError:
            if self._reader is not None and self._reader is not asyncio.current_task():
                self._reader.cancel()
            # Only a cancellation of this task propagates, not the shielded reader's.
            current = asyncio.current_task()
            cancelled = current is not None and current.cancelling() > 0
        except Exception as exc:
            _LOGGER.warning(
                "Live call teardown failed (call=%s error_type=%s)",
                self._log_id,
                type(exc).__name__,
            )
        # A cancelled closer still ends the call and publishes its final state.
        await self._finish(None)
        if cancelled:
            raise asyncio.CancelledError

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
        elif not self._closing and self._expired(closed):
            reason = "expired"
        elif closed is not None and closed.confirmed:
            reason = closed.reason or "closed"
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
            _LOGGER.error(
                "Live call Usage could not be saved (call=%s)", self._log_id, exc_info=True
            )
            raise
        finally:
            # A terminal Provider event need not close its transport. Settle
            # Usage first so cancellation during socket cleanup cannot lose it.
            try:
                await self._close_brain()
                async with asyncio.timeout(_TEARDOWN_TIMEOUT_SECONDS):
                    await self._wire.aclose()
            except Exception as exc:
                _LOGGER.warning(
                    "Live call transport cleanup failed (call=%s error_type=%s)",
                    self._log_id,
                    type(exc).__name__,
                )
            finally:
                # An owner abort can arrive while transport cleanup is waiting.
                reason = self._abort_reason or reason
                failed = reason in {"connection_lost", "start_timeout"}
                self._set_phase("failed" if failed else "closed")
                self._publish({"type": "closed", "reason": reason, "usage": usage})
                # A lost connection or a start timeout is a failed call.
                _LOGGER.log(
                    logging.WARNING if failed else logging.INFO,
                    "Live call ended (call=%s target=%s reason=%s duration_s=%.1f)",
                    self._log_id,
                    self._target,
                    reason,
                    self._clock() - self._started_at,
                )
                self._done.set()

    def _expired(self, closed: WireClosed | None) -> bool:
        """Whether the provider ended the call at its session limit."""
        if closed is not None and closed.reason in _EXPIRED_REASONS:
            return True
        return (
            self._expires_at is not None
            and self._clock() >= self._expires_at - _EXPIRY_TOLERANCE_SECONDS
        )

    async def _close_brain(self) -> None:
        """Release the backend's connection; a failure cannot keep the call open."""
        if self._brain is None:
            return
        try:
            async with asyncio.timeout(_TEARDOWN_TIMEOUT_SECONDS):
                await self._brain.aclose()
        except Exception as exc:
            _LOGGER.warning(
                "Live call backend cleanup failed (call=%s error_type=%s)",
                self._log_id,
                type(exc).__name__,
            )

    def _save_usage(self) -> None:
        """Save the latest cumulative Usage in the background; the reader never waits."""
        if self._usage_accounting is None:
            return
        self._usage_dirty = True
        if self._usage_writer is None or self._usage_writer.done():
            self._usage_writer = asyncio.ensure_future(self._write_usage())
            self._usage_writer.set_name(f"live-call-usage:{self._log_id}")
            self._tasks.add(self._usage_writer)
            self._usage_writer.add_done_callback(self._tasks.discard)

    async def _write_usage(self) -> None:
        accounting = self._usage_accounting
        assert accounting is not None
        while self._usage_dirty:
            self._usage_dirty = False
            try:
                await accounting.update(self._usage_call_id, self._usage)
            except Exception as exc:
                # The close settles Usage again; a failed update never ends the call.
                if not self._usage_failure_logged:
                    self._usage_failure_logged = True
                    _LOGGER.warning(
                        "Live call Usage update failed (call=%s error_type=%s)",
                        self._log_id,
                        type(exc).__name__,
                    )

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
                "Live call command failed (call=%s error_type=%s)",
                self._log_id,
                type(exc).__name__,
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
                "Live call task failed (call=%s error_type=%s)",
                self._log_id,
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
                "Live call update delivery failed (call=%s error_type=%s)",
                self._log_id,
                type(exc).__name__,
            )

    def _publish_audio(self, pcm: bytes) -> None:
        try:
            self._host.publish_audio(pcm)
        except Exception as exc:
            _LOGGER.warning(
                "Live call audio delivery failed (call=%s error_type=%s)",
                self._log_id,
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
