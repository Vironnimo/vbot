"""Signal-independent stream clocks, resampling and zero-filled reference storage."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import numpy as np

logger = logging.getLogger("vbot.desktop.speech.echo")
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
