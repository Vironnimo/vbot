"""Continuous wake phrase detection over a capture subscription."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from desktop.wakeword.capture import CaptureSubscription
from desktop.wakeword.detection import PRE_ROLL_SECONDS, Detection, DetectionLoop
from tests.desktop.voice_fakes import (
    AmplitudeVad,
    FakeSubscription,
    ScriptedEngine,
    silence,
    tone,
    wait_until,
)


class ResettableDetector:
    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def speech_probability(self, _chunk: bytes) -> float:
        return 0.0


@dataclass
class Loop:
    loop: DetectionLoop
    subscription: FakeSubscription
    engine: ScriptedEngine
    stop: threading.Event
    detections: list[Detection] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    started: threading.Event = field(default_factory=threading.Event)
    calibrating: list[bool] = field(default_factory=lambda: [False])

    def wait_idle(self, chunks: int) -> None:
        wait_until(lambda: self.subscription.empty and len(self.engine.chunks) >= chunks)


@pytest.fixture
def start_loop() -> Iterator[Callable[..., Loop]]:
    loops: list[Loop] = []

    def start(
        *,
        engine: ScriptedEngine | None = None,
        detector: Any = None,
        on_detection: Callable[[Detection], None] | None = None,
    ) -> Loop:
        subscription = FakeSubscription()
        stop = threading.Event()
        state = Loop(
            loop=cast(DetectionLoop, None),
            subscription=subscription,
            engine=engine or ScriptedEngine(),
            stop=stop,
        )
        state.loop = DetectionLoop(
            subscription=cast(CaptureSubscription, subscription),
            engine=state.engine,
            stop_event=stop,
            on_detection=on_detection or state.detections.append,
            on_started=state.started.set,
            on_failed=state.failures.append,
            calibrating=lambda: state.calibrating[0],
            speech_detector_factory=lambda: detector,
            fallback_vad_factory=AmplitudeVad,
        )
        loops.append(state)
        state.loop.start()
        return state

    yield start
    for state in loops:
        state.stop.set()
        assert state.loop.join(5), "detection thread did not stop"


def test_blocks_are_rechunked_into_engine_chunks_with_a_speech_gate(
    start_loop: Callable[..., Loop],
) -> None:
    state = start_loop()
    assert state.started.wait(5)

    state.subscription.push_audio(silence(0.16, 16000))  # 2 chunks of silence
    state.subscription.push_audio(tone(0.2, 16000))  # 2.5 chunks of speech
    state.wait_idle(4)

    assert state.engine.chunks == [(2560, False), (2560, False), (2560, True), (2560, True)]


def test_a_detection_carries_the_preceding_whole_blocks_as_pre_roll(
    start_loop: Callable[..., Loop], caplog: pytest.LogCaptureFixture
) -> None:
    state = start_loop()
    assert state.started.wait(5)
    blocks = state.subscription.push_audio(tone(0.8, 16000))
    state.wait_idle(10)

    with caplog.at_level(logging.INFO, logger="vbot.desktop.wakeword.detection"):
        state.engine.fire("builtin/hey_nabu", score=0.8, threshold=0.4)
        more = state.subscription.push_audio(tone(0.08, 16000))
        wait_until(lambda: bool(state.detections))

    detection = state.detections[0]
    assert (detection.model_id, detection.score, detection.threshold) == (
        "builtin/hey_nabu",
        0.8,
        0.4,
    )
    pre_roll = detection.pre_roll
    assert pre_roll[-1] == more[-1]  # ends with the block that completed the phrase
    assert [block.index for block in pre_roll] == list(range(pre_roll[0].index, more[-1].index + 1))
    assert PRE_ROLL_SECONDS <= sum(block.duration for block in pre_roll) < PRE_ROLL_SECONDS + 0.04
    assert blocks[0] not in pre_roll
    assert "Wakeword detected: model=builtin/hey_nabu score=0.800 threshold=0.400" in caplog.text


def test_the_pre_roll_starts_over_after_a_detection(start_loop: Callable[..., Loop]) -> None:
    state = start_loop()
    assert state.started.wait(5)
    state.engine.fire("builtin/okay_nabu")
    state.engine.fire("builtin/okay_nabu")

    first = state.subscription.push_audio(tone(0.16, 16000))
    wait_until(lambda: len(state.detections) == 2)

    assert state.detections[0].pre_roll == tuple(first[:2])
    assert state.detections[1].pre_roll == tuple(first[2:])


def test_a_gap_clears_the_pending_audio_and_the_pre_roll(start_loop: Callable[..., Loop]) -> None:
    detector = ResettableDetector()
    state = start_loop(detector=detector)
    assert state.started.wait(5)
    state.subscription.push_audio(tone(0.12, 16000))  # 1.5 chunks
    state.wait_idle(1)

    state.subscription.push_gap()
    state.engine.fire("builtin/okay_nabu")
    after = state.subscription.push_audio(tone(0.08, 16000))
    wait_until(lambda: bool(state.detections))

    assert len(state.engine.chunks) == 2  # the half chunk before the gap was dropped
    assert state.detections[0].pre_roll == tuple(after)
    assert detector.resets == 1


def test_calibration_suppresses_matches(start_loop: Callable[..., Loop]) -> None:
    state = start_loop()
    assert state.started.wait(5)
    state.calibrating[0] = True
    state.engine.fire("builtin/okay_nabu")

    state.subscription.push_audio(tone(0.16, 16000))
    state.wait_idle(2)

    assert state.engine.pending == 0
    assert state.detections == []


def test_a_failing_detection_handler_does_not_stop_detection(
    start_loop: Callable[..., Loop],
) -> None:
    seen: list[str] = []

    def handler(detection: Detection) -> None:
        seen.append(detection.model_id)
        raise RuntimeError("handler broke")

    state = start_loop(on_detection=handler)
    assert state.started.wait(5)
    state.engine.fire("builtin/okay_nabu")
    state.engine.fire("builtin/hey_nabu")

    state.subscription.push_audio(tone(0.16, 16000))
    wait_until(lambda: len(seen) == 2)

    assert state.failures == []


def test_an_engine_that_cannot_start_reports_engine_start_failed(
    start_loop: Callable[..., Loop],
) -> None:
    engine = ScriptedEngine()
    engine.fail_start = True

    state = start_loop(engine=engine)

    assert state.loop.join(5)
    assert state.failures == ["engine_start_failed"]
    assert not state.started.is_set()
    assert state.subscription.closed


def test_a_failing_engine_reports_detection_failed(start_loop: Callable[..., Loop]) -> None:
    engine = ScriptedEngine()
    engine.fail_detect = True
    state = start_loop(engine=engine)
    assert state.started.wait(5)

    state.subscription.push_audio(tone(0.08, 16000))

    assert state.loop.join(5)
    assert state.failures == ["detection_failed"]
    assert engine.stopped.is_set()
    assert state.subscription.closed


def test_stopping_ends_the_loop_and_releases_the_engine(start_loop: Callable[..., Loop]) -> None:
    state = start_loop()
    assert state.started.wait(5)

    state.stop.set()

    assert state.loop.join(5)
    assert state.engine.stopped.is_set()
    assert state.subscription.closed
    assert state.failures == []


def test_the_loop_ends_when_its_capture_ends(start_loop: Callable[..., Loop]) -> None:
    state = start_loop()
    assert state.started.wait(5)

    state.subscription.close()

    assert state.loop.join(5)
    assert state.engine.stopped.is_set()
