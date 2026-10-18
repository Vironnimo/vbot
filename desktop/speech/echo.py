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
* Each stream is placed by ``alignment._StreamClock``: contiguous device-clock samples
  kept on the lower envelope of their arrival times. Arrival jitter is
  one-sided (late only), so a block that arrives earlier than its placement
  pulls the stream back at once (inserted samples, a fast device clock), a
  floor that stays later for a confirmation window moves it forward by the
  whole excess (lost samples), slow device-clock drift is corrected one sample
  at a time and long delivery gaps re-anchor. Only arrival times and sample
  counts are used, never the signal, so gated or silent audio places the same.
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
import threading
import time
from collections.abc import Callable
from typing import Protocol

import numpy as np

from desktop.speech import _echo_alignment as alignment
from desktop.speech import _echo_reference as echo_reference
from desktop.speech import _echo_windows as windows

logger = logging.getLogger("vbot.desktop.speech.echo")

ECHO_SAMPLE_RATE = 48000
"""Output rate of the echo stage and processing rate of the echo canceller."""

_FRAME = ECHO_SAMPLE_RATE // 100  # the processing module only accepts 10 ms frames
_LEAD = ECHO_SAMPLE_RATE * 20 // 1000  # reference fed ahead of the capture frame
_GRACE = ECHO_SAMPLE_RATE * 40 // 1000  # extra wait once the loopback went silent
_HARD_LIMIT = ECHO_SAMPLE_RATE * 300 // 1000  # longest a capture frame is held back
_REFERENCE_IDLE_S = 0.1  # loopback silence after which missing reference means silence
_RING_SAMPLES = ECHO_SAMPLE_RATE * 4

_INT16_SCALE = 32767.0


class EchoProcessor(Protocol):
    """Echo canceller working on exact 10 ms mono int16 frames at one fixed rate."""

    def process_reverse(self, frame: np.ndarray) -> None:
        """Feed one reference (speaker) frame."""

    def process_capture(self, frame: np.ndarray) -> np.ndarray:
        """Return the echo-cancelled copy of one microphone frame."""

    def close(self) -> None:
        """Release native resources."""


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
        loopback: echo_reference.LoopbackBackend | None,
        clock: Callable[[], float] = time.perf_counter,
        poll_interval: float = echo_reference._POLL_INTERVAL_S,
        retry_interval: float = echo_reference._RETRY_INTERVAL_S,
    ) -> None:
        self.rate: int = ECHO_SAMPLE_RATE
        self.state: str = echo_reference.STATE_NO_REFERENCE
        self._processor_factory = processor_factory
        self._loopback = loopback
        self._clock = clock
        self._poll_interval = poll_interval
        self._retry_interval = retry_interval
        self._lock = threading.Lock()
        self._processor: EchoProcessor | None = None
        self._capture_rate = 0
        self._mic: alignment._StreamPlacer | None = None
        self._mic_stats = alignment._PlacementStats("microphone", ECHO_SAMPLE_RATE)
        self._pending = np.zeros(0, np.int16)
        self._pending_start = 0
        self._monitor: echo_reference.ReferenceMonitor | None = None
        # Reference side, guarded by _lock.
        self._ring = alignment._ReferenceRing(_RING_SAMPLES)
        self._reference: alignment._StreamPlacer | None = None
        self._reference_stats = alignment._PlacementStats("reference", ECHO_SAMPLE_RATE)
        self._reference_generation = 0
        self._last_reference_arrival = float("-inf")

    # ------------------------------------------------------------------ capture thread

    def open(self, capture_rate: int) -> None:
        """Prepare for a microphone stream at ``capture_rate`` and open the echo_reference.

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
        self._mic_stats = alignment._PlacementStats("microphone", ECHO_SAMPLE_RATE)
        self._reset_capture()
        with self._lock:
            self._reference_stats = alignment._PlacementStats("reference", ECHO_SAMPLE_RATE)
            self._reset_reference()
        if self._loopback is None:
            self._set_state(
                echo_reference.STATE_NO_REFERENCE, "no playback loopback on this platform"
            )
            return
        self._set_state(echo_reference.STATE_ACTIVE, "opening the playback loopback")
        monitor = echo_reference.ReferenceMonitor(
            self, self._loopback, self._poll_interval, self._retry_interval
        )
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

    def stats(self) -> dict[str, dict[str, float]]:
        """Timing counters of the microphone and the reference stream since ``open``.

        Read-only diagnostics, safe from any thread: effective rate, lost-sample
        re-anchors, pull-backs (inserted samples), drift slips and gap re-anchors.
        """
        with self._lock:
            reference = self._reference_stats.snapshot()
        return {"microphone": self._mic_stats.snapshot(), "reference": reference}

    def close(self) -> None:
        """Close the reference and release the canceller; ``open`` may follow."""
        with self._lock:
            monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.stop()
            monitor.join(echo_reference._MONITOR_JOIN_S)
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

    def _require_open(self) -> alignment._StreamPlacer:
        if self._mic is None or self._processor is None:
            raise RuntimeError("echo stage is not open")
        return self._mic

    def _reset_capture(self) -> None:
        self._mic = alignment._StreamPlacer(
            self._capture_rate, ECHO_SAMPLE_RATE, np.int16, self._mic_stats
        )
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
        if self.state != echo_reference.STATE_ACTIVE:
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
        self._ring = alignment._ReferenceRing(_RING_SAMPLES)
        self._reference = None
        self._last_reference_arrival = float("-inf")

    def _reference_sink(
        self, monitor: echo_reference.ReferenceMonitor
    ) -> echo_reference.ReferenceBlockCallback:
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
                placer = self._reference = alignment._StreamPlacer(
                    rate, ECHO_SAMPLE_RATE, np.float32, self._reference_stats
                )
            if discontinuity:
                placer.clock.request_reanchor()
            position, output = placer.feed(mono, arrival)
            pcm = np.clip(np.round(output * _INT16_SCALE), -32768, 32767).astype(np.int16)
            self._ring.write(position, pcm)
            self._last_reference_arrival = max(self._last_reference_arrival, arrival)

    def _set_state(
        self, state: str, reason: str, monitor: echo_reference.ReferenceMonitor | None = None
    ) -> None:
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


# ---------------------------------------------------------------------------- factory


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
    return WebRtcEchoStage(processor_factory=_LiveKitProcessor, loopback=windows.default_loopback())
