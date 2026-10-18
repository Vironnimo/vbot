"""Stream-clock placement and the ring's zero-filled rolling window contract."""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np
import pytest

from desktop.speech import _echo_alignment as alignment

RATE = 48000
FRAME = RATE // 100
BASE = 1000 * RATE


DEADBAND = round(alignment._CLOCK_DEADBAND_S * RATE)


CONFIRM = round(alignment._CLOCK_CONFIRM_S * RATE)


@pytest.mark.parametrize("ppm", [200.0, -200.0])
def test_clock_corrects_device_drift_one_sample_at_a_time(ppm: float) -> None:
    clock = alignment._StreamClock(RATE)
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
    assert max(abs(error) for error in errors) <= 2 * DEADBAND
    stats = clock.stats
    assert stats.steps == 0  # drift is never mistaken for lost samples
    if ppm > 0:  # a fast device: early blocks pull it back a sample at a time
        assert stats.pullbacks > 100 and stats.largest_pullback <= 2
    else:
        assert stats.slips > 100


def test_clock_reanchors_after_a_gap_and_after_a_persistent_jump() -> None:
    clock = alignment._StreamClock(RATE)
    end = 0

    def deliver(delay: int = 0) -> int:
        nonlocal end
        end += FRAME
        return clock.place(FRAME, (BASE + end + delay) / RATE)

    for _ in range(100):
        deliver()
    end += RATE // 2  # 500 ms without blocks (silent loopback)
    assert deliver() == BASE + end - FRAME
    assert clock.stats.anchors == 1

    for _ in range(60):
        deliver()
    for _ in range(60):  # from now on every block arrives 50 ms after its samples
        placed = deliver(2400)
    assert (clock.stats.anchors, clock.stats.steps) == (1, 1)
    assert placed == BASE + end + 2400 - FRAME


MIC_BLOCK = 1920  # 40 ms blocks exercise the clock's slower drift-correction limit


def deliver_device(
    *,
    seconds: float = 30.0,
    ppm: float = 0.0,
    changes: dict[int, int] | None = None,
    late: Callable[[int], float] | None = None,
) -> tuple[alignment._StreamClock, np.ndarray]:
    """Feed a simulated capture device to a clock; return it and each block's placement error.

    The device clock runs ``ppm`` off the timeline. ``changes`` maps a block
    index to device samples lost (positive) or inserted (negative) just before
    that block. Every fourth block arrives on time and the others up to 2 ms
    late, unless ``late`` gives each block's delay in samples.
    """
    clock = alignment._StreamClock(RATE)
    rng = np.random.default_rng(31)
    scale = 1 + ppm * 1e-6
    skipped = 0
    errors = []
    for index in range(round(seconds * RATE) // MIC_BLOCK):
        skipped += (changes or {}).get(index, 0)
        true_start = (index * MIC_BLOCK + skipped) / scale
        delay = late(index) if late else 0.0 if index % 4 == 0 else rng.uniform(0, 96)
        arrival = (BASE + true_start + MIC_BLOCK / scale + delay) / RATE
        errors.append(clock.place(MIC_BLOCK, arrival) - (BASE + true_start))
    return clock, np.array(errors)


def test_clock_undoes_lost_microphone_chunks_after_the_confirmation_window() -> None:
    lost = RATE * 30 // 1000
    period = 3 * RATE // MIC_BLOCK  # a 30 ms chunk vanishes every 3 s
    changes = dict.fromkeys(range(period, 30 * RATE // MIC_BLOCK, period), lost)

    clock, errors = deliver_device(changes=changes)

    settle = -(-CONFIRM // MIC_BLOCK) + 2
    for index in changes:
        assert errors[index] == -lost  # placed early by the loss until it is confirmed,
        assert np.abs(errors[index + settle : index + period]).max() <= 1  # then back on time
    assert errors.max() <= 1  # never overshoots
    assert clock.stats.steps == len(changes)
    assert clock.stats.step_samples == len(changes) * lost


@pytest.mark.parametrize(("ppm", "bound_ms"), [(-10000.0, 4.0), (10000.0, 2.0)])
def test_clock_bounds_the_error_of_a_device_one_percent_off(ppm: float, bound_ms: float) -> None:
    clock, errors = deliver_device(ppm=ppm)

    # A slow device falls behind continuously and is corrected in small steps
    # whenever the floor of a confirmation window lies beyond the jitter
    # margin: the placement trails by about the drift across one window (3 ms
    # at 1 %) and never jumps by more than the margin. A fast one delivers
    # early: every on-time block pulls it back.
    settled = errors[2 * CONFIRM // MIC_BLOCK :]
    assert np.abs(settled).max() <= bound_ms * RATE / 1000
    assert (clock.stats.steps > 0) == (ppm < 0)
    assert max(clock.stats.largest_step, clock.stats.largest_pullback) <= 2.5 * RATE / 1000


@pytest.mark.parametrize("ppm", [-450.0, 450.0])
def test_clock_follows_a_steady_device_one_sample_at_a_time(ppm: float) -> None:
    # Without jitter the step margin is at its smallest, and at 40 ms blocks
    # this drift is close to what one-sample corrections can follow (520 ppm).
    clock, errors = deliver_device(seconds=60, ppm=ppm, late=lambda index: 0.0)

    stats = clock.stats
    assert (stats.steps, stats.anchors) == (0, 0)  # drift is never mistaken for lost samples
    # The placement trails a slow device by up to the drift across a slip
    # window; that delay is steady (the canceller absorbs a constant delay).
    assert np.abs(errors).max() <= DEADBAND + alignment._CLOCK_WINDOW_BLOCKS
    assert np.ptp(errors[-500:]) <= 2
    assert (stats.slips if ppm < 0 else stats.pullbacks) > 1000
    assert stats.largest_pullback <= 1


def test_clock_pulls_back_at_once_when_the_device_inserts_samples() -> None:
    inserted = RATE * 20 // 1000  # packet-loss concealment adds 20 ms every 2 s
    changes = dict.fromkeys(range(50, 30 * RATE // MIC_BLOCK, 50), -inserted)

    clock, errors = deliver_device(changes=changes)

    assert np.abs(errors).max() <= 96  # never off by more than one block's lateness
    assert clock.stats.steps == 0
    assert clock.stats.pullback_samples >= len(changes) * (inserted - 96)


def test_clock_does_not_chase_delivery_bursts() -> None:
    stall = RATE // 10  # every 2 s the reader stalls 100 ms, then gets the queue at once

    def late(index: int) -> float:
        phase = ((index + 1) * MIC_BLOCK - RATE) % (2 * RATE)
        return float(stall - phase) if phase < stall else 0.0

    clock, errors = deliver_device(late=late)

    assert late(25) > 0  # the stalls are there
    assert not errors.any()
    assert (clock.stats.steps, clock.stats.slips, clock.stats.anchors) == (0, 0, 0)


@pytest.mark.parametrize(("lag_ms", "reanchor"), [(80, "steps"), (150, "anchors")])
def test_clock_recovers_on_its_own_from_a_wrong_reanchor(lag_ms: int, reanchor: str) -> None:
    lag = RATE * lag_ms // 1000  # the reader lags for a second, then catches up

    def late(index: int) -> float:
        return float(lag) if 100 <= index < 125 else 0.0

    clock, errors = deliver_device(late=late)

    # A second of uniformly late blocks is indistinguishable from lost samples
    # (a lag beyond the gap limit even starts a new segment); the first
    # on-time block afterwards proves otherwise and pulls the stream back.
    assert getattr(clock.stats, reanchor) == 1
    assert clock.stats.largest_pullback == lag
    assert not errors[125:].any()


def test_clock_reports_lost_samples_once_and_summarises_at_most_once_a_minute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=alignment.logger.name)
    seconds, period = 200, 3 * RATE // MIC_BLOCK
    lost = RATE * 30 // 1000
    changes = dict.fromkeys(range(period, seconds * RATE // MIC_BLOCK, period), lost)

    deliver_device(seconds=seconds, changes=changes)

    reported = timing_logs(caplog, logging.INFO)
    assert len(reported) == 1
    assert "1 lost-sample re-anchors (largest 30.0 ms" in reported[0]
    assert 1 <= len(timing_logs(caplog, logging.DEBUG)) <= seconds // 60


def test_clock_stays_quiet_about_a_healthy_device(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=alignment.logger.name)

    clock, _errors = deliver_device(seconds=120, ppm=-200.0)

    assert clock.stats.slips > 0
    assert not timing_logs(caplog, logging.INFO) and not timing_logs(caplog, logging.DEBUG)


def test_reference_ring_keeps_only_its_window() -> None:
    ring = alignment._ReferenceRing(1000)
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


def timing_logs(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and "timing" in record.getMessage()
    ]
