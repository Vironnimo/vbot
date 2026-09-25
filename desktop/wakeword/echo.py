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

# Stream placement (``_StreamClock``). The confirmation window must outlast the
# delivery stalls of a working audio stack (scheduling, USB and wireless
# buffering: tens of milliseconds, rarely 100 ms), so a burst of late blocks is
# never mistaken for lost samples, and stay well below the canceller's own
# recovery time from a delay change (about a second), so a real loss is undone
# before the canceller re-adapts to it.
_CLOCK_CONFIRM_S = 0.3
_CLOCK_CONFIRM_BLOCKS = 4  # a floor needs a few blocks even when blocks are long
# A later floor counts as lost samples only when it exceeds this many times the
# stream's median jitter (a block's lateness over the floor of the confirmation
# window it starts), measured at runtime over the last 200 blocks: 2 s of 10 ms
# blocks, so the few blocks of a delivery stall stay a minority, yet short
# enough to follow a change of system load. Recurring latency floors, as
# measured on WASAPI streams (median jitter about 1 ms, the 0.3 s floor within
# 0.7 ms of the 2 s floor at p99), and simulated exponential, uniform and
# packetised jitter never put a whole window above three medians by chance in
# hours, while a loss of a few milliseconds is still caught.
_CLOCK_JITTER_MARGIN = 3
_CLOCK_JITTER_BLOCKS = 200
# Slow drift follows the floor of the last 50 blocks, one sample per block
# beyond a deadband: the floor of a steady stream wanders by a few samples
# (sample-rounded timestamps, sub-0.1 ms scheduling noise); following that
# would only add placement noise, and 8 samples at 48 kHz is far below what the
# canceller notices. One sample per block tracks up to 2000 ppm at 10 ms blocks
# and 500 ppm at 40 ms blocks; faster drift is caught by the step correction.
# The slips act only on a full window of blocks since the last step or anchor,
# so drift within their reach may raise the floor by the deadband plus one
# sample per block of that window before they catch up. A step needs a floor
# beyond that allowance, so drift the slips can follow never turns into steps,
# while a device falling behind faster keeps stepping by small amounts (a step
# restarts the allowance).
_CLOCK_WINDOW_BLOCKS = 50
_CLOCK_DEADBAND_S = 1 / 6000
# No blocks for longer than any stall of a working stream: the stream paused
# (a loopback delivers nothing while nothing plays) or restarted.
_CLOCK_GAP_S = 0.1
# Diagnostics: an effective rate beyond ordinary crystal tolerance (about
# +-100 ppm) by a factor of ten is worth an INFO line once enough audio passed.
_CLOCK_RATE_NOTICE_PPM = 1000.0
_CLOCK_RATE_NOTICE_AFTER_S = 10.0
_CLOCK_SUMMARY_S = 60.0

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


class _PlacementStats:
    """Placement counters of one stream since the stage opened; reports anomalies.

    Survives the stream's re-anchors and replacements (a reopened loopback, a
    capture gap). Holds counts and durations only, never audio. The first lost
    samples or an effective rate far off nominal are logged once at INFO; later
    changes are summarised at most every ``_CLOCK_SUMMARY_S`` at DEBUG.
    """

    def __init__(self, name: str, rate: int) -> None:
        self.name = name
        self.rate = rate
        self.samples = 0  # placed (after resampling to ``rate``)
        self.anchors = 0  # new segments after a delivery gap or a reported loss
        self.steps = 0  # confirmed later floors: samples lost
        self.step_samples = 0
        self.largest_step = 0
        self.pullbacks = 0  # blocks earlier than their placement: samples inserted
        self.pullback_samples = 0
        self.largest_pullback = 0
        self.slips = 0  # one-sample drift corrections
        self._reported = False
        self._summary_at: float | None = None
        self._summarised: dict[str, float] = {}

    def rate_ppm(self) -> float:
        """Delivered samples against timeline time, in ppm (negative: samples missing)."""
        net = self.step_samples + self.slips - self.pullback_samples
        elapsed = self.samples + net
        return (self.samples / elapsed - 1.0) * 1e6 if elapsed > 0 else 0.0

    def snapshot(self) -> dict[str, float]:
        ms = 1000.0 / self.rate
        return {
            "seconds": round(self.samples / self.rate, 3),
            "anchors": self.anchors,
            "steps": self.steps,
            "step_ms_total": round(self.step_samples * ms, 3),
            "step_ms_largest": round(self.largest_step * ms, 3),
            "pullbacks": self.pullbacks,
            "pullback_ms_total": round(self.pullback_samples * ms, 3),
            "pullback_ms_largest": round(self.largest_pullback * ms, 3),
            "slips": self.slips,
            "rate_ppm": round(self.rate_ppm(), 1),
        }

    def review(self, now: float) -> None:
        """Log the first anomaly at INFO, later changes at most once a period at DEBUG."""
        if not self._reported:
            seconds = self.samples / self.rate
            off_rate = (
                seconds >= _CLOCK_RATE_NOTICE_AFTER_S
                and abs(self.rate_ppm()) > _CLOCK_RATE_NOTICE_PPM
            )
            if self.steps or off_rate:
                self._reported = True
                self._summary_at = now
                self._summarised = self.snapshot()
                logger.info("Echo %s timing: %s", self.name, self._describe(self._summarised))
            return
        assert self._summary_at is not None
        if now - self._summary_at < _CLOCK_SUMMARY_S:
            return
        current = self.snapshot()
        keys = ("anchors", "steps", "pullbacks", "slips")
        if any(current[key] != self._summarised[key] for key in keys):
            logger.debug("Echo %s timing: %s", self.name, self._describe(current))
        self._summary_at = now
        self._summarised = current

    @staticmethod
    def _describe(values: dict[str, float]) -> str:
        return (
            f"effective rate {values['rate_ppm']:+.0f} ppm over {values['seconds']:.0f} s; "
            f"{values['steps']:.0f} lost-sample re-anchors (largest "
            f"{values['step_ms_largest']:.1f} ms, total {values['step_ms_total']:.1f} ms); "
            f"{values['pullbacks']:.0f} pull-backs (largest "
            f"{values['pullback_ms_largest']:.1f} ms); {values['slips']:.0f} drift slips; "
            f"{values['anchors']:.0f} gap re-anchors"
        )


class _StreamClock:
    """Place the contiguous blocks of one device stream on the shared timeline.

    ``offset = latest possible start (from the arrival) - placed start``. Arrival
    jitter only ever delays blocks, so the placement follows the lower envelope
    of the arrivals:

    * A block that arrived before its placed start (``offset < 0``, which is
      impossible) pulls the stream back at once: the device inserted samples
      or runs fast.
    * A floor that stays a few samples late over the last 50 blocks is a slow
      device clock and is corrected one sample per block.
    * When every block of the confirmation window arrived later than the
      placement by more than a margin over the stream's median jitter (plus
      the drift the slips could still take up: one sample per block since the
      last step, at most a slip window), samples were lost or the device falls
      behind faster than the slips follow: the stream moves forward by the
      whole window minimum. A delivery stall cannot do this: once the
      reader catches up, the last queued block arrives on time. Jitter is
      measured as each block's lateness over the floor of the confirmation
      window that starts with it, which neither a loss nor a device falling
      behind inflates.
    * A delivery gap (a silent loopback) or a reported loss re-anchors the
      next block at its arrival.
    """

    def __init__(self, rate: int, stats: _PlacementStats | None = None) -> None:
        self.rate = rate
        self.stats = stats if stats is not None else _PlacementStats("stream", rate)
        self.next: int | None = None
        self.anchored = False
        self._last_arrival: float | None = None
        self._times: deque[int] = deque()  # arrival index of each recent block
        self._offsets: deque[int] = deque()  # its lateness behind the placement
        self._since_step = 0  # blocks placed since the last step or anchor
        # Lateness over the confirmation-window floor; kept across re-anchors.
        self._jitter: deque[int] = deque(maxlen=_CLOCK_JITTER_BLOCKS)
        self._segment_start = 0
        self._confirm = round(_CLOCK_CONFIRM_S * rate)
        self._deadband = max(1, round(_CLOCK_DEADBAND_S * rate))
        self._reanchor = False

    def request_reanchor(self) -> None:
        """Start a new segment with the next block (samples were lost)."""
        self._reanchor = True

    def place(self, n: int, arrival: float) -> int:
        """Return the timeline index of the first of ``n`` samples that arrived."""
        end = round(arrival * self.rate)
        gap = (
            self._last_arrival is not None
            and arrival - self._last_arrival > _CLOCK_GAP_S + n / self.rate
        )
        self._last_arrival = arrival
        self.anchored = False
        if self.next is None or gap or self._reanchor:
            if self.next is not None:
                self.stats.anchors += 1
            self._reanchor = False
            self.next = end - n
            self._times.clear()
            self._offsets.clear()
            self._since_step = 0
            self._segment_start = end
            self.anchored = True
        offset = end - n - self.next
        if offset < 0:
            self._pull_back(-offset, end)
            offset = 0
        self._times.append(end)
        self._offsets.append(offset)
        while len(self._times) > _CLOCK_WINDOW_BLOCKS and self._times[1] <= end - self._confirm:
            self._times.popleft()
            self._offsets.popleft()
        recent = self._recent(end)
        # The oldest block of the window against the floor of the window that
        # follows it: a device falling behind or a loss inside the window
        # raises only later blocks, so neither inflates the jitter.
        self._jitter.append(recent[0] - min(recent))
        self._since_step += 1
        self._correct(end, recent)
        start = self.next
        self.next = start + n
        self.stats.samples += n
        self.stats.review(arrival)
        return start

    def _correct(self, end: int, recent: list[int]) -> None:
        offsets = self._offsets
        if end - self._times[0] >= self._confirm:
            lowest = min(recent)
            jitter = sorted(self._jitter)[len(self._jitter) // 2]
            slipping = self._deadband + min(self._since_step, _CLOCK_WINDOW_BLOCKS)
            if lowest > slipping + max(self._deadband, _CLOCK_JITTER_MARGIN * jitter):
                self._shift(lowest)
                self._since_step = 0
                self.stats.steps += 1
                self.stats.step_samples += lowest
                self.stats.largest_step = max(self.stats.largest_step, lowest)
                return
        if len(offsets) >= _CLOCK_WINDOW_BLOCKS:
            window = list(offsets)[-_CLOCK_WINDOW_BLOCKS:]
            if min(window) > self._deadband:
                self._shift(1)
                self.stats.slips += 1

    def _recent(self, end: int) -> list[int]:
        """Offsets of the blocks inside the confirmation window (at least a few)."""
        count = 0
        for arrived in reversed(self._times):
            if arrived < end - self._confirm:
                break
            count += 1
        count = min(len(self._offsets), max(count, _CLOCK_CONFIRM_BLOCKS))
        return list(self._offsets)[-count:]

    def _pull_back(self, samples: int, end: int) -> None:
        assert self.next is not None
        self.next -= samples
        self._offsets = deque(offset + samples for offset in self._offsets)
        if end - self._segment_start > self._confirm:  # not the settling of a new anchor
            self.stats.pullbacks += 1
            self.stats.pullback_samples += samples
            self.stats.largest_pullback = max(self.stats.largest_pullback, samples)

    def _shift(self, samples: int) -> None:
        """Move the stream later; blocks from before a step clamp to the new floor."""
        assert self.next is not None
        self.next += samples
        self._offsets = deque(max(0, offset - samples) for offset in self._offsets)


class _StreamPlacer:
    """Resample one mono stream to the processing rate and place its output.

    Resampler output comes in bursts; it is positioned by its cumulative count,
    with the clock's corrections applied as deltas.
    """

    def __init__(
        self,
        in_rate: int,
        out_rate: int,
        dtype: type[np.generic],
        stats: _PlacementStats | None = None,
    ) -> None:
        self.in_rate = in_rate
        self.dtype = dtype
        self.clock = _StreamClock(out_rate, stats)
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
        self._mic_stats = _PlacementStats("microphone", ECHO_SAMPLE_RATE)
        self._pending = np.zeros(0, np.int16)
        self._pending_start = 0
        self._monitor: _ReferenceMonitor | None = None
        # Reference side, guarded by _lock.
        self._ring = _ReferenceRing(_RING_SAMPLES)
        self._reference: _StreamPlacer | None = None
        self._reference_stats = _PlacementStats("reference", ECHO_SAMPLE_RATE)
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
        self._mic_stats = _PlacementStats("microphone", ECHO_SAMPLE_RATE)
        self._reset_capture()
        with self._lock:
            self._reference_stats = _PlacementStats("reference", ECHO_SAMPLE_RATE)
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
        self._mic = _StreamPlacer(self._capture_rate, ECHO_SAMPLE_RATE, np.int16, self._mic_stats)
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
                placer = self._reference = _StreamPlacer(
                    rate, ECHO_SAMPLE_RATE, np.float32, self._reference_stats
                )
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
