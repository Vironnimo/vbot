"""Microphone capture: device selection, projections, gaps, recovery and the echo stage."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from desktop.wakeword._microphones import (
    MicrophoneUnavailableError,
    _candidate_device_indices,
    _select_capture_format,
    close_input_stream,
    list_microphones,
    open_input_stream,
    refresh_microphone_devices,
)
from desktop.wakeword.capture import (
    AudioBlock,
    AudioCapture,
    CaptureGap,
    CaptureStatus,
    CaptureSubscription,
    EchoStage,
    EchoStagePool,
)
from desktop.wakeword.config import MicrophoneSelection
from tests.desktop.voice_fakes import (
    FakeEchoStage,
    FakeSoundDevice,
    Overflow,
    tone,
    wait_until,
)

BLOCK_16K = 640  # samples of one 40 ms block at 16 kHz
BLOCK_48K = 1920


@dataclass
class Running:
    capture: AudioCapture
    stop: threading.Event
    statuses: list[CaptureStatus] = field(default_factory=list)

    def states(self) -> list[tuple[str, str | None]]:
        return [(status.state, status.error_code) for status in list(self.statuses)]

    def has_status(self) -> bool:
        return bool(self.statuses)


@pytest.fixture
def start_capture() -> Iterator[Callable[..., Running]]:
    running: list[Running] = []

    def start(
        sd: FakeSoundDevice,
        *,
        echo_cancellation: bool = False,
        echo_stages: EchoStagePool | None = None,
        microphone: MicrophoneSelection | None = None,
        subscribe: Callable[[AudioCapture], Any] | None = None,
        **kwargs: Any,
    ) -> Running:
        stop = threading.Event()
        statuses: list[CaptureStatus] = []
        capture = AudioCapture(
            microphone=microphone,
            echo_cancellation=echo_cancellation,
            echo_stages=echo_stages,
            on_status=statuses.append,
            stop_event=stop,
            backend=sd,
            reconnect_interval=kwargs.pop("reconnect_interval", 0.05),
            **kwargs,
        )
        run = Running(capture, stop, statuses)
        running.append(run)
        if subscribe is not None:
            subscribe(capture)
        capture.start()
        return run

    yield start
    for run in running:
        run.stop.set()
        assert run.capture.join(5), "capture thread did not stop"


def read_items(
    subscription: CaptureSubscription, count: int, timeout: float = 5.0
) -> list[AudioBlock | CaptureGap]:
    items: list[AudioBlock | CaptureGap] = []
    deadline = time.monotonic() + timeout
    while len(items) < count:
        assert time.monotonic() < deadline, f"only {len(items)} of {count} items arrived"
        item = subscription.read(0.1)
        if item is not None:
            items.append(item)
    return items


def samples(block: AudioBlock | CaptureGap, *, projection: str = "recording") -> np.ndarray:
    assert isinstance(block, AudioBlock)
    return np.frombuffer(getattr(block, projection), dtype=np.int16)


def subscribe_to(target: list[CaptureSubscription], **kwargs: Any) -> Callable[..., None]:
    def subscribe(capture: AudioCapture) -> None:
        target.append(capture.subscribe(max_seconds=kwargs.get("max_seconds", 30.0)))

    return subscribe


def pool_of(*stages: EchoStage | None) -> tuple[EchoStagePool, list[int]]:
    """A pool whose factory hands out ``stages`` in order and counts its calls."""
    calls: list[int] = []
    queue = list(stages)

    def factory() -> EchoStage | None:
        calls.append(1)
        return queue.pop(0)

    return EchoStagePool(factory), calls


# -- Projections ---------------------------------------------------------------------


def test_blocks_carry_the_detection_projection_and_the_recording(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice(default_samplerate=16000)
    signal = tone(0.2, 16000)
    sd.feed(signal)
    subscriptions: list[CaptureSubscription] = []

    run = start_capture(sd, subscribe=subscribe_to(subscriptions))
    blocks = read_items(subscriptions[0], 5)

    assert [block.index for block in blocks if isinstance(block, AudioBlock)] == [0, 1, 2, 3, 4]
    for block in blocks:
        assert isinstance(block, AudioBlock)
        assert block.recording_rate == 16000
        assert block.pcm16 == block.recording
        assert block.duration == pytest.approx(0.04)
    assert np.array_equal(np.concatenate([samples(block) for block in blocks]), signal)
    first = run.statuses[0]
    assert first.state == "capturing"
    assert first.echo_state == "off"
    assert first.microphone is not None
    assert first.microphone.to_status() == {
        "index": 0,
        "name": "Mic",
        "host_api": "Windows WASAPI",
        "sample_rate": 16000,
    }


def test_a_48k_microphone_is_captured_natively_and_projected_continuously(
    start_capture: Callable[..., Running],
) -> None:
    soxr = pytest.importorskip("soxr")
    sd = FakeSoundDevice(default_samplerate=48000)
    t = np.arange(12 * BLOCK_48K)
    signal = (3000 * np.sin(2 * np.pi * 1000 * t / 48000)).astype(np.int16)
    sd.feed(signal)
    subscriptions: list[CaptureSubscription] = []

    start_capture(sd, subscribe=subscribe_to(subscriptions))
    blocks = read_items(subscriptions[0], len(signal) // BLOCK_48K)

    assert {block.recording_rate for block in blocks if isinstance(block, AudioBlock)} == {48000}
    assert np.array_equal(np.concatenate([samples(block) for block in blocks]), signal)
    projected = np.concatenate([samples(block, projection="pcm16") for block in blocks])
    # One stateful resampler across blocks: the stream equals a one-shot resample.
    reference = soxr.resample(signal, 48000, 16000).astype(np.int16)
    assert len(projected) > len(reference) * 0.95
    assert np.abs(projected.astype(int) - reference[: len(projected)].astype(int)).max() <= 2


def test_the_detection_projection_is_anti_aliased(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice(default_samplerate=48000)
    t = np.arange(48000 // 2)
    # 12 kHz lies above the 8 kHz Nyquist limit of the 16 kHz projection.
    sd.feed((3000 * np.sin(2 * np.pi * 12000 * t / 48000)).astype(np.int16))
    subscriptions: list[CaptureSubscription] = []

    start_capture(sd, subscribe=subscribe_to(subscriptions))
    blocks = read_items(subscriptions[0], len(t) // BLOCK_48K)

    projected = np.concatenate([samples(block, projection="pcm16") for block in blocks])
    assert np.sqrt(np.mean(projected.astype(float) ** 2)) < 30  # naive decimation: ~2100


def test_a_float32_microphone_is_converted_to_int16(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice(default_samplerate=16000, dtypes=("float32",))
    signal = tone(0.08, 16000)
    sd.feed(signal)
    subscriptions: list[CaptureSubscription] = []

    start_capture(sd, subscribe=subscribe_to(subscriptions))
    blocks = read_items(subscriptions[0], 2)

    assert sd.streams[0].dtype == "float32"
    recorded = np.concatenate([samples(block) for block in blocks])
    assert np.abs(recorded.astype(int) - signal.astype(int)).max() <= 1


# -- Gaps and recovery -------------------------------------------------------------------


def test_an_overflow_delivers_a_gap_and_keeps_reading(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    sd.feed(tone(0.08, 16000), Overflow(), tone(0.08, 16000))
    subscriptions: list[CaptureSubscription] = []

    start_capture(sd, subscribe=subscribe_to(subscriptions))
    items = read_items(subscriptions[0], 5)

    assert [type(item).__name__ for item in items] == [
        "AudioBlock",
        "AudioBlock",
        "CaptureGap",
        "AudioBlock",
        "AudioBlock",
    ]
    assert items[2] == CaptureGap("overflow")
    assert len(sd.streams) == 1


def test_a_failed_read_reopens_the_stream(start_capture: Callable[..., Running]) -> None:
    sd = FakeSoundDevice()
    sd.feed(tone(0.04, 16000), OSError("read failed"), tone(0.04, 16000))
    subscriptions: list[CaptureSubscription] = []

    run = start_capture(sd, subscribe=subscribe_to(subscriptions))
    items = read_items(subscriptions[0], 3)

    assert isinstance(items[0], AudioBlock)
    assert items[1] == CaptureGap("read_failed")
    assert isinstance(items[2], AudioBlock)
    assert len(sd.streams) == 2
    assert sd.streams[0].closed
    assert run.states()[:2] == [("capturing", None), ("capturing", None)]


def test_repeated_read_failures_disconnect_until_the_microphone_returns(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    sd.feed(OSError("gone"), OSError("gone"), OSError("gone"))

    run = start_capture(sd)
    wait_until(lambda: len(run.statuses) >= 5)

    assert run.states()[:5] == [
        ("capturing", None),
        ("capturing", None),
        ("capturing", None),
        ("disconnected", "microphone_read_failed"),
        ("capturing", None),
    ]
    # PortAudio is refreshed only while no Voice stream is open.
    refresh = sd.events.index("terminate")
    assert sd.events[refresh - 1] == "stream.close"
    assert sd.events[refresh + 1 : refresh + 3] == ["initialize", "stream.open"]


def test_a_microphone_missing_at_start_is_retried_after_a_refresh(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    sd.open_failures = 2

    run = start_capture(sd)
    wait_until(lambda: len(run.statuses) >= 2)

    assert run.states()[:2] == [
        ("disconnected", "microphone_unavailable"),
        ("capturing", None),
    ]
    assert sd.events[:7] == [
        "open_failed",
        "terminate",
        "initialize",
        "open_failed",
        "terminate",
        "initialize",
        "stream.open",
    ]


def test_a_selected_microphone_that_disappeared_is_not_replaced(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()

    run = start_capture(
        sd, microphone=MicrophoneSelection(index=0, name="Headset", host_api="Windows WASAPI")
    )
    wait_until(lambda: bool(run.statuses))

    assert run.states()[0] == ("disconnected", "microphone_unavailable")
    assert sd.streams == []


# -- Subscriptions -----------------------------------------------------------------------


def test_a_late_subscription_starts_right_after_a_block_already_seen(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    subscriptions: list[CaptureSubscription] = []
    run = start_capture(sd, subscribe=subscribe_to(subscriptions))
    first = read_items(subscriptions[0], 3)

    late = run.capture.subscribe(max_seconds=30.0, after_index=first[0].index)  # type: ignore[union-attr]
    items = read_items(late, 3)

    assert [item.index for item in items] == [1, 2, 3]  # type: ignore[union-attr]


def test_a_subscription_older_than_the_history_starts_with_a_gap(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    subscriptions: list[CaptureSubscription] = []
    run = start_capture(sd, subscribe=subscribe_to(subscriptions), history_seconds=0.1)
    read_items(subscriptions[0], 6)

    late = run.capture.subscribe(max_seconds=30.0, after_index=0)
    items = read_items(late, 2)

    assert items[0] == CaptureGap("history")
    assert isinstance(items[1], AudioBlock)
    assert items[1].index > 1


def test_a_slow_subscription_overruns_with_a_gap(start_capture: Callable[..., Running]) -> None:
    sd = FakeSoundDevice()
    subscriptions: list[CaptureSubscription] = []
    run = start_capture(sd, subscribe=subscribe_to(subscriptions))
    slow = run.capture.subscribe(max_seconds=0.1)

    read_items(subscriptions[0], 8)
    items = read_items(slow, 3)

    assert items[0] == CaptureGap("overrun")
    indices = [item.index for item in items[1:]]  # type: ignore[union-attr]
    assert indices[1] == indices[0] + 1
    assert indices[0] > 0


def test_stopping_closes_the_stream_and_every_subscription(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    subscriptions: list[CaptureSubscription] = []
    run = start_capture(sd, subscribe=subscribe_to(subscriptions))
    read_items(subscriptions[0], 1)

    run.stop.set()

    assert run.capture.join(5)
    assert subscriptions[0].closed
    assert subscriptions[0].read(0) is None
    assert run.capture.status == CaptureStatus("stopped")
    assert all(stream.closed for stream in sd.streams)
    assert run.capture.subscribe(max_seconds=1.0).closed


def test_arrival_accounts_for_audio_still_waiting_in_the_device(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    sd.read_available = 320
    stage = FakeEchoStage()
    pool, _ = pool_of(stage)
    subscriptions: list[CaptureSubscription] = []

    start_capture(
        sd,
        echo_cancellation=True,
        echo_stages=pool,
        clock=lambda: 10.0,
        subscribe=subscribe_to(subscriptions),
    )
    read_items(subscriptions[0], 1)

    assert stage.processed[0] == (BLOCK_16K, "int16", pytest.approx(9.98))


# -- Echo stage ------------------------------------------------------------------------


def test_disabled_echo_cancellation_never_creates_a_stage(
    start_capture: Callable[..., Running],
) -> None:
    pool, calls = pool_of(FakeEchoStage())
    subscriptions: list[CaptureSubscription] = []

    run = start_capture(FakeSoundDevice(), echo_stages=pool, subscribe=subscribe_to(subscriptions))
    read_items(subscriptions[0], 1)

    assert calls == []
    assert run.statuses[0].echo_state == "off"


def test_the_echo_stage_output_is_the_recording_and_feeds_the_projection(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice(default_samplerate=16000)
    sd.feed(tone(0.2, 16000))
    stage = FakeEchoStage(rate=48000)
    pool, _ = pool_of(stage)
    subscriptions: list[CaptureSubscription] = []

    run = start_capture(
        sd, echo_cancellation=True, echo_stages=pool, subscribe=subscribe_to(subscriptions)
    )
    blocks = read_items(subscriptions[0], 5)

    assert stage.log[0] == "stage.open:16000"
    assert stage.processed[0][:2] == (BLOCK_16K, "int16")
    for block in blocks:
        assert isinstance(block, AudioBlock)
        assert block.recording_rate == 48000
        assert len(samples(block)) == 3 * BLOCK_16K
    projected = sum(len(samples(block, projection="pcm16")) for block in blocks)
    assert 0.9 * 5 * BLOCK_16K < projected <= 5 * BLOCK_16K
    assert run.statuses[0].echo_state == "active"


def test_variable_stage_output_keeps_the_block_numbering_continuous(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    signal = tone(0.32, 16000)
    sd.feed(signal)
    stage = FakeEchoStage(rate=48000)
    stage.hold = True
    pool, _ = pool_of(stage)
    subscriptions: list[CaptureSubscription] = []

    start_capture(
        sd, echo_cancellation=True, echo_stages=pool, subscribe=subscribe_to(subscriptions)
    )
    blocks = read_items(subscriptions[0], 4)

    assert [block.index for block in blocks] == [0, 1, 2, 3]  # type: ignore[union-attr]
    assert all(len(samples(block)) == 6 * BLOCK_16K for block in blocks)
    assert np.array_equal(
        np.concatenate([samples(block) for block in blocks]), np.repeat(signal, 3)
    )


def test_echo_state_changes_are_published(start_capture: Callable[..., Running]) -> None:
    stage = FakeEchoStage()
    pool, _ = pool_of(stage)
    run = start_capture(FakeSoundDevice(), echo_cancellation=True, echo_stages=pool)
    wait_until(lambda: bool(run.statuses))

    stage.state = "no_reference"

    wait_until(lambda: run.capture.status.echo_state == "no_reference")
    assert run.capture.status.state == "capturing"


def test_a_failing_stage_is_replaced_by_pass_through(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    stage = FakeEchoStage(rate=48000)
    pool, _ = pool_of(stage)
    subscriptions: list[CaptureSubscription] = []
    run = start_capture(
        sd, echo_cancellation=True, echo_stages=pool, subscribe=subscribe_to(subscriptions)
    )
    read_items(subscriptions[0], 2)

    stage.fail_process = True
    items = read_items(subscriptions[0], 12)

    assert CaptureGap("echo_failed") in items
    after = items[items.index(CaptureGap("echo_failed")) + 1 :]
    assert len(after) >= 2
    assert all(isinstance(item, AudioBlock) and item.recording_rate == 16000 for item in after)
    assert "stage.close" in stage.log
    wait_until(lambda: run.capture.status.echo_state == "unavailable")
    assert run.capture.status.state == "capturing"


def test_a_stage_that_cannot_open_passes_the_microphone_through(
    start_capture: Callable[..., Running],
) -> None:
    stage = FakeEchoStage()
    stage.fail_open = True
    pool, _ = pool_of(stage)
    subscriptions: list[CaptureSubscription] = []

    run = start_capture(
        FakeSoundDevice(),
        echo_cancellation=True,
        echo_stages=pool,
        subscribe=subscribe_to(subscriptions),
    )
    items = read_items(subscriptions[0], 2)

    assert all(isinstance(item, AudioBlock) and item.recording_rate == 16000 for item in items)
    assert run.statuses[0].echo_state == "unavailable"
    assert "stage.close" in stage.log


def test_an_unavailable_echo_canceller_is_asked_once(
    start_capture: Callable[..., Running],
) -> None:
    pool, calls = pool_of(None)

    for _ in range(2):
        run = start_capture(FakeSoundDevice(), echo_cancellation=True, echo_stages=pool)
        wait_until(run.has_status)
        assert run.statuses[0].echo_state == "unavailable"
        run.stop.set()
        assert run.capture.join(5)

    assert calls == [1]


def test_the_pool_reuses_the_stage_a_finished_capture_returned(
    start_capture: Callable[..., Running],
) -> None:
    stage = FakeEchoStage()
    pool, calls = pool_of(stage)

    for _ in range(2):
        run = start_capture(FakeSoundDevice(), echo_cancellation=True, echo_stages=pool)
        wait_until(run.has_status)
        run.stop.set()
        assert run.capture.join(5)

    assert calls == [1]
    assert [entry for entry in stage.log if entry.startswith("stage.")] == [
        "stage.open:16000",
        "stage.flush",
        "stage.close",
        "stage.open:16000",
        "stage.flush",
        "stage.close",
    ]


def test_a_stage_still_lent_out_is_never_shared(start_capture: Callable[..., Running]) -> None:
    held, fresh = FakeEchoStage(), FakeEchoStage()
    pool, calls = pool_of(held, fresh)
    assert pool.acquire() is held  # an abandoned capture still holds it

    run = start_capture(FakeSoundDevice(), echo_cancellation=True, echo_stages=pool)
    wait_until(lambda: bool(run.statuses))

    assert calls == [1, 1]
    assert fresh.log[0] == "stage.open:16000"
    assert held.log == []


def test_a_gap_releases_the_held_back_audio_before_the_discontinuity(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    sd.feed(tone(0.04, 16000), Overflow(), tone(0.04, 16000))
    stage = FakeEchoStage(rate=48000)
    stage.flush_output = np.full(96, 7, dtype=np.int16)
    pool, _ = pool_of(stage)
    subscriptions: list[CaptureSubscription] = []

    start_capture(
        sd, echo_cancellation=True, echo_stages=pool, subscribe=subscribe_to(subscriptions)
    )
    items = read_items(subscriptions[0], 4)

    assert isinstance(items[0], AudioBlock)
    assert np.array_equal(samples(items[1]), np.full(96, 7, dtype=np.int16))
    assert items[2] == CaptureGap("overflow")
    assert isinstance(items[3], AudioBlock)
    assert stage.log[1:3] == ["stage.flush", "stage.reset"]


def test_the_echo_reference_closes_before_portaudio_refreshes(
    start_capture: Callable[..., Running],
) -> None:
    sd = FakeSoundDevice()
    sd.feed(OSError("gone"), OSError("gone"), OSError("gone"))
    stage = FakeEchoStage(sd.events)
    pool, _ = pool_of(stage)

    run = start_capture(sd, echo_cancellation=True, echo_stages=pool)
    wait_until(lambda: len(run.statuses) >= 5)

    refresh = sd.events.index("terminate")
    assert "stage.close" in sd.events[:refresh]
    assert sd.events[refresh:].index("stage.open:16000") > sd.events[refresh:].index("initialize")


# -- Microphone selection ----------------------------------------------------------------


class _HostApiSoundDevice:
    """Two host APIs exposing the same headset; WDM-KS is the default input."""

    host_apis = [
        {"name": "Windows WDM-KS", "default_input_device": 0},
        {"name": "Windows WASAPI", "default_input_device": 1},
    ]
    devices = [
        {"name": "Headset", "hostapi": 0, "max_input_channels": 1, "default_samplerate": 48000},
        {"name": "Headset", "hostapi": 1, "max_input_channels": 1, "default_samplerate": 48000},
        {"name": "Speakers", "hostapi": 1, "max_input_channels": 0, "default_samplerate": 48000},
    ]

    class default:  # noqa: N801 - mirrors sounddevice.default
        device = (0, 2)

    def query_devices(self, device: int | None = None) -> Any:
        return list(self.devices) if device is None else self.devices[device]

    def query_hostapis(self, index: int | None = None) -> Any:
        return list(self.host_apis) if index is None else self.host_apis[index]

    def check_input_settings(self, **_kwargs: Any) -> None:
        return


def test_list_microphones_is_empty_without_sounddevice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    assert list_microphones() == []
    assert refresh_microphone_devices() is False


def test_a_refresh_makes_a_microphone_connected_later_visible() -> None:
    sd = FakeSoundDevice(default_samplerate=48000)
    connected = sd.devices
    sd.devices = []
    sd._initialize = lambda: setattr(sd, "devices", connected)  # type: ignore[method-assign]

    assert list_microphones(sd) == []
    assert refresh_microphone_devices(sd) is True
    assert list_microphones(sd) == [
        {
            "index": 0,
            "name": "Mic",
            "host_api": "Windows WASAPI",
            "default_sample_rate": 48000,
            "supported": True,
            "capture_sample_rate": 48000,
        }
    ]


def test_no_refresh_while_a_voice_stream_is_open() -> None:
    sd = FakeSoundDevice()
    stream, _ = open_input_stream(sd, None)
    try:
        assert refresh_microphone_devices(sd) is False
    finally:
        close_input_stream(stream)
    assert refresh_microphone_devices(sd) is True


@pytest.mark.parametrize(
    ("default_rate", "supported", "expected"),
    [
        (44100, None, 44100),
        (48000, None, 48000),
        (96000, {48000, 16000}, 48000),
        (8000, {8000, 16000}, 16000),
    ],
)
def test_capture_prefers_the_device_rate(
    default_rate: int, supported: set[int] | None, expected: int
) -> None:
    sd = FakeSoundDevice(default_samplerate=default_rate, rates=supported)

    assert _select_capture_format(sd, None).sample_rate == expected


def test_saved_microphone_identity_survives_device_reordering() -> None:
    class ReorderedSoundDevice:
        @staticmethod
        def query_devices() -> list[dict[str, object]]:
            return [
                {"name": "Webcam mic", "hostapi": 0, "max_input_channels": 1},
                {"name": "Studio mic", "hostapi": 1, "max_input_channels": 1},
            ]

        @staticmethod
        def query_hostapis(index: int) -> dict[str, object]:
            return {"name": ["WASAPI", "ASIO"][index]}

    requested = {"index": 0, "name": "Studio mic", "host_api": "ASIO"}

    assert _candidate_device_indices(ReorderedSoundDevice(), requested) == [1]


def test_saved_microphone_never_uses_a_recycled_index() -> None:
    class RecycledSoundDevice:
        @staticmethod
        def query_devices() -> list[dict[str, object]]:
            return [{"name": "Webcam mic", "hostapi": 0, "max_input_channels": 1}]

        @staticmethod
        def query_hostapis(_index: int) -> dict[str, object]:
            return {"name": "WASAPI"}

    requested = {"index": 0, "name": "Studio mic", "host_api": "ASIO"}

    assert _candidate_device_indices(RecycledSoundDevice(), requested) == []


def test_automatic_selection_never_uses_exclusive_wdm_ks_devices() -> None:
    sd = _HostApiSoundDevice()

    assert _candidate_device_indices(sd, None) == [1]
    assert _select_capture_format(sd, None).host_api == "Windows WASAPI"


def test_a_saved_wdm_ks_microphone_is_unavailable() -> None:
    sd = _HostApiSoundDevice()
    requested = {"index": 0, "name": "Headset", "host_api": "Windows WDM-KS"}

    assert _candidate_device_indices(sd, requested) == []
    with pytest.raises(MicrophoneUnavailableError):
        _select_capture_format(sd, requested)


def test_the_microphone_list_hides_wdm_ks_devices() -> None:
    devices = list_microphones(_HostApiSoundDevice())

    assert [(device["index"], device["host_api"]) for device in devices] == [(1, "Windows WASAPI")]
