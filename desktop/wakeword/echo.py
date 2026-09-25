"""Acoustic echo cancellation for the Desktop capture.

The capture owner feeds every microphone block through one echo stage before
any consumer sees it. The stage removes what the speakers played, using the
default playback device's loopback as reference, so speaker output (Live voice,
chat TTS, other audio) neither triggers phrases nor lands in command
recordings.

Pipeline (all on one shared ``time.perf_counter`` timeline at 48 kHz):

* The loopback thread delivers stereo float32 blocks roughly every 10 ms while
  something plays and nothing while the endpoint is silent. Blocks are
  downmixed, resampled to 48 kHz and written into a zero-initialised ring:
  time nobody wrote is silence, which is exactly what a silent loopback means.
* The capture thread passes each microphone block with its arrival time. The
  block is resampled to 48 kHz (stateful soxr) and placed on the timeline.
* Each stream is placed by ``_StreamClock``: contiguous device-clock samples
  re-anchored to arrival times. Arrival jitter is one-sided (late only), so the
  minimum offset over recent blocks tracks delivery latency; slow device-clock
  drift is corrected one sample at a time and long delivery gaps re-anchor.
* For every 10 ms capture frame at timeline index ``c`` the WebRTC audio
  processing module (AEC3) first receives the reference frame at
  ``c + lead`` and then the capture frame. The lead keeps the reference causal
  despite timestamp bias; AEC3 estimates the remaining device and acoustic
  delay itself (usable up to about 500 ms).
* A capture frame waits until the reference covers its window. When the
  loopback has been silent for a while the window is silence after a short
  grace; a hard limit bounds the wait while the loopback lags.

Without a reference (no loopback endpoint, other platforms, library missing)
the stage passes the resampled microphone audio through unchanged.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np

logger = logging.getLogger("vbot.desktop.wakeword.echo")

ECHO_SAMPLE_RATE = 48000
"""Output rate of the echo stage and processing rate of the echo canceller."""

STATE_ACTIVE = "active"
"""A reference is open (flowing or legitimately silent) or is being opened."""

STATE_NO_REFERENCE = "no_reference"
"""The echo canceller is installed but no playback reference is available."""

_FRAME = ECHO_SAMPLE_RATE // 100  # the processing module only accepts 10 ms frames
_LEAD = ECHO_SAMPLE_RATE * 20 // 1000  # reference fed ahead of the capture frame
_GRACE = ECHO_SAMPLE_RATE * 40 // 1000  # extra wait once the loopback went silent
_HARD_LIMIT = ECHO_SAMPLE_RATE * 300 // 1000  # longest a capture frame is held back
_REFERENCE_IDLE_S = 0.1  # loopback silence after which missing reference means silence
_RING_SAMPLES = ECHO_SAMPLE_RATE * 4

_CLOCK_WINDOW_BLOCKS = 50
_CLOCK_SLIP_SAMPLES = 8
_CLOCK_GAP_S = 0.1
_CLOCK_JUMP_S = 0.03

_POLL_INTERVAL_S = 1.0
_RETRY_INTERVAL_S = 30.0
_MONITOR_JOIN_S = 2.0
_INT16_SCALE = 32767.0

# PyAudioWPatch links its own PortAudio copy, independent of sounddevice's. Its
# initialisation is process-global and not thread-safe, so every stage's
# monitor thread initialises, opens, closes and terminates it under this lock.
_PORTAUDIO_WPATCH_LOCK = threading.Lock()


class EchoProcessor(Protocol):
    """Echo canceller working on exact 10 ms mono int16 frames at one fixed rate."""

    def process_reverse(self, frame: np.ndarray) -> None:
        """Feed one reference (speaker) frame."""

    def process_capture(self, frame: np.ndarray) -> np.ndarray:
        """Return the echo-cancelled copy of one microphone frame."""

    def close(self) -> None:
        """Release native resources."""


ReferenceBlockCallback = Callable[[np.ndarray, int, float, bool], None]
"""``(block[frames, channels] float32, rate, arrival, discontinuity)``."""


class LoopbackStream(Protocol):
    """An open capture of the default playback endpoint."""

    @property
    def name(self) -> str:
        """Human-readable endpoint name for logs."""

    def is_active(self) -> bool:
        """False once the stream stopped delivering for good (device lost)."""

    def close(self) -> None:
        """Stop and release the stream."""


class LoopbackBackend(Protocol):
    """Opens the loopback of the default playback endpoint.

    Every method runs on the stage's reference monitor thread.
    """

    def start(self) -> None:
        """Prepare the thread (for example COM) before any other call."""

    def default_endpoint(self) -> str | None:
        """Identity of the current default playback endpoint, None without one.

        Must be cheap: it is polled. Raises ``OSError`` when change detection is
        not possible on this system.
        """

    def open_default(self, on_block: ReferenceBlockCallback) -> LoopbackStream:
        """Open the default endpoint's loopback.

        Raises ``LookupError`` when there is no loopback endpoint and ``OSError``
        (or another exception) when opening failed.
        """

    def stop(self) -> None:
        """Release what ``start`` prepared; no stream is open any more."""


class _StreamClock:
    """Place the contiguous blocks of one device stream on the shared timeline.

    ``offset = arrival-derived latest possible start - contiguous start``.
    Arrival jitter only ever delays blocks, so the minimum offset over a window
    is a stable estimate of the stream's delivery latency. The first full
    window after a (re)anchor becomes the baseline; later drift of the window
    minimum away from it is device-clock drift and is corrected one sample per
    block. A persistent jump re-anchors at once; a delivery gap (silent
    loopback) re-anchors with the learned latency.
    """

    def __init__(self, rate: int) -> None:
        self.rate = rate
        self.next: int | None = None
        self.last_arrival: float | None = None
        self.offsets: deque[int] = deque(maxlen=_CLOCK_WINDOW_BLOCKS)
        self.latency: int | None = None
        self.baseline: int | None = None
        self.anchored = False
        self.resyncs = 0
        self.slips = 0
        self._jump = int(_CLOCK_JUMP_S * rate)
        self._reanchor = False

    def request_reanchor(self) -> None:
        """Start a new segment with the next block (samples were lost)."""
        self._reanchor = True

    def place(self, n: int, arrival: float) -> int:
        """Return the timeline index of the first of ``n`` samples that arrived."""
        arrival_start = round(arrival * self.rate) - n
        gap = (
            self.last_arrival is not None
            and arrival - self.last_arrival > _CLOCK_GAP_S + n / self.rate
        )
        self.last_arrival = arrival
        self.anchored = False
        if self.next is None or gap or self._reanchor:
            if self.next is not None:
                self.resyncs += 1
            self._reanchor = False
            self.next = arrival_start - (self.latency or 0)
            self.offsets.clear()
            self.baseline = None
            self.anchored = True
        offset = arrival_start - self.next
        if offset < 0:  # the block cannot start after it arrived: pull the stream back
            self._shift(offset)
            if self.baseline is not None:
                self.baseline = max(0, self.baseline + offset)
            offset = 0
        self.offsets.append(offset)
        if len(self.offsets) == _CLOCK_WINDOW_BLOCKS:
            self._correct(min(self.offsets))
        start = self.next
        self.next = start + n
        return start

    def _correct(self, window_min: int) -> None:
        if self.baseline is None:
            self.baseline = window_min
            if self.latency is None:
                self.latency = window_min
        elif window_min - self.baseline > self._jump:  # persistently late: re-anchor
            self._shift(window_min - self.baseline)
            self.resyncs += 1
        elif window_min - self.baseline > _CLOCK_SLIP_SAMPLES:  # device clock slower
            self._shift(1)
        elif self.baseline - window_min > _CLOCK_SLIP_SAMPLES:  # device clock faster
            self._shift(-1)

    def _shift(self, samples: int) -> None:
        assert self.next is not None
        self.next += samples
        self.offsets = deque(
            (offset - samples for offset in self.offsets), maxlen=_CLOCK_WINDOW_BLOCKS
        )
        self.slips += 1


class _StreamPlacer:
    """Resample one mono stream to the processing rate and place its output.

    Resampler output comes in bursts; it is positioned by its cumulative count,
    with the clock's corrections applied as deltas.
    """

    def __init__(self, in_rate: int, out_rate: int, dtype: type[np.generic]) -> None:
        self.in_rate = in_rate
        self.dtype = dtype
        self.clock = _StreamClock(out_rate)
        self._ratio = out_rate / in_rate
        self._resampler: Any | None = None
        if in_rate != out_rate:
            import soxr  # type: ignore[import-untyped]

            self._resampler = soxr.ResampleStream(in_rate, out_rate, 1, dtype=np.dtype(dtype).name)
        self._fraction = 0.0
        self._expected: int | None = None
        self._out_pos: int | None = None

    def feed(self, samples: np.ndarray, arrival: float) -> tuple[int, np.ndarray]:
        exact = len(samples) * self._ratio + self._fraction
        count = int(exact)
        self._fraction = exact - count
        start = self.clock.place(count, arrival)
        if self.clock.anchored or self._out_pos is None or self._expected is None:
            if self._resampler is not None and self._out_pos is not None:
                self._resampler.clear()  # drop filter state from before the gap
            self._out_pos = start
        else:
            self._out_pos += start - self._expected  # slip or re-anchor correction
        self._expected = start + count
        output = self._resample(samples, last=False)
        position = self._out_pos
        self._out_pos += len(output)
        return position, output

    def drain(self) -> tuple[int, np.ndarray]:
        """Return the samples the resampler still holds (end of stream)."""
        if self._resampler is None or self._out_pos is None:
            return 0, np.zeros(0, self.dtype)
        output = self._resample(np.zeros(0, self.dtype), last=True)
        position = self._out_pos
        self._out_pos += len(output)
        return position, output

    def _resample(self, samples: np.ndarray, *, last: bool) -> np.ndarray:
        contiguous = np.ascontiguousarray(samples, dtype=self.dtype)
        if self._resampler is None:
            return contiguous
        output = self._resampler.resample_chunk(contiguous, last=last)
        return np.asarray(output, dtype=self.dtype).reshape(-1)


class _ReferenceRing:
    """Reference samples for the timeline window ``[written_until - size, written_until)``.

    Positions inside the window that nobody wrote read as silence, as does
    everything outside it.
    """

    def __init__(self, size: int) -> None:
        self._buffer = np.zeros(size, np.int16)
        self._size = size
        self.written_until: int | None = None

    def write(self, start: int, samples: np.ndarray) -> None:
        if len(samples) == 0:
            return
        end = start + len(samples)
        until = self.written_until
        if until is None or end - self._size >= until:  # nothing old stays visible
            self._buffer[:] = 0
            until = end
        elif end > until:
            if start > until:  # silence between the previous write and this one
                self._put(until, np.zeros(start - until, np.int16))
            until = end
        oldest = until - self._size
        if start < oldest:
            samples = samples[oldest - start :]
            start = oldest
        self._put(start, samples)
        self.written_until = until

    def read(self, start: int, count: int) -> np.ndarray:
        output = np.zeros(count, np.int16)
        until = self.written_until
        if until is None:
            return output
        low = max(start, until - self._size)
        high = min(start + count, until)
        if high > low:
            index = low % self._size
            length = high - low
            first = min(length, self._size - index)
            output[low - start : low - start + first] = self._buffer[index : index + first]
            if first < length:
                output[low - start + first : high - start] = self._buffer[: length - first]
        return output

    def _put(self, start: int, samples: np.ndarray) -> None:
        index = start % self._size
        first = min(len(samples), self._size - index)
        self._buffer[index : index + first] = samples[:first]
        if first < len(samples):
            self._buffer[: len(samples) - first] = samples[first:]


class WebRtcEchoStage:
    """Echo stage of the Desktop capture: WebRTC AEC3 against the playback loopback.

    Only the capture thread calls ``open``, ``process``, ``flush``, ``reset`` and
    ``close``. Reference blocks arrive on the loopback library's thread; the
    loopback is opened, watched and reopened on a monitor thread started by
    ``open``. ``rate`` and ``state`` are read-only for callers; ``state`` may
    change on the monitor thread at any time between ``open`` and ``close``
    and can be read from any thread.
    """

    def __init__(
        self,
        *,
        processor_factory: Callable[[int], EchoProcessor],
        loopback: LoopbackBackend | None,
        clock: Callable[[], float] = time.perf_counter,
        poll_interval: float = _POLL_INTERVAL_S,
        retry_interval: float = _RETRY_INTERVAL_S,
    ) -> None:
        self.rate: int = ECHO_SAMPLE_RATE
        self.state: str = STATE_NO_REFERENCE
        self._processor_factory = processor_factory
        self._loopback = loopback
        self._clock = clock
        self._poll_interval = poll_interval
        self._retry_interval = retry_interval
        self._lock = threading.Lock()
        self._processor: EchoProcessor | None = None
        self._capture_rate = 0
        self._mic: _StreamPlacer | None = None
        self._pending = np.zeros(0, np.int16)
        self._pending_start = 0
        self._monitor: _ReferenceMonitor | None = None
        # Reference side, guarded by _lock.
        self._ring = _ReferenceRing(_RING_SAMPLES)
        self._reference: _StreamPlacer | None = None
        self._reference_generation = 0
        self._last_reference_arrival = float("-inf")

    # ------------------------------------------------------------------ capture thread

    def open(self, capture_rate: int) -> None:
        """Prepare for a microphone stream at ``capture_rate`` and open the reference.

        The loopback opens asynchronously on the monitor thread; until it is
        open the reference counts as silent. Never raises for a missing
        reference: ``state`` reports it.
        """
        if capture_rate <= 0:
            raise ValueError("capture_rate must be positive")
        if self._processor is not None:
            self.close()
        self._processor = self._processor_factory(ECHO_SAMPLE_RATE)
        self._capture_rate = capture_rate
        self._reset_capture()
        with self._lock:
            self._reset_reference()
        if self._loopback is None:
            self._set_state(STATE_NO_REFERENCE, "no playback loopback on this platform")
            return
        self._set_state(STATE_ACTIVE, "opening the playback loopback")
        monitor = _ReferenceMonitor(self, self._loopback, self._poll_interval, self._retry_interval)
        with self._lock:
            self._monitor = monitor
        monitor.start()

    def process(self, mic: np.ndarray, arrival: float) -> np.ndarray:
        """Feed one mono int16 block at the capture rate; return cleaned 48 kHz audio.

        ``arrival`` is the ``perf_counter`` time at which the block's last
        sample was available. The result may be shorter or longer than the
        input (frames wait for their reference) and may be empty.
        """
        placer = self._require_open()
        samples = np.asarray(mic)
        if samples.dtype != np.int16:
            raise TypeError("microphone samples must be int16")
        now = max(arrival, self._clock())
        if samples.size:
            self._append_capture(*placer.feed(samples.reshape(-1), arrival))
        return self._release(now, force=False)

    def flush(self) -> np.ndarray:
        """Release every held-back sample, then start the next block afresh."""
        if self._mic is None or self._processor is None:
            return np.zeros(0, np.int16)
        self._append_capture(*self._mic.drain())
        output = self._release(self._clock(), force=True)
        self._reset_capture()
        return output

    def reset(self) -> None:
        """Forget held-back audio and timing after a capture gap; keep the canceller."""
        if self._processor is not None:
            self._reset_capture()

    def close(self) -> None:
        """Close the reference and release the canceller; ``open`` may follow."""
        with self._lock:
            monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.stop()
            monitor.join(_MONITOR_JOIN_S)
            if monitor.is_alive():
                logger.warning("Echo reference monitor did not stop in time; abandoning it")
        with self._lock:
            self._reference_generation += 1
            self._reset_reference()
        processor, self._processor = self._processor, None
        self._mic = None
        self._pending = np.zeros(0, np.int16)
        if processor is not None:
            processor.close()

    # ------------------------------------------------------------------ capture internals

    def _require_open(self) -> _StreamPlacer:
        if self._mic is None or self._processor is None:
            raise RuntimeError("echo stage is not open")
        return self._mic

    def _reset_capture(self) -> None:
        self._mic = _StreamPlacer(self._capture_rate, ECHO_SAMPLE_RATE, np.int16)
        self._pending = np.zeros(0, np.int16)
        self._pending_start = 0

    def _append_capture(self, position: int, samples: np.ndarray) -> None:
        if not len(samples):
            return
        # Held samples stay contiguous; a clock correction moves the whole backlog.
        self._pending_start = position - len(self._pending)
        self._pending = np.concatenate((self._pending, samples))

    def _release(self, now: float, *, force: bool) -> np.ndarray:
        processor = self._processor
        assert processor is not None
        if self.state != STATE_ACTIVE:
            output = self._pending
            self._pending_start += len(output)
            self._pending = np.zeros(0, np.int16)
            return output
        frames: list[np.ndarray] = []
        while len(self._pending) >= _FRAME:
            reference_start = self._pending_start + _LEAD
            with self._lock:
                if not force and not self._reference_ready(reference_start, now):
                    break
                reference = self._ring.read(reference_start, _FRAME)
            processor.process_reverse(reference)
            frames.append(_as_frame(processor.process_capture(self._pending[:_FRAME])))
            self._pending = self._pending[_FRAME:]
            self._pending_start += _FRAME
        if force and len(self._pending):
            remainder = len(self._pending)
            with self._lock:
                reference = self._ring.read(self._pending_start + _LEAD, _FRAME)
            padded = np.zeros(_FRAME, np.int16)
            padded[:remainder] = self._pending
            processor.process_reverse(reference)
            frames.append(_as_frame(processor.process_capture(padded))[:remainder])
            self._pending_start += remainder
            self._pending = np.zeros(0, np.int16)
        return np.concatenate(frames) if frames else np.zeros(0, np.int16)

    def _reference_ready(self, reference_start: int, now: float) -> bool:
        """Whether the capture frame whose reference starts here may be processed."""
        end = reference_start + _FRAME
        until = self._ring.written_until
        if until is not None and until >= end:
            return True
        now_index = round(now * ECHO_SAMPLE_RATE)
        if now_index >= end + _HARD_LIMIT:
            return True
        idle = now - self._last_reference_arrival > _REFERENCE_IDLE_S
        return idle and now_index >= end + _GRACE

    # ------------------------------------------------------------------ reference side

    def _reset_reference(self) -> None:
        self._ring = _ReferenceRing(_RING_SAMPLES)
        self._reference = None
        self._last_reference_arrival = float("-inf")

    def _reference_sink(self, monitor: _ReferenceMonitor) -> ReferenceBlockCallback:
        """Start a new reference stream and return the callback for its blocks.

        A monitor that was already replaced gets a sink whose blocks are dropped.
        """
        with self._lock:
            if monitor is self._monitor:
                self._reference_generation += 1
                self._reference = None
                generation = self._reference_generation
            else:
                generation = -1

        def on_block(block: np.ndarray, rate: int, arrival: float, discontinuity: bool) -> None:
            self._push_reference(generation, block, rate, arrival, discontinuity)

        return on_block

    def _push_reference(
        self,
        generation: int,
        block: np.ndarray,
        rate: int,
        arrival: float,
        discontinuity: bool,
    ) -> None:
        samples = np.asarray(block, dtype=np.float32)
        mono = samples.mean(axis=1) if samples.ndim == 2 else samples.reshape(-1)
        if not mono.size or rate <= 0:
            return
        with self._lock:
            if generation != self._reference_generation:
                return  # a late block from a stream that was already replaced
            placer = self._reference
            if placer is None or placer.in_rate != rate:
                placer = self._reference = _StreamPlacer(rate, ECHO_SAMPLE_RATE, np.float32)
            if discontinuity:
                placer.clock.request_reanchor()
            position, output = placer.feed(mono, arrival)
            pcm = np.clip(np.round(output * _INT16_SCALE), -32768, 32767).astype(np.int16)
            self._ring.write(position, pcm)
            self._last_reference_arrival = max(self._last_reference_arrival, arrival)

    def _set_state(self, state: str, reason: str, monitor: _ReferenceMonitor | None = None) -> None:
        """Record a state change; a retired monitor's report is ignored."""
        with self._lock:
            if monitor is not None and monitor is not self._monitor:
                return
            if self.state == state:
                return
            self.state = state
        logger.info("Echo cancellation reference %s: %s", state, reason)


def _as_frame(output: np.ndarray) -> np.ndarray:
    frame = np.asarray(output, dtype=np.int16).reshape(-1)
    if len(frame) != _FRAME:
        raise RuntimeError("echo canceller returned a frame of the wrong length")
    return frame


class _ReferenceMonitor(threading.Thread):
    """Open the playback loopback and follow default-device changes.

    Neither PortAudio library follows a change of the default playback device,
    so the monitor polls the default endpoint's identity (a cheap Core Audio
    query) and reopens the loopback when it changes or when the stream stopped.
    Polling on a timer bounds the uncancelled time after a device switch to
    about one interval even while audio keeps playing; reopening only on a
    silence gap could not tell "nothing plays" from "device switched" and would
    re-enumerate every device again and again while the speakers are quiet.
    """

    def __init__(
        self,
        stage: WebRtcEchoStage,
        backend: LoopbackBackend,
        poll_interval: float,
        retry_interval: float,
    ) -> None:
        super().__init__(name="vbot-echo-reference", daemon=True)
        self._stage = stage
        self._backend = backend
        self._poll_interval = poll_interval
        self._retry_interval = retry_interval
        self._stopping = threading.Event()
        self._stream: LoopbackStream | None = None
        self._endpoint: str | None = None
        self._failed_endpoint: str | None = None
        self._retry_at = 0.0
        self._detection_failed = False

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        try:
            self._backend.start()
        except Exception as error:
            logger.warning("Echo reference could not start: %s", type(error).__name__)
            self._update(STATE_NO_REFERENCE, "playback loopback unavailable")
            return
        try:
            while not self._stopping.is_set():
                self._poll()
                self._stopping.wait(self._poll_interval)
        finally:
            self._close_stream()
            try:
                self._backend.stop()
            except Exception as error:
                logger.warning("Echo reference cleanup failed: %s", type(error).__name__)

    def _poll(self) -> None:
        endpoint = self._current_endpoint()
        if endpoint is None:
            self._close_stream()
            self._endpoint = None
            self._failed_endpoint = None
            self._update(STATE_NO_REFERENCE, "no playback device")
            return
        stream = self._stream
        if stream is not None:
            if endpoint != self._endpoint:
                logger.info("Default playback device changed; reopening the echo reference")
            elif not _stream_active(stream):
                logger.warning("Echo reference stream stopped; reopening it")
            else:
                return
            self._close_stream()
            self._failed_endpoint = None
        elif endpoint == self._failed_endpoint and time.monotonic() < self._retry_at:
            return
        self._open(endpoint)

    def _current_endpoint(self) -> str | None:
        """Default endpoint identity; a constant when change detection is unavailable."""
        if self._detection_failed:
            return "default"
        try:
            return self._backend.default_endpoint()
        except OSError as error:
            self._detection_failed = True
            logger.warning(
                "Playback device changes cannot be detected (%s); keeping the first loopback",
                type(error).__name__,
            )
            return "default"

    def _open(self, endpoint: str) -> None:
        first_failure = endpoint != self._failed_endpoint
        try:
            stream = self._backend.open_default(self._stage._reference_sink(self))
        except LookupError:
            self._failed(endpoint)
            self._update(STATE_NO_REFERENCE, "the playback device has no loopback")
            return
        except Exception as error:
            self._failed(endpoint)
            log = logger.warning if first_failure else logger.debug
            log("Echo reference could not open the playback loopback: %s", type(error).__name__)
            self._update(STATE_NO_REFERENCE, "opening the playback loopback failed")
            return
        if self._stopping.is_set():
            _close_quietly(stream)
            return
        self._stream = stream
        self._endpoint = endpoint
        self._failed_endpoint = None
        logger.info("Echo reference opened: %s", stream.name)
        self._update(STATE_ACTIVE, f"playing device {stream.name}")

    def _failed(self, endpoint: str) -> None:
        self._failed_endpoint = endpoint
        self._retry_at = time.monotonic() + self._retry_interval

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            _close_quietly(stream)

    def _update(self, state: str, reason: str) -> None:
        self._stage._set_state(state, reason, self)


def _stream_active(stream: LoopbackStream) -> bool:
    try:
        return stream.is_active()
    except Exception:
        return False


def _close_quietly(stream: LoopbackStream) -> None:
    try:
        stream.close()
    except Exception as error:
        logger.warning("Echo reference stream did not close cleanly: %s", type(error).__name__)


# ---------------------------------------------------------------------------- WebRTC


class _LiveKitProcessor:
    """WebRTC audio processing (AEC3 + high-pass) through livekit's local FFI.

    No LiveKit server or room is involved. The native side terminates the
    process on malformed input, so every frame is validated here first.
    """

    def __init__(self, rate: int) -> None:
        rtc = importlib.import_module("livekit.rtc")
        self._samples = rate // 100
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)
        self._capture = bytearray(self._samples * 2)
        self._reverse = bytearray(self._samples * 2)
        self._capture_frame = rtc.AudioFrame(self._capture, rate, 1, self._samples)
        self._reverse_frame = rtc.AudioFrame(self._reverse, rate, 1, self._samples)
        self._capture_view = np.frombuffer(self._capture, np.int16)
        self._reverse_view = np.frombuffer(self._reverse, np.int16)

    def process_reverse(self, frame: np.ndarray) -> None:
        self._reverse_view[:] = self._checked(frame)
        self._apm.process_reverse_stream(self._reverse_frame)

    def process_capture(self, frame: np.ndarray) -> np.ndarray:
        self._capture_view[:] = self._checked(frame)
        self._apm.process_stream(self._capture_frame)
        return self._capture_view.copy()

    def close(self) -> None:
        handle = getattr(self._apm, "_ffi_handle", None)
        dispose = getattr(handle, "dispose", None)
        if callable(dispose):
            dispose()

    def _checked(self, frame: np.ndarray) -> np.ndarray:
        if frame.dtype != np.int16 or frame.shape != (self._samples,):
            raise ValueError("echo canceller frames must be 10 ms of mono int16")
        return frame


# ---------------------------------------------------------------------------- Windows loopback


class _WasapiLoopbackStream:
    """PyAudioWPatch loopback stream; owns its PortAudio initialisation."""

    def __init__(self, audio: Any, stream: Any, name: str) -> None:
        self._audio = audio
        self._stream = stream
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def is_active(self) -> bool:
        return bool(self._stream.is_active())

    def close(self) -> None:
        with _PORTAUDIO_WPATCH_LOCK:
            try:
                self._stream.stop_stream()
                self._stream.close()
            finally:
                # Terminating makes the next open enumerate devices afresh.
                self._audio.terminate()


class _WasapiLoopbackBackend:
    """Default playback endpoint loopback on Windows (PyAudioWPatch + Core Audio).

    WASAPI loopback delivers blocks only while something plays. Its callback
    timestamps are unusable, so arrival is ``perf_counter`` at callback start.
    """

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._endpoints: _DefaultPlaybackEndpoints | None = None

    def start(self) -> None:
        self._endpoints = _DefaultPlaybackEndpoints()

    def default_endpoint(self) -> str | None:
        if self._endpoints is None:
            raise OSError("playback endpoint detection is not started")
        return self._endpoints.current()

    def open_default(self, on_block: ReferenceBlockCallback) -> LoopbackStream:
        pyaudio = importlib.import_module("pyaudiowpatch")
        with _PORTAUDIO_WPATCH_LOCK:
            audio = pyaudio.PyAudio()
            try:
                device = audio.get_default_wasapi_loopback()
                rate = int(device["defaultSampleRate"])
                channels = int(device["maxInputChannels"])
                if rate < 8000 or channels < 1:
                    raise LookupError("loopback endpoint reports no usable format")
                stream = audio.open(
                    format=pyaudio.paFloat32,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=int(device["index"]),
                    frames_per_buffer=rate // 100,
                    stream_callback=self._callback(pyaudio, on_block, rate, channels),
                )
            except BaseException:
                audio.terminate()
                raise
        name = str(device.get("name", "")).removesuffix(" [Loopback]")
        return _WasapiLoopbackStream(audio, stream, f"{name} ({rate} Hz, {channels} ch)")

    def stop(self) -> None:
        endpoints, self._endpoints = self._endpoints, None
        if endpoints is not None:
            endpoints.close()

    def _callback(
        self, pyaudio: Any, on_block: ReferenceBlockCallback, rate: int, channels: int
    ) -> Callable[[bytes | None, int, Any, int], tuple[None, int]]:
        clock = self._clock
        overflow = int(pyaudio.paInputOverflow)
        keep_going = int(pyaudio.paContinue)
        failed = False

        def callback(
            data: bytes | None, frame_count: int, _time_info: Any, status: int
        ) -> tuple[None, int]:
            nonlocal failed
            arrival = clock()
            try:
                if data and frame_count > 0:
                    block = np.frombuffer(data, dtype=np.float32)
                    if block.size == frame_count * channels:
                        on_block(
                            block.reshape(frame_count, channels),
                            rate,
                            arrival,
                            bool(status & overflow),
                        )
            except Exception as error:  # never let an exception stop the audio thread
                if not failed:
                    failed = True
                    logger.warning("Echo reference block was dropped: %s", type(error).__name__)
            return None, keep_going

        return callback


class _DefaultPlaybackEndpoints:
    """Identity of the default playback endpoints through Core Audio (COM, ctypes).

    Created, queried and closed on one thread. The query costs well under a
    millisecond with the cached device enumerator.
    """

    _E_NOTFOUND = 0x80070490
    _RPC_E_CHANGED_MODE = 0x80010106
    _CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    _IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    _RENDER = 0
    _ROLES = (0, 1)  # console, multimedia: PortAudio and browsers pick either

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Core Audio is only available on Windows")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
        ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        ole32.CLSIDFromString.restype = ctypes.c_long
        ole32.CoCreateInstance.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        ole32.CoCreateInstance.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        self._ole32 = ole32
        self._get_default = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._get_id = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )
        self._release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

        result = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        if result < 0 and result & 0xFFFFFFFF != self._RPC_E_CHANGED_MODE:
            raise OSError(f"CoInitializeEx failed (0x{result & 0xFFFFFFFF:08X})")
        self._uninitialize = result >= 0
        self._enumerator = ctypes.c_void_p()
        try:
            clsid = self._guid(self._CLSID_ENUMERATOR)
            iid = self._guid(self._IID_ENUMERATOR)
            result = ole32.CoCreateInstance(
                ctypes.byref(clsid), None, 0x17, ctypes.byref(iid), ctypes.byref(self._enumerator)
            )
            if result < 0 or not self._enumerator.value:
                raise OSError(f"device enumerator unavailable (0x{result & 0xFFFFFFFF:08X})")
        except BaseException:
            if self._uninitialize:
                ole32.CoUninitialize()
            raise

    def current(self) -> str | None:
        ids = [self._default_id(role) for role in self._ROLES]
        if not any(ids):
            return None
        return "|".join(endpoint or "" for endpoint in ids)

    def close(self) -> None:
        enumerator, self._enumerator = self._enumerator, self._ctypes.c_void_p()
        if enumerator.value:
            self._method(enumerator, 2, self._release)(enumerator)
        if self._uninitialize:
            self._uninitialize = False
            self._ole32.CoUninitialize()

    def _default_id(self, role: int) -> str | None:
        ctypes = self._ctypes
        if not self._enumerator.value:
            raise OSError("device enumerator is closed")
        device = ctypes.c_void_p()
        get_default = self._method(self._enumerator, 4, self._get_default)
        result = get_default(self._enumerator, self._RENDER, role, ctypes.byref(device))
        if result & 0xFFFFFFFF == self._E_NOTFOUND:
            return None
        if result < 0 or not device.value:
            raise OSError(f"default playback endpoint query failed (0x{result & 0xFFFFFFFF:08X})")
        try:
            text = ctypes.c_void_p()
            result = self._method(device, 5, self._get_id)(device, ctypes.byref(text))
            if result < 0 or not text.value:
                raise OSError(f"playback endpoint id unavailable (0x{result & 0xFFFFFFFF:08X})")
            try:
                return str(ctypes.wstring_at(text.value))
            finally:
                self._ole32.CoTaskMemFree(text)
        finally:
            self._method(device, 2, self._release)(device)

    def _method(self, instance: Any, index: int, prototype: Any) -> Any:
        ctypes = self._ctypes
        table = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        return prototype(table[index])

    def _guid(self, text: str) -> Any:
        guid = (self._ctypes.c_ubyte * 16)()
        result = self._ole32.CLSIDFromString(text, self._ctypes.byref(guid))
        if result < 0:
            raise OSError(f"invalid GUID {text}")
        return guid


# ---------------------------------------------------------------------------- factory


def _default_loopback() -> LoopbackBackend | None:
    if sys.platform != "win32" or importlib.util.find_spec("pyaudiowpatch") is None:
        return None
    return _WasapiLoopbackBackend()


def create_echo_stage() -> WebRtcEchoStage | None:
    """Create the Desktop echo stage, or None when the echo canceller is not installed.

    Imports the WebRTC library and initialises its native runtime once (a
    first import can take seconds), so call it off the GUI thread.
    """
    try:
        _LiveKitProcessor(ECHO_SAMPLE_RATE).close()
    except Exception as error:
        logger.warning("Echo cancellation unavailable: %s", type(error).__name__)
        return None
    return WebRtcEchoStage(processor_factory=_LiveKitProcessor, loopback=_default_loopback())
