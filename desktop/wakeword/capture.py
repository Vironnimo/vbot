"""Microphone capture for Voice: one owner of the input stream, fanned out to subscribers.

:class:`AudioCapture` owns the microphone stream on one daemon thread
(``vbot-voice-capture``). Every blocking read goes through one chain::

    native read -> mono int16 -> echo stage (native rate in, stage.rate out)
    -> stateful soxr projection of the stage output to 16 kHz -> AudioBlock

Each :class:`AudioBlock` carries both projections: ``pcm16`` (16 kHz mono, for
detection, the speech gate and endpointing) and ``recording`` (the echo stage
output at ``recording_rate``, for the command WAV). Blocks are numbered
continuously; consumers read them through a :class:`CaptureSubscription`, a
bounded queue that never blocks the capture thread. A short history lets a
new subscription start right after a block another consumer already saw, so
the command recorder starts at the detection pre-roll without losing audio.

Failures: an overflowing read delivers a :class:`CaptureGap` to every
subscription and keeps reading; a failed read also reopens the stream. Three
consecutive failed reads, or a reopen that fails, disconnect the microphone:
the status reports ``disconnected`` and the thread retries every
``reconnect_interval`` seconds, refreshing the PortAudio device list first
while no Voice stream is open. A microphone that cannot open at start is
refreshed and retried once before it counts as disconnected.

The echo stage (:class:`EchoStage`) comes from an :class:`EchoStagePool`
when echo cancellation is enabled: the capture thread borrows it before it
opens the microphone and returns it when it ends, so the slow creation of the
echo canceller happens once per process. A disabled setting uses a
pass-through stage in state ``off``; no pool, an unavailable library or a
failing stage fall back to a pass-through stage in state ``unavailable`` for
the rest of the capture. Only the capture thread calls the stage, except for
reading ``state``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from desktop.wakeword._microphones import (
    CaptureFormat,
    close_input_stream,
    open_input_stream,
    refresh_microphone_devices,
)
from desktop.wakeword.config import MicrophoneSelection

logger = logging.getLogger("vbot.desktop.wakeword.capture")

DETECTION_SAMPLE_RATE = 16000
"""Rate of every block's ``pcm16`` projection."""

BLOCK_SECONDS = 0.04
"""Native audio read per blocking call."""

HISTORY_SECONDS = 3.0
"""Recent blocks kept for subscriptions that start at an earlier block."""

RECONNECT_INTERVAL_SECONDS = 30.0
"""Wait between attempts to reopen a disconnected microphone."""

_MAX_CONSECUTIVE_READ_FAILURES = 3

CAPTURE_OPENING = "opening"
CAPTURE_CAPTURING = "capturing"
CAPTURE_DISCONNECTED = "disconnected"
CAPTURE_STOPPED = "stopped"
CAPTURE_FAILED = "failed"

ECHO_OFF = "off"
ECHO_ACTIVE = "active"
ECHO_NO_REFERENCE = "no_reference"
ECHO_UNAVAILABLE = "unavailable"

ERROR_MICROPHONE_UNAVAILABLE = "microphone_unavailable"
ERROR_MICROPHONE_READ_FAILED = "microphone_read_failed"
ERROR_PIPELINE_FAILED = "pipeline_failed"

GAP_OVERFLOW = "overflow"
"""The device delivered audio faster than it was read; samples were lost."""
GAP_READ_FAILED = "read_failed"
"""A read failed; the stream was reopened or the microphone disconnected."""
GAP_ECHO_FAILED = "echo_failed"
"""The echo stage failed and was replaced by a pass-through stage."""
GAP_OVERRUN = "overrun"
"""This subscription fell behind; its oldest blocks were dropped."""
GAP_HISTORY = "history"
"""A subscription could not start right after the requested block."""


class EchoStage(Protocol):
    """Echo cancellation between the microphone read and every consumer.

    Only the capture thread calls the methods; ``state`` may be read from any
    thread.

    - ``open(capture_rate)``: called after the microphone stream opened; may
      open its own reference stream. Never raises for a missing reference,
      ``state`` reports it.
    - ``rate``: output (recording) rate after ``open``.
    - ``process(mic, arrival)``: mono int16 samples at the capture rate in,
      mono int16 at ``rate`` out. ``arrival`` is the ``time.perf_counter()``
      time at which the block's last sample was available. The output length
      varies: the stage may hold audio back while it waits for its reference
      and release it later (possibly as an empty array).
    - ``flush()``: release held-back audio (on a gap and on stop).
    - ``reset()``: forget held-back audio and timing after a capture gap.
    - ``state``: ``off`` | ``active`` | ``no_reference`` | ``unavailable``.
    - ``close()``: close the reference; called before a PortAudio refresh and
      on stop. ``open`` follows when the microphone reopens.
    """

    rate: int
    state: str

    def open(self, capture_rate: int) -> None: ...

    def process(self, mic: np.ndarray, arrival: float) -> np.ndarray: ...

    def flush(self) -> np.ndarray: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


EchoStageFactory = Callable[[], EchoStage | None]
"""Creates an echo stage; ``None`` means the echo canceller is unavailable."""


class EchoStagePool:
    """Creates echo stages on demand and keeps a returned one for the next capture.

    Creating the real stage loads a native library (up to seconds), so every
    capture after the first reuses the stage the previous one returned. A
    stage is lent to one capture at a time: while an abandoned capture thread
    still holds it, the next capture gets a new one. A factory that returns
    ``None`` or raises is not asked again.
    """

    def __init__(self, factory: EchoStageFactory) -> None:
        self._factory = factory
        self._create_lock = threading.Lock()
        self._lock = threading.Lock()
        self._idle: EchoStage | None = None
        self._unavailable = False

    def acquire(self) -> EchoStage | None:
        """Lend a closed stage, creating one when none is idle; ``None`` when unavailable."""
        with self._create_lock:
            with self._lock:
                idle, self._idle = self._idle, None
                if idle is not None or self._unavailable:
                    return idle
            created: EchoStage | None
            try:
                created = self._factory()
            except Exception:
                logger.exception("Echo cancellation could not be created")
                created = None
            if created is None:
                with self._lock:
                    self._unavailable = True
            return created

    def release(self, stage: EchoStage) -> None:
        """Take back a closed stage for the next capture."""
        with self._lock:
            if self._idle is None:
                self._idle = stage


class PassThroughEchoStage:
    """An echo stage that returns the microphone audio unchanged."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.rate = 0

    def open(self, capture_rate: int) -> None:
        self.rate = capture_rate

    def process(self, mic: np.ndarray, arrival: float) -> np.ndarray:
        return mic

    def flush(self) -> np.ndarray:
        return np.zeros(0, dtype=np.int16)

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class AudioBlock:
    """One processed stretch of microphone audio in both projections."""

    index: int
    pcm16: bytes
    """16 kHz mono int16 projection."""
    recording: bytes
    """Echo-processed mono int16 audio at ``recording_rate``."""
    recording_rate: int

    @property
    def duration(self) -> float:
        """Length of the block in seconds."""
        return len(self.recording) / 2 / self.recording_rate


@dataclass(frozen=True)
class CaptureGap:
    """A discontinuity: audio between the previous and the next block is missing."""

    reason: str


@dataclass(frozen=True)
class CaptureStatus:
    """Observable state of one capture."""

    state: str
    error_code: str | None = None
    microphone: CaptureFormat | None = None
    echo_state: str = ECHO_OFF


class CaptureSubscription:
    """A bounded, ordered queue of blocks and gaps for one consumer.

    The capture thread never blocks on it: once more than ``max_seconds`` of
    audio is waiting, the oldest blocks are dropped and the next read returns
    a :class:`CaptureGap` (``overrun``) before the remaining blocks.
    """

    def __init__(
        self,
        max_seconds: float,
        on_close: Callable[[CaptureSubscription], None] | None = None,
    ) -> None:
        self._condition = threading.Condition()
        self._items: deque[AudioBlock | CaptureGap] = deque()
        self._buffered_seconds = 0.0
        self._max_seconds = max_seconds
        self._overrun = False
        self._closed = False
        self._on_close = on_close

    @property
    def closed(self) -> bool:
        """Whether the subscription ended (closed by its reader or its capture)."""
        with self._condition:
            return self._closed

    def read(self, timeout: float | None = None) -> AudioBlock | CaptureGap | None:
        """Return the next block or gap, or ``None`` after ``timeout`` or once closed."""
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed or self._overrun or bool(self._items), timeout
            )
            if self._closed:
                return None
            if self._overrun:
                self._overrun = False
                return CaptureGap(GAP_OVERRUN)
            if not self._items:
                return None
            item = self._items.popleft()
            if isinstance(item, AudioBlock):
                self._buffered_seconds -= item.duration
            return item

    def close(self) -> None:
        """End the subscription and wake a waiting reader."""
        on_close = self._end()
        if on_close is not None:
            on_close(self)

    def _end(self) -> Callable[[CaptureSubscription], None] | None:
        with self._condition:
            if self._closed:
                return None
            self._closed = True
            self._items.clear()
            self._buffered_seconds = 0.0
            self._condition.notify_all()
            on_close, self._on_close = self._on_close, None
            return on_close

    def _push(self, item: AudioBlock | CaptureGap) -> None:
        with self._condition:
            if self._closed:
                return
            self._items.append(item)
            if isinstance(item, AudioBlock):
                self._buffered_seconds += item.duration
                while self._buffered_seconds > self._max_seconds and len(self._items) > 1:
                    dropped = self._items.popleft()
                    if isinstance(dropped, AudioBlock):
                        self._buffered_seconds -= dropped.duration
                    self._overrun = True
            self._condition.notify_all()


class _ReadError(Exception):
    """A read that returned no trustworthy audio."""


class AudioCapture:
    """Owns the microphone stream of one listener and fans its audio out.

    ``stop_event`` ends the capture thread; it closes its stream and echo
    stage itself and closes every subscription. ``on_status`` receives every
    :class:`CaptureStatus` change on the capture thread and must not block.
    ``backend`` is the sounddevice module (imported on the capture thread when
    ``None``).
    """

    def __init__(
        self,
        *,
        microphone: MicrophoneSelection | None,
        echo_cancellation: bool,
        echo_stages: EchoStagePool | None,
        on_status: Callable[[CaptureStatus], None],
        stop_event: threading.Event,
        backend: Any = None,
        reconnect_interval: float = RECONNECT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.perf_counter,
        block_seconds: float = BLOCK_SECONDS,
        history_seconds: float = HISTORY_SECONDS,
    ) -> None:
        self._requested = microphone.to_dict() if microphone is not None else None
        self._echo_cancellation = echo_cancellation
        self._echo_stages = echo_stages
        self._on_status = on_status
        self._stop = stop_event
        self._backend = backend
        self._reconnect_interval = reconnect_interval
        self._clock = clock
        self._block_seconds = block_seconds
        self._history_seconds = history_seconds
        self._thread: threading.Thread | None = None

        self._lock = threading.Lock()
        self._subscriptions: list[CaptureSubscription] = []
        self._history: deque[AudioBlock] = deque()
        self._history_seconds_buffered = 0.0
        self._next_index = 0
        self._contiguous_from = 0
        self._finished = False
        self._status = CaptureStatus(CAPTURE_OPENING)

        # Capture-thread state.
        self._sd: Any = None
        self._stream: Any = None
        self._format: CaptureFormat | None = None
        self._stage: EchoStage = PassThroughEchoStage(ECHO_OFF)
        self._borrowed_stage: EchoStage | None = None  # returned to the pool at the end
        self._stage_rate = 0  # output rate of the open stage; 0 while closed
        self._stage_open_rate = 0  # capture rate the stage was opened for
        self._resampler: Any = None

    # -- Public interface ----------------------------------------------------

    @property
    def status(self) -> CaptureStatus:
        """The latest published status."""
        with self._lock:
            return self._status

    def start(self) -> None:
        """Start the capture thread (once)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="vbot-voice-capture", daemon=True)
        self._thread.start()

    def join(self, timeout: float) -> bool:
        """Wait for the capture thread after ``stop_event``; ``True`` once it ended."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def subscribe(
        self, *, max_seconds: float, after_index: int | None = None
    ) -> CaptureSubscription:
        """Open a subscription for blocks after ``after_index`` (``None``: from now on).

        Blocks after ``after_index`` still in the history are delivered first.
        When the history cannot supply every block after it (trimmed, or a gap
        occurred since), a :class:`CaptureGap` (``history``) comes first. A
        subscription opened after the capture ended is closed at once.
        """
        subscription = CaptureSubscription(max_seconds, on_close=self._unsubscribe)
        with self._lock:
            if self._finished:
                subscription._end()
                return subscription
            if after_index is not None:
                first_index = after_index + 1
                if first_index < self._contiguous_from:
                    subscription._push(CaptureGap(GAP_HISTORY))
                for block in self._history:
                    if block.index >= first_index:
                        subscription._push(block)
            self._subscriptions.append(subscription)
        return subscription

    # -- Capture thread ------------------------------------------------------

    def _run(self) -> None:
        final = CaptureStatus(CAPTURE_STOPPED)
        try:
            self._sd = self._backend if self._backend is not None else _import_sounddevice()
            self._stage = self._create_stage()
            opened = self._open_first() if not self._stop.is_set() else False
            while not self._stop.is_set():
                opened = self._read_until_failure() if opened else self._wait_for_microphone()
        except Exception:
            logger.exception("Voice microphone capture stopped unexpectedly")
            final = CaptureStatus(CAPTURE_FAILED, ERROR_PIPELINE_FAILED)
        finally:
            self._close_stream()
            self._close_stage()
            if self._borrowed_stage is not None and self._echo_stages is not None:
                self._echo_stages.release(self._borrowed_stage)
            with self._lock:
                self._finished = True
                subscriptions, self._subscriptions = self._subscriptions, []
                self._history.clear()
            for subscription in subscriptions:
                subscription._end()
            self._publish(final)

    def _open_first(self) -> bool:
        if self._open_stream():
            return True
        if self._stop.is_set():
            return False
        # A device connected after startup is only visible after a refresh.
        if refresh_microphone_devices(self._sd) and self._open_stream():
            return True
        self._disconnect(ERROR_MICROPHONE_UNAVAILABLE)
        return False

    def _wait_for_microphone(self) -> bool:
        if self._stop.wait(self._reconnect_interval):
            return False
        refresh_microphone_devices(self._sd)
        if self._stop.is_set():
            return False
        if self._open_stream():
            logger.info("Voice microphone reconnected")
            return True
        logger.debug("Voice microphone is still unavailable")
        return False

    def _read_until_failure(self) -> bool:
        """Read until stopped (``True``) or until the microphone disconnects (``False``)."""
        failures = 0
        while not self._stop.is_set():
            try:
                samples, arrival = self._read_block()
            except _ReadError:
                failures += 1
                logger.warning("Voice microphone read failed", exc_info=True)
                self._end_segment(GAP_READ_FAILED)
                self._close_stream()
                if self._stop.is_set():
                    return True
                if failures >= _MAX_CONSECUTIVE_READ_FAILURES or not self._open_stream():
                    self._disconnect(ERROR_MICROPHONE_READ_FAILED)
                    return False
                continue
            failures = 0
            self._process(samples, arrival)
        return True

    def _read_block(self) -> tuple[np.ndarray, float]:
        capture_format = self._format
        assert capture_format is not None
        frames = max(1, round(capture_format.sample_rate * self._block_seconds))
        try:
            audio, overflowed = self._stream.read(frames)
            arrival = self._clock() - _read_available(self._stream) / capture_format.sample_rate
            samples = np.asarray(audio).reshape(-1)
        except Exception as exc:
            raise _ReadError("The microphone read failed") from exc
        if len(samples) != frames:
            raise _ReadError(f"The microphone returned {len(samples)} of {frames} samples")
        if overflowed:
            logger.info("Voice microphone input overflowed; delivering a capture gap")
            self._end_segment(GAP_OVERFLOW)
        return _to_int16(samples, capture_format.dtype), arrival

    def _process(self, samples: np.ndarray, arrival: float) -> None:
        try:
            output = self._stage.process(samples, arrival)
        except Exception:
            logger.warning(
                "Echo cancellation failed; passing the microphone through", exc_info=True
            )
            self._replace_failed_stage()
            output = self._stage.process(samples, arrival)
        self._emit(output)
        self._publish_echo_state()

    def _emit(self, output: np.ndarray) -> None:
        recording = np.asarray(output, dtype=np.int16).reshape(-1)
        if recording.size == 0:
            return
        if self._resampler is None:
            detection = recording
        else:
            detection = np.asarray(self._resampler.resample_chunk(recording), dtype=np.int16)
        with self._lock:
            block = AudioBlock(
                index=self._next_index,
                pcm16=detection.tobytes(),
                recording=recording.tobytes(),
                recording_rate=self._stage_rate,
            )
            self._next_index += 1
            self._history.append(block)
            self._history_seconds_buffered += block.duration
            while self._history_seconds_buffered > self._history_seconds and len(self._history) > 1:
                self._history_seconds_buffered -= self._history.popleft().duration
            self._contiguous_from = max(self._contiguous_from, self._history[0].index)
            for subscription in self._subscriptions:
                subscription._push(block)

    def _end_segment(self, reason: str) -> None:
        """Release held-back audio, then mark a discontinuity for every consumer."""
        try:
            self._emit(self._stage.flush())
            self._stage.reset()
        except Exception:
            logger.warning(
                "Echo cancellation failed; passing the microphone through", exc_info=True
            )
            self._replace_failed_stage()
        self._resampler = _create_resampler(self._stage_rate)
        self._deliver_gap(reason)

    def _deliver_gap(self, reason: str) -> None:
        gap = CaptureGap(reason)
        with self._lock:
            self._history.clear()
            self._history_seconds_buffered = 0.0
            self._contiguous_from = self._next_index
            for subscription in self._subscriptions:
                subscription._push(gap)

    def _open_stream(self) -> bool:
        """Open the microphone and prepare the echo stage for it; ``False`` on failure."""
        try:
            stream, capture_format = open_input_stream(self._sd, self._requested)
        except Exception:
            logger.warning("Voice microphone could not be opened", exc_info=True)
            return False
        if self._stop.is_set():
            close_input_stream(stream)
            return False
        self._stream = stream
        self._format = capture_format
        self._prepare_stage(capture_format.sample_rate)
        logger.info(
            "Voice microphone opened (device=%s, host_api=%s, rate=%s, echo=%s)",
            capture_format.name,
            capture_format.host_api,
            capture_format.sample_rate,
            self._stage.state,
        )
        self._publish(
            CaptureStatus(
                CAPTURE_CAPTURING,
                microphone=capture_format,
                echo_state=self._stage.state,
            )
        )
        return True

    def _prepare_stage(self, capture_rate: int) -> None:
        """Open the stage for ``capture_rate`` (again when the rate changed)."""
        if self._stage_rate and self._stage_open_rate == capture_rate:
            return
        if self._stage_rate:
            self._close_stage()
        try:
            self._stage.open(capture_rate)
            rate = int(self._stage.rate)
            if rate <= 0:
                raise ValueError(f"Echo stage reported an invalid rate: {rate}")
        except Exception:
            logger.warning(
                "Echo cancellation could not start; passing the microphone through", exc_info=True
            )
            self._stage_rate = 0
            self._replace_failed_stage()
            return
        self._stage_open_rate = capture_rate
        self._stage_rate = rate
        self._resampler = _create_resampler(rate)

    def _replace_failed_stage(self) -> None:
        """Pass the microphone through (``unavailable``) for the rest of this capture."""
        failed = self._stage
        self._stage = PassThroughEchoStage(ECHO_UNAVAILABLE)
        try:
            failed.close()
        except Exception:
            logger.debug("Failed echo stage did not close cleanly", exc_info=True)
        capture_rate = self._format.sample_rate if self._format is not None else 0
        previous_rate = self._stage_rate
        self._stage.open(capture_rate)
        self._stage_open_rate = capture_rate
        self._stage_rate = capture_rate
        self._resampler = _create_resampler(capture_rate)
        if previous_rate and previous_rate != capture_rate:
            self._deliver_gap(GAP_ECHO_FAILED)

    def _disconnect(self, error_code: str) -> None:
        logger.warning("Voice microphone disconnected (reason=%s)", error_code)
        self._close_stream()
        # The reference stream must be closed before PortAudio is refreshed.
        self._close_stage()
        self._publish(CaptureStatus(CAPTURE_DISCONNECTED, error_code, echo_state=self._stage.state))

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        self._format = None
        if stream is not None:
            close_input_stream(stream)

    def _close_stage(self) -> None:
        if not self._stage_rate:
            return
        self._stage_rate = 0
        self._stage_open_rate = 0
        self._resampler = None
        try:
            # Held-back audio is dropped: a close ends the stretch it belonged to.
            self._stage.flush()
            self._stage.close()
        except Exception:
            logger.warning("Echo stage did not close cleanly", exc_info=True)

    def _create_stage(self) -> EchoStage:
        if not self._echo_cancellation:
            return PassThroughEchoStage(ECHO_OFF)
        stage = self._echo_stages.acquire() if self._echo_stages is not None else None
        if stage is None:
            return PassThroughEchoStage(ECHO_UNAVAILABLE)
        self._borrowed_stage = stage
        return stage

    def _publish_echo_state(self) -> None:
        state = self._stage.state
        with self._lock:
            current = self._status
        if current.state == CAPTURE_CAPTURING and current.echo_state != state:
            self._publish(
                CaptureStatus(current.state, current.error_code, current.microphone, state)
            )

    def _publish(self, status: CaptureStatus) -> None:
        with self._lock:
            self._status = status
        try:
            self._on_status(status)
        except Exception:
            logger.exception("Voice capture status listener failed")

    def _unsubscribe(self, subscription: CaptureSubscription) -> None:
        with self._lock:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)


def _import_sounddevice() -> Any:
    import sounddevice  # type: ignore[import-untyped]

    return sounddevice


def _read_available(stream: Any) -> int:
    try:
        return max(0, int(stream.read_available))
    except Exception:
        return 0


def _to_int16(samples: np.ndarray, dtype: str) -> np.ndarray:
    if dtype == "float32":
        scaled = np.clip(samples.astype(np.float32), -1.0, 1.0) * 32767.0
        return np.asarray(np.clip(scaled, -32768, 32767), dtype=np.int16)
    return np.asarray(samples, dtype=np.int16)


def _create_resampler(source_rate: int) -> Any:
    """A stateful int16 resampler to 16 kHz, or ``None`` when no projection is needed."""
    if source_rate in (0, DETECTION_SAMPLE_RATE):
        return None
    import soxr  # type: ignore[import-untyped]

    return soxr.ResampleStream(source_rate, DETECTION_SAMPLE_RATE, 1, dtype="int16")
