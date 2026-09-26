"""Continuous wake phrase detection over a capture subscription."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from desktop.wakeword import engine as engine_module
from desktop.wakeword.capture import CaptureSubscription
from desktop.wakeword.config import DEFAULT_MODEL_IDS, PhraseConfig
from desktop.wakeword.detection import PRE_ROLL_SECONDS, Detection, DetectionLoop
from desktop.wakeword.engine import MultiWakewordEngine, WakewordModelCatalog
from tests.desktop.voice_fakes import (
    AmplitudeVad,
    FakeSubscription,
    ScriptedEngine,
    silence,
    tone,
    wait_until,
)


class ResettableDetector:
    """Speech detector double: scripted per-chunk probabilities, then silence."""

    def __init__(self, probabilities: Iterable[float] = ()) -> None:
        self.resets = 0
        self._probabilities = iter(probabilities)

    def reset(self) -> None:
        self.resets += 1

    def speech_probability(self, _chunk: bytes) -> float:
        return next(self._probabilities, 0.0)


def _engine_with_scripted_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head_scores: list[float],
    score_listener: Callable[[dict[str, float]], None],
) -> MultiWakewordEngine:
    """A real engine whose feature extractor and detector head are scripted per chunk."""
    features = Mock()
    features.process_streaming.return_value = ["features"]
    head = Mock()
    head.process_streaming.side_effect = [[score] for score in head_scores]
    monkeypatch.setattr(
        engine_module, "_create_pyopenwakeword_features", Mock(return_value=features)
    )
    monkeypatch.setattr(engine_module, "_create_pyopenwakeword_model", Mock(return_value=head))
    return WakewordModelCatalog(tmp_path / "settings.json").create_engine(
        [PhraseConfig(DEFAULT_MODEL_IDS[0])], score_listener=score_listener
    )


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


def test_blocks_are_rechunked_into_engine_chunks_with_a_delayed_speech_gate(
    start_loop: Callable[..., Loop],
) -> None:
    state = start_loop()
    assert state.started.wait(5)

    state.subscription.push_audio(silence(0.16, 16000))  # chunks 0-1
    state.subscription.push_audio(tone(0.2, 16000))  # chunks 2-4 carry speech
    state.subscription.push_audio(silence(0.48, 16000))  # up to chunk 9
    state.wait_idle(10)

    # A chunk's scores count 4 to 6 chunks after speech: chunks 6 to 10.
    assert state.engine.chunks == [(2560, False)] * 6 + [(2560, True)] * 4


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


def test_a_gap_forgets_the_speech_heard_before_it(start_loop: Callable[..., Loop]) -> None:
    detector = ResettableDetector([0.9] * 3)
    state = start_loop(detector=detector)
    assert state.started.wait(5)

    state.subscription.push_audio(silence(0.24, 16000))  # 3 chunks the detector hears as speech
    state.subscription.push_gap()
    state.subscription.push_audio(silence(0.56, 16000))  # 7 chunks
    state.wait_idle(10)

    # Without the reset, chunks 4 to 8 would count on the speech before the gap.
    assert [speech for _size, speech in state.engine.chunks] == [False] * 10
    assert detector.resets == 1


def test_a_phrase_the_heads_score_after_the_speech_ended_is_detected(
    start_loop: Callable[..., Loop], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The phrase fills chunks 0-9; the head peaks 4 chunks later and scores noise in the pause.
    head_scores = [0.0] * 12 + [0.3, 0.9, 0.4] + [0.0] * 5 + [0.9] * 10
    scores: list[float] = []
    engine = _engine_with_scripted_head(
        tmp_path, monkeypatch, head_scores, lambda frame: scores.append(frame[DEFAULT_MODEL_IDS[0]])
    )
    state = start_loop(engine=engine, detector=ResettableDetector([0.9] * 10))
    assert state.started.wait(5)

    state.subscription.push_audio(silence(2.4, 16000))  # 30 chunks
    wait_until(lambda: len(scores) == 30)

    assert [(detection.model_id, detection.score) for detection in state.detections] == [
        (DEFAULT_MODEL_IDS[0], 0.9)
    ]
    # The score listener (calibration) sees the gated scores: the pause's noise is zeroed.
    assert scores == [0.0] * 12 + [0.3, 0.9, 0.4] + [0.0] * 15


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
