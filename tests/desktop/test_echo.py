"""Tests for the Desktop echo stage (``desktop.wakeword.echo``).

No audio devices and no WebRTC library are needed: the echo canceller and the
playback loopback are injected fakes, and time is a synthetic ``perf_counter``
timeline. An optional test runs the real WebRTC canceller when it is installed.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest

from desktop.wakeword import echo
from desktop.wakeword.echo import (
    ECHO_SAMPLE_RATE,
    STATE_ACTIVE,
    STATE_NO_REFERENCE,
    ReferenceBlockCallback,
    WebRtcEchoStage,
)

RATE = ECHO_SAMPLE_RATE
FRAME = echo._FRAME
LEAD = echo._LEAD
GRACE = echo._GRACE
HARD_LIMIT = echo._HARD_LIMIT
T0 = 1000.0
BASE = round(T0 * RATE)  # timeline index of synthetic sample 0


class FakeClock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class RecordingProcessor:
    """Echo canceller double that records every frame it is fed."""

    def __init__(self, rate: int) -> None:
        self.rate = rate
        self.reverse: list[np.ndarray] = []
        self.capture: list[np.ndarray] = []
        self.closed = False

    def process_reverse(self, frame: np.ndarray) -> None:
        self._check(frame)
        self.reverse.append(frame.copy())

    def process_capture(self, frame: np.ndarray) -> np.ndarray:
        self._check(frame)
        assert len(self.reverse) == len(self.capture) + 1, "reference must precede capture"
        self.capture.append(frame.copy())
        return frame.copy()

    def close(self) -> None:
        self.closed = True

    def _check(self, frame: np.ndarray) -> None:
        assert not self.closed
        assert frame.dtype == np.int16
        assert frame.shape == (self.rate // 100,)


class FakeStream:
    def __init__(self, name: str) -> None:
        self._name = name
        self.active = True
        self.closed = False

    @property
    def name(self) -> str:
        return self._name

    def is_active(self) -> bool:
        return self.active

    def close(self) -> None:
        self.closed = True


class FakeLoopback:
    """Loopback backend double; the monitor thread calls it."""

    def __init__(self, endpoint: str | None = "speakers") -> None:
        self.endpoint = endpoint
        self.open_error: BaseException | None = None
        self.detection_error: OSError | None = None
        self.sinks: list[ReferenceBlockCallback] = []
        self.streams: list[FakeStream] = []
        self.open_attempts = 0
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def default_endpoint(self) -> str | None:
        if self.detection_error is not None:
            raise self.detection_error
        return self.endpoint

    def open_default(self, on_block: ReferenceBlockCallback) -> FakeStream:
        self.open_attempts += 1
        if self.open_error is not None:
            raise self.open_error
        stream = FakeStream(f"{self.endpoint} loopback")
        self.sinks.append(on_block)
        self.streams.append(stream)
        return stream

    def stop(self) -> None:
        self.stopped += 1


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.002)


class Harness:
    def __init__(
        self,
        loopback: FakeLoopback | None,
        retry_interval: float = 30.0,
        processor_factory: Callable[[int], echo.EchoProcessor] | None = None,
    ) -> None:
        self.clock = FakeClock()
        self.loopback = loopback
        self.processors: list[RecordingProcessor] = []
        self.stage = WebRtcEchoStage(
            processor_factory=processor_factory or self._processor,
            loopback=loopback,
            clock=self.clock,
            poll_interval=0.005,
            retry_interval=retry_interval,
        )

    @property
    def processor(self) -> RecordingProcessor:
        return self.processors[-1]

    def _processor(self, rate: int) -> RecordingProcessor:
        processor = RecordingProcessor(rate)
        self.processors.append(processor)
        return processor

    def open(self, capture_rate: int = RATE, streams: int = 1) -> None:
        self.stage.open(capture_rate)
        if self.loopback is not None:
            loopback = self.loopback
            wait_for(lambda: len(loopback.sinks) >= streams)

    def push_reference(
        self,
        samples: np.ndarray,
        end: int,
        latency: int = 0,
        *,
        sink: ReferenceBlockCallback | None = None,
        discontinuity: bool = False,
    ) -> None:
        """Deliver int16 reference samples ending at synthetic sample ``end`` as stereo float."""
        assert self.loopback is not None
        mono = samples.astype(np.float32) / np.float32(32767.0)
        arrival = (BASE + end + latency) / RATE
        self.clock.t = max(self.clock.t, arrival)
        (sink or self.loopback.sinks[-1])(
            np.stack((mono, mono), axis=1), RATE, arrival, discontinuity
        )

    def process(self, samples: np.ndarray, end: int, latency: int = 0) -> np.ndarray:
        """Feed microphone samples whose last one is synthetic sample ``end`` (48 kHz units)."""
        arrival = (BASE + end + latency) / RATE
        self.clock.t = max(self.clock.t, arrival)
        return self.stage.process(samples, arrival)

    def tick(self, index: int) -> np.ndarray:
        """Advance the clock to synthetic sample ``index`` and poll without new audio."""
        self.clock.t = (BASE + index) / RATE
        return self.stage.process(np.zeros(0, np.int16), self.clock.t)


@pytest.fixture
def harness() -> Iterator[Harness]:
    subject = Harness(FakeLoopback())
    yield subject
    subject.stage.close()


def noise(count: int, seed: int = 0, scale: float = 4000.0) -> np.ndarray:
    values = np.random.default_rng(seed).standard_normal(count) * scale
    return np.clip(np.round(values), -32767, 32767).astype(np.int16)


def simulate(
    subject: Harness,
    *,
    reference: np.ndarray,
    mic: np.ndarray,
    mic_block: int = 3840,
    reference_block: int = FRAME,
    latency: int = 96,
    jitter: Callable[[int], int] = lambda _index: 0,
    reference_playing: Callable[[int], bool] = lambda _start: True,
) -> np.ndarray:
    """Deliver both streams in arrival order; return all output including the flush."""
    events: list[tuple[int, int, int]] = []
    for index, start in enumerate(range(0, len(reference) - reference_block + 1, reference_block)):
        if reference_playing(start):
            events.append((start + reference_block + latency + jitter(index), 0, start))
    for index, start in enumerate(range(0, len(mic) - mic_block + 1, mic_block)):
        events.append((start + mic_block + latency + jitter(index), 1, start))
    events.sort()
    outputs = []
    for arrival, kind, start in events:
        if kind == 0:
            block = reference[start : start + reference_block]
            subject.push_reference(block, arrival, 0)
        else:
            outputs.append(subject.process(mic[start : start + mic_block], arrival))
    outputs.append(subject.stage.flush())
    return np.concatenate(outputs)


def lagged(signal: np.ndarray, delay: int) -> np.ndarray:
    return np.concatenate((np.zeros(delay, np.int16), signal[: len(signal) - delay]))


def every_third_late(index: int) -> int:
    return 48 if index % 3 == 1 else 0


# ----------------------------------------------------------------------------- framing


def test_frames_reach_the_canceller_as_exact_10ms_int16_and_output_keeps_order(
    harness: Harness,
) -> None:
    harness.open()
    mic = noise(RATE, seed=1)
    outputs = []
    end = 0
    for size in (1000, 777, 4096, 1, 3000, 13, 5000):
        chunk = mic[end : end + size]
        end += size
        outputs.append(harness.process(chunk, end))
    outputs.append(harness.stage.flush())
    output = np.concatenate(outputs)

    assert np.array_equal(output, mic[:end])
    processor = harness.processor
    assert all(frame.shape == (FRAME,) for frame in processor.capture)
    assert all(not frame.any() for frame in processor.reverse)  # nothing played


def test_flush_pads_the_last_partial_frame_and_restarts_timing(harness: Harness) -> None:
    harness.open()
    mic = noise(FRAME * 3 + 100, seed=2)
    held = harness.process(mic, len(mic))
    flushed = harness.stage.flush()

    assert np.array_equal(np.concatenate((held, flushed)), mic)
    assert len(harness.processor.capture) == 4
    assert not harness.processor.capture[-1][100:].any()  # zero padding, never sent back
    # The next block starts a new segment at its own arrival time.
    later = noise(FRAME, seed=3)
    harness.process(later, len(mic) + 10 * RATE)
    assert np.array_equal(harness.stage.flush(), later)


# ----------------------------------------------------------------------------- alignment


def test_reference_window_leads_each_capture_frame_of_a_delayed_echo(harness: Harness) -> None:
    harness.open()
    echo_delay = RATE * 60 // 1000
    reference = noise(RATE * 2, seed=4)
    mic = lagged(reference, echo_delay)

    output = simulate(harness, reference=reference, mic=mic, jitter=every_third_late)

    processor = harness.processor
    assert np.array_equal(output, mic)
    assert len(processor.capture) == len(mic) // FRAME
    pairs = zip(processor.reverse, processor.capture, strict=True)
    for index, (reverse, capture) in enumerate(pairs):
        start = index * FRAME
        assert np.array_equal(capture, mic[start : start + FRAME])
        expected = reference[start + LEAD : start + LEAD + FRAME]
        if len(expected) == FRAME:  # the flush may run past the reference's end
            assert np.array_equal(reverse, expected), f"frame {index}"
            if start >= echo_delay:  # the echo trails its reference by lead + delay
                assert np.array_equal(
                    capture, reference[start - echo_delay : start - echo_delay + FRAME]
                )


def test_resampled_microphone_is_aligned_on_the_shared_timeline(harness: Harness) -> None:
    capture_rate = 16000
    harness.open(capture_rate)
    reference = noise(RATE * 2, seed=5)
    mic16 = noise(capture_rate * 2, seed=6)
    outputs = []
    for start in range(0, len(reference), FRAME):
        harness.push_reference(reference[start : start + FRAME], start + FRAME)
        if (start + FRAME) % 3840 == 0:
            mic_end = (start + FRAME) // 3
            outputs.append(harness.process(mic16[mic_end - 1280 : mic_end], start + FRAME))
    outputs.append(harness.stage.flush())

    assert len(np.concatenate(outputs)) == 3 * len(mic16)  # 48 kHz, resampler drained
    fed = np.concatenate(harness.processor.reverse[:150])
    # The resampler emits in bursts; placement by cumulative count keeps the
    # reference window exactly lead samples ahead of the capture timeline.
    assert np.array_equal(fed[: 150 * FRAME - LEAD], reference[LEAD : 150 * FRAME])


# ----------------------------------------------------------------------------- hold-back


def test_capture_frames_wait_until_the_reference_covers_their_window(harness: Harness) -> None:
    harness.open()
    reference = noise(RATE, seed=7)
    for start in range(0, 4800, FRAME):
        harness.push_reference(reference[start : start + FRAME], start + FRAME)
    mic = noise(RATE, seed=8)

    released = harness.process(mic[:4800], 4800)

    # A frame at c needs the reference through c + lead + 10 ms.
    assert len(released) == (4800 - LEAD - FRAME) // FRAME * FRAME + FRAME
    harness.push_reference(reference[4800:5280], 5280)
    assert len(harness.tick(5280)) == FRAME
    assert all(frame.any() for frame in harness.processor.reverse)


def test_grace_releases_frames_as_silence_once_the_loopback_is_idle(harness: Harness) -> None:
    harness.open()
    mic = noise(RATE, seed=9)

    released = harness.process(mic[:4800], 4800)

    # Silent loopback: a frame goes after its reference window plus the grace.
    waiting = LEAD + FRAME + GRACE
    assert len(released) == ((4800 - waiting) // FRAME + 1) * FRAME
    assert len(harness.tick(4800 + FRAME)) == FRAME
    assert not any(frame.any() for frame in harness.processor.reverse)


def test_a_lagging_reference_holds_frames_no_longer_than_the_hard_limit(
    harness: Harness,
) -> None:
    harness.open()
    reference = noise(RATE * 3, seed=10)
    mic = noise(RATE * 3, seed=11)
    released = 0
    waited_beyond_grace = False
    block = 0
    for end in range(3840, len(mic) + 1, 3840):
        # A loopback at half speed: blocks keep arriving (the loopback is not
        # idle) while their contiguous placement falls further behind.
        while (block + 1) * 2 * FRAME <= end:
            samples = reference[block * FRAME : (block + 1) * FRAME]
            harness.push_reference(samples, (block + 1) * 2 * FRAME)
            block += 1
        released += len(harness.process(mic[end - 3840 : end], end))

        def frames_due(wait: int, end: int = end) -> int:
            return max(0, (end - (LEAD + FRAME + wait)) // FRAME + 1) * FRAME

        assert released >= frames_due(HARD_LIMIT)
        waited_beyond_grace = waited_beyond_grace or released < frames_due(GRACE)
    assert waited_beyond_grace
    assert released == frames_due(HARD_LIMIT)


def test_capture_passes_through_unchanged_without_a_reference() -> None:
    subject = Harness(None)
    subject.stage.open(16000)
    mic = noise(16000, seed=12)
    first = subject.stage.process(mic[:8000], T0)
    second = subject.stage.process(mic[8000:], T0 + 0.5)
    rest = subject.stage.flush()

    assert subject.stage.state == STATE_NO_REFERENCE
    assert len(first) > 0  # nothing is held back for a reference
    assert len(first) + len(second) + len(rest) == 3 * len(mic)
    assert subject.processor.capture == []
    subject.stage.close()


# ----------------------------------------------------------------------------- gaps and drift


def test_reference_realigns_after_a_silence_gap(harness: Harness) -> None:
    harness.open()
    reference = noise(RATE * 3, seed=13)
    for silent in range(RATE, 2 * RATE):
        reference[silent] = 0
    mic = lagged(reference, 2880)

    output = simulate(
        harness,
        reference=reference,
        mic=mic,
        jitter=every_third_late,
        reference_playing=lambda start: not RATE <= start < 2 * RATE,
    )

    processor = harness.processor
    assert np.array_equal(output, mic[: len(mic) // 3840 * 3840])
    for index, reverse in enumerate(processor.reverse):
        start = index * FRAME + LEAD
        expected = reference[start : start + FRAME]
        if len(expected) == FRAME:
            assert np.array_equal(reverse, expected), f"frame {index}"
    placer = harness.stage._reference
    assert placer is not None
    assert placer.clock.resyncs >= 1


def test_overflow_discontinuity_reanchors_the_reference(harness: Harness) -> None:
    harness.open()
    reference = noise(RATE, seed=14)
    for start in range(0, 9600, FRAME):
        harness.push_reference(reference[start : start + FRAME], start + FRAME)
    # 20 ms of loopback samples were lost: the next block arrives 30 ms after the last.
    harness.push_reference(reference[10560:11040], 11040, discontinuity=True)

    ring = harness.stage._ring
    assert ring.written_until == BASE + 11040
    assert np.array_equal(ring.read(BASE + 10560, FRAME), reference[10560:11040])
    assert not ring.read(BASE + 9600, 960).any()  # the lost span is silence


@pytest.mark.parametrize("ppm", [200.0, -200.0])
def test_clock_corrects_device_drift_one_sample_at_a_time(ppm: float) -> None:
    clock = echo._StreamClock(RATE)
    block = FRAME
    seconds = 60
    errors = []
    rng = np.random.default_rng(15)
    for index in range(seconds * 100):
        true_start = (index * block) / (1 + ppm * 1e-6)  # perf-timeline samples
        late = 0.0 if index % 4 == 0 else rng.uniform(0, 48)
        arrival = (BASE + true_start + block / (1 + ppm * 1e-6) + late) / RATE
        placed = clock.place(block, arrival)
        errors.append(placed - (BASE + true_start))

    uncorrected = abs(ppm) * 1e-6 * seconds * RATE
    assert uncorrected > 500
    assert max(abs(error) for error in errors) <= 2 * echo._CLOCK_SLIP_SAMPLES
    assert clock.slips > 100


def test_clock_reanchors_after_a_gap_and_after_a_persistent_jump() -> None:
    clock = echo._StreamClock(RATE)
    end = 0

    def deliver(delay: int = 0) -> int:
        nonlocal end
        end += FRAME
        return clock.place(FRAME, (BASE + end + delay) / RATE)

    for _ in range(100):
        deliver()
    end += RATE // 2  # 500 ms without blocks (silent loopback)
    assert deliver() == BASE + end - FRAME
    assert clock.resyncs == 1

    for _ in range(60):
        deliver()
    for _ in range(60):  # from now on every block arrives 50 ms after its samples
        placed = deliver(2400)
    assert clock.resyncs == 2
    assert placed == BASE + end + 2400 - FRAME


def test_reference_ring_keeps_only_its_window() -> None:
    ring = echo._ReferenceRing(1000)
    ring.write(0, np.full(600, 1, np.int16))
    ring.write(900, np.full(300, 2, np.int16))  # 300 silent samples in between

    assert ring.written_until == 1200
    window = ring.read(0, 1300)
    assert not window[:200].any()  # left the window
    assert (window[200:600] == 1).all()
    assert not window[600:900].any()
    assert (window[900:1200] == 2).all()
    assert not window[1200:].any()  # not written yet
    ring.write(5000, np.full(10, 3, np.int16))
    assert not ring.read(1000, 200).any()


# ----------------------------------------------------------------------------- lifecycle


def test_reset_forgets_held_audio_but_keeps_the_canceller(harness: Harness) -> None:
    harness.open()
    harness.process(noise(4800, seed=16), 4800)
    harness.stage.reset()

    later = noise(FRAME, seed=17)
    released = harness.process(later, 3 * RATE)
    assert np.array_equal(np.concatenate((released, harness.stage.flush())), later)
    assert len(harness.processors) == 1
    assert not harness.processor.closed


def test_close_releases_everything_and_open_starts_again(harness: Harness) -> None:
    loopback = harness.loopback
    assert loopback is not None
    harness.open()
    first_stream = loopback.streams[0]

    harness.stage.close()

    assert first_stream.closed
    assert harness.processor.closed
    assert loopback.stopped == 1
    assert not any(thread.name == "vbot-echo-reference" for thread in threading.enumerate())
    with pytest.raises(RuntimeError):
        harness.stage.process(noise(FRAME), T0)
    assert len(harness.stage.flush()) == 0

    harness.open(16000, streams=2)
    assert len(harness.processors) == 2
    assert harness.stage.state == STATE_ACTIVE
    output = harness.process(noise(16000, seed=18), 10 * RATE)
    assert len(output) + len(harness.stage.flush()) == RATE


def test_late_blocks_from_a_replaced_stream_are_ignored(harness: Harness) -> None:
    loopback = harness.loopback
    assert loopback is not None
    harness.open()
    old_sink = loopback.sinks[0]
    loopback.endpoint = "headphones"
    wait_for(lambda: len(loopback.sinks) == 2)

    harness.push_reference(noise(FRAME, seed=19), FRAME, sink=old_sink)

    assert harness.stage._ring.written_until is None
    assert loopback.streams[0].closed
    assert not loopback.streams[1].closed


# ----------------------------------------------------------------------------- state


def test_state_follows_the_default_playback_device() -> None:
    loopback = FakeLoopback(endpoint=None)
    subject = Harness(loopback)
    subject.stage.open(RATE)
    try:
        wait_for(lambda: subject.stage.state == STATE_NO_REFERENCE)
        assert loopback.open_attempts == 0

        loopback.endpoint = "speakers"
        wait_for(lambda: subject.stage.state == STATE_ACTIVE and len(loopback.streams) == 1)

        loopback.endpoint = "headset"  # default device changed: reopen on the new one
        wait_for(lambda: len(loopback.streams) == 2)
        assert loopback.streams[0].closed
        assert subject.stage.state == STATE_ACTIVE

        loopback.streams[1].active = False  # device lost without a default change
        wait_for(lambda: len(loopback.streams) == 3)

        loopback.endpoint = None
        wait_for(lambda: subject.stage.state == STATE_NO_REFERENCE)
        assert loopback.streams[2].closed
    finally:
        subject.stage.close()


def test_missing_loopback_endpoint_reports_no_reference_and_passes_audio_through() -> None:
    loopback = FakeLoopback()
    loopback.open_error = LookupError("no loopback analogue")
    subject = Harness(loopback)
    subject.stage.open(RATE)
    try:
        wait_for(lambda: subject.stage.state == STATE_NO_REFERENCE)
        mic = noise(1000, seed=20)
        assert np.array_equal(subject.stage.process(mic, T0), mic)
        attempts = loopback.open_attempts
        time.sleep(0.05)
        assert loopback.open_attempts == attempts  # no retry until the device changes

        loopback.open_error = None
        loopback.endpoint = "usb speakers"
        wait_for(lambda: subject.stage.state == STATE_ACTIVE and len(loopback.streams) == 1)
    finally:
        subject.stage.close()


def test_failed_open_is_retried_after_the_retry_interval() -> None:
    loopback = FakeLoopback()
    loopback.open_error = OSError("device busy")
    subject = Harness(loopback, retry_interval=0.05)
    subject.stage.open(RATE)
    try:
        wait_for(lambda: subject.stage.state == STATE_NO_REFERENCE)
        loopback.open_error = None
        wait_for(lambda: len(loopback.streams) == 1 and subject.stage.state == STATE_ACTIVE)
    finally:
        subject.stage.close()


def test_losing_the_reference_releases_held_frames_unprocessed(harness: Harness) -> None:
    loopback = harness.loopback
    assert loopback is not None
    harness.open()
    reference = noise(4800, seed=21)
    for start in range(0, 4800, FRAME):
        harness.push_reference(reference[start : start + FRAME], start + FRAME)
    mic = noise(4800, seed=22)
    processed = harness.process(mic, 4800)
    assert 0 < len(processed) < len(mic)

    loopback.endpoint = None
    wait_for(lambda: harness.stage.state == STATE_NO_REFERENCE)
    rest = harness.tick(4800)
    assert np.array_equal(np.concatenate((processed, rest)), mic)


def test_undetectable_device_changes_keep_the_first_loopback() -> None:
    loopback = FakeLoopback()
    loopback.detection_error = OSError("no Core Audio")
    subject = Harness(loopback)
    subject.stage.open(RATE)
    try:
        wait_for(lambda: len(loopback.streams) == 1)
        time.sleep(0.05)
        assert len(loopback.streams) == 1
        assert subject.stage.state == STATE_ACTIVE
    finally:
        subject.stage.close()


# ----------------------------------------------------------------------------- libraries


class FakeFfiHandle:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class FakeApm:
    instances: list[FakeApm] = []

    def __init__(self, **options: bool) -> None:
        self.options = options
        self._ffi_handle = FakeFfiHandle()
        self.calls: list[str] = []
        FakeApm.instances.append(self)

    def process_reverse_stream(self, frame: Any) -> None:
        self.calls.append("reverse")

    def process_stream(self, frame: Any) -> None:
        self.calls.append("capture")


class FakeAudioFrame:
    def __init__(self, data: bytearray, rate: int, channels: int, samples: int) -> None:
        assert (rate, channels, samples, len(data)) == (RATE, 1, FRAME, 2 * FRAME)


@pytest.fixture
def fake_livekit(monkeypatch: pytest.MonkeyPatch) -> list[FakeApm]:
    FakeApm.instances = []
    rtc = types.ModuleType("livekit.rtc")
    rtc.AudioProcessingModule = FakeApm  # type: ignore[attr-defined]
    rtc.AudioFrame = FakeAudioFrame  # type: ignore[attr-defined]
    package = types.ModuleType("livekit")
    package.rtc = rtc  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "livekit", package)
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)
    monkeypatch.setattr(echo, "_default_loopback", lambda: None)
    return FakeApm.instances


def test_factory_returns_none_without_the_webrtc_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "livekit", None)
    monkeypatch.setitem(sys.modules, "livekit.rtc", None)

    assert echo.create_echo_stage() is None


def test_factory_initialises_the_library_once_and_builds_a_stage(
    fake_livekit: list[FakeApm],
) -> None:
    stage = echo.create_echo_stage()

    assert isinstance(stage, WebRtcEchoStage)
    assert stage.rate == RATE
    assert len(fake_livekit) == 1 and fake_livekit[0]._ffi_handle.disposed
    assert fake_livekit[0].options == {"echo_cancellation": True, "high_pass_filter": True}


def test_webrtc_processor_validates_frames_and_releases_its_handle(
    fake_livekit: list[FakeApm],
) -> None:
    processor = echo._LiveKitProcessor(RATE)
    frame = noise(FRAME, seed=23)

    processor.process_reverse(frame)
    assert np.array_equal(processor.process_capture(frame), frame)
    for bad in (frame[:-1], frame.astype(np.float32), np.stack((frame, frame))):
        with pytest.raises(ValueError):
            processor.process_capture(bad)
    assert fake_livekit[-1].calls == ["reverse", "capture"]
    processor.close()
    assert fake_livekit[-1]._ffi_handle.disposed


class FakePyAudioStream:
    def __init__(self, **options: Any) -> None:
        self.options = options
        self.stopped = False
        self.closed = False

    def is_active(self) -> bool:
        return not self.stopped

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakePyAudio:
    instances: list[FakePyAudio] = []
    loopback: dict[str, Any] | Exception = {}

    def __init__(self) -> None:
        self.terminated = False
        self.stream: FakePyAudioStream | None = None
        FakePyAudio.instances.append(self)

    def get_default_wasapi_loopback(self) -> dict[str, Any]:
        if isinstance(FakePyAudio.loopback, Exception):
            raise FakePyAudio.loopback
        return FakePyAudio.loopback

    def open(self, **options: Any) -> FakePyAudioStream:
        self.stream = FakePyAudioStream(**options)
        return self.stream

    def terminate(self) -> None:
        self.terminated = True


@pytest.fixture
def fake_pyaudiowpatch(monkeypatch: pytest.MonkeyPatch) -> type[FakePyAudio]:
    FakePyAudio.instances = []
    FakePyAudio.loopback = {
        "index": 24,
        "name": "Speakers [Loopback]",
        "defaultSampleRate": 44100.0,
        "maxInputChannels": 2,
    }
    module = types.ModuleType("pyaudiowpatch")
    module.PyAudio = FakePyAudio  # type: ignore[attr-defined]
    module.paFloat32 = 1  # type: ignore[attr-defined]
    module.paContinue = 0  # type: ignore[attr-defined]
    module.paInputOverflow = 2  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", module)
    return FakePyAudio


def test_wasapi_loopback_opens_10ms_float_callbacks_and_times_them(
    fake_pyaudiowpatch: type[FakePyAudio],
) -> None:
    clock = FakeClock(T0 + 5)
    backend = echo._WasapiLoopbackBackend(clock=clock)
    blocks: list[tuple[Any, ...]] = []

    stream = backend.open_default(lambda *block: blocks.append(block))

    audio = fake_pyaudiowpatch.instances[0]
    assert audio.stream is not None
    options = audio.stream.options
    assert options["frames_per_buffer"] == 441 and options["rate"] == 44100
    assert options["input_device_index"] == 24 and options["channels"] == 2
    assert stream.name.startswith("Speakers (44100 Hz")
    callback = options["stream_callback"]
    samples = np.arange(8, dtype=np.float32)
    assert callback(samples.tobytes(), 4, {}, 2) == (None, 0)
    block, rate, arrival, discontinuity = blocks[0]
    assert block.shape == (4, 2) and rate == 44100 and arrival == T0 + 5 and discontinuity
    assert callback(samples.tobytes(), 3, {}, 0) == (None, 0)  # malformed size: dropped
    assert len(blocks) == 1

    stream.close()
    assert audio.stream.stopped and audio.stream.closed and audio.terminated


def test_wasapi_loopback_callback_survives_a_failing_sink(
    fake_pyaudiowpatch: type[FakePyAudio],
) -> None:
    def failing(*_block: Any) -> None:
        raise RuntimeError("sink broke")

    backend = echo._WasapiLoopbackBackend()
    backend.open_default(failing)
    stream = fake_pyaudiowpatch.instances[0].stream
    assert stream is not None
    callback = stream.options["stream_callback"]

    assert callback(np.zeros(4, np.float32).tobytes(), 2, {}, 0) == (None, 0)


def test_wasapi_loopback_without_an_endpoint_terminates_portaudio(
    fake_pyaudiowpatch: type[FakePyAudio],
) -> None:
    fake_pyaudiowpatch.loopback = LookupError("no analogue")

    with pytest.raises(LookupError):
        echo._WasapiLoopbackBackend().open_default(lambda *_block: None)
    assert fake_pyaudiowpatch.instances[0].terminated


# ----------------------------------------------------------------------------- real WebRTC


def test_real_webrtc_canceller_removes_a_synthetic_echo() -> None:
    pytest.importorskip("livekit.rtc")
    subject = Harness(FakeLoopback(), processor_factory=echo._LiveKitProcessor)
    subject.open()
    try:
        seconds = 8
        rng = np.random.default_rng(24)
        far = rng.standard_normal(RATE * seconds)
        far *= np.repeat(rng.uniform(0.2, 1.0, seconds * 5), RATE // 5)  # speech-like level changes
        reference = np.clip(np.round(far * 3000), -32767, 32767).astype(np.int16)
        taps = rng.standard_normal(RATE // 50) * np.exp(-np.arange(RATE // 50) / (RATE * 0.004))
        taps /= np.abs(taps).sum()
        room = np.convolve(reference.astype(np.float64), taps)[: len(reference)]
        delay = RATE * 80 // 1000
        echo_signal = np.concatenate((np.zeros(delay), room[: len(room) - delay])) * 0.5
        mic = np.clip(np.round(echo_signal), -32767, 32767).astype(np.int16)

        output = simulate(subject, reference=reference, mic=mic, jitter=every_third_late)

        tail = slice(RATE * (seconds - 3), RATE * seconds)
        mic_power = float(np.mean(mic[tail].astype(np.float64) ** 2))
        out_power = float(np.mean(output[tail].astype(np.float64) ** 2))
        erle = 10 * np.log10(mic_power / max(out_power, 1e-9))
        assert erle > 20, f"ERLE {erle:.1f} dB"
    finally:
        subject.stage.close()
