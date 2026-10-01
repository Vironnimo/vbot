"""Tests for the speech gate and endpointing detectors in ``desktop.wakeword._speech_detection``."""

from __future__ import annotations

import wave
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from desktop.wakeword._speech_detection import (
    SpeechDetector,
    SpeechGate,
    chunk_contains_speech,
    frame_is_speech,
)

_RECORDING_FRAME = b"\x10\x00" * 512  # one 32 ms endpointing frame
_DETECTION_CHUNK = b"\x10\x00" * 1280  # one 80 ms detection chunk
_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "wakeword" / "okay_nabu.wav"


class ScriptedSession:
    """An ONNX session double that returns scripted speech probabilities."""

    def __init__(self, probabilities: list[float]) -> None:
        self._probabilities = list(probabilities)
        self.calls = 0

    def run(self, _output_names: object, _feeds: object) -> tuple[object, object]:
        probability = self._probabilities[self.calls]
        self.calls += 1
        return (
            np.array([[probability]], dtype=np.float32),
            np.zeros((2, 1, 128), dtype=np.float32),
        )


# -- Speech decisions --------------------------------------------------------------


class ExplodingDetector:
    """A loaded detector whose scoring fails."""

    @staticmethod
    def is_speech(_frame: bytes) -> bool:
        raise RuntimeError("model exploded")

    @staticmethod
    def speech_probability(_chunk: bytes) -> float:
        raise RuntimeError("model exploded")


@pytest.mark.parametrize("detector", [None, ExplodingDetector()], ids=["absent", "failing"])
def test_speech_decisions_fail_open_without_a_working_detector(detector: object) -> None:
    speech_detector = cast(SpeechDetector | None, detector)

    assert frame_is_speech(_RECORDING_FRAME, speech_detector) is True
    assert chunk_contains_speech(_DETECTION_CHUNK, speech_detector) is True


def test_detection_gate_follows_the_neural_probability() -> None:
    confident = cast(SpeechDetector, ScriptedChunkDetector([0.9]))
    unsure = cast(SpeechDetector, ScriptedChunkDetector([0.1]))

    assert chunk_contains_speech(_DETECTION_CHUNK, confident) is True
    assert chunk_contains_speech(_DETECTION_CHUNK, unsure) is False


# -- Delayed wakeword score gate (SpeechGate) --------------------------------------


class ScriptedChunkDetector:
    """Returns one scripted speech probability per detection chunk and counts resets."""

    def __init__(self, probabilities: list[float]) -> None:
        self._probabilities = iter(probabilities)
        self.resets = 0

    def speech_probability(self, _chunk: bytes) -> float:
        return next(self._probabilities)

    def reset(self) -> None:
        self.resets += 1


def _admitted(gate: SpeechGate, chunks: int) -> list[bool]:
    return [gate.admits(_DETECTION_CHUNK) for _ in range(chunks)]


def test_speech_gate_admits_the_scores_four_to_six_chunks_after_speech() -> None:
    detector = ScriptedChunkDetector([0.0] * 6 + [0.9] + [0.0] * 9)
    gate = SpeechGate(cast(SpeechDetector, detector))

    assert _admitted(gate, 16) == [False] * 10 + [True] * 3 + [False] * 3


def test_speech_gate_stays_closed_until_four_chunks_of_history_exist() -> None:
    gate = SpeechGate(cast(SpeechDetector, ScriptedChunkDetector([0.9] * 8)))

    assert _admitted(gate, 8) == [False] * 4 + [True] * 4


def test_speech_gate_reset_forgets_the_history_and_resets_the_detector() -> None:
    detector = ScriptedChunkDetector([0.9] * 6 + [0.0] * 6)
    gate = SpeechGate(cast(SpeechDetector, detector))
    _admitted(gate, 6)

    gate.reset()

    assert _admitted(gate, 6) == [False] * 6
    assert detector.resets == 1


def test_speech_gate_is_open_without_any_speech_detector() -> None:
    assert _admitted(SpeechGate(None), 3) == [True] * 3


# -- Neural endpointing detector ---------------------------------------------------


def test_speech_detector_hysteresis_needs_a_conclusive_close() -> None:
    detector = SpeechDetector(ScriptedSession([0.9, 0.4, 0.45, 0.2, 0.2]))
    frame = np.zeros(512, dtype=np.int16).tobytes()

    assert detector.is_speech(frame) is True  # 0.9 opens
    assert detector.is_speech(frame) is True  # 0.4 in the hysteresis band stays open
    assert detector.is_speech(frame) is True  # 0.45 still above the negative threshold
    assert detector.is_speech(frame) is False  # 0.2 conclusively closes
    assert detector.is_speech(frame) is False  # a closed detector does not reopen mid-band


def test_speech_detector_reset_reopens_the_threshold() -> None:
    detector = SpeechDetector(ScriptedSession([0.9, 0.2, 0.9, 0.9]))
    frame = np.zeros(512, dtype=np.int16).tobytes()

    assert detector.is_speech(frame) is True
    assert detector.is_speech(frame) is False
    detector.reset()
    assert detector.is_speech(frame) is True  # a fresh utterance opens high again


def test_real_speech_detector_endpoints_speech_and_ignores_noise() -> None:
    """Integration: the bundled model opens on a real phrase and stays closed on noise."""
    pytest.importorskip("onnxruntime")
    soxr = pytest.importorskip("soxr")

    detector = SpeechDetector.create()
    assert detector is not None
    with wave.open(str(_FIXTURE), "rb") as wav_file:
        rate = wav_file.getframerate()
        raw = wav_file.readframes(wav_file.getnframes())
    samples = soxr.resample(np.frombuffer(raw, dtype=np.int16), rate, 16000).astype(np.int16)

    active_frames = sum(
        1
        for position in range(0, len(samples) - 512, 512)
        if detector.is_speech(samples[position : position + 512].tobytes())
    )
    assert active_frames > 10

    rng = np.random.default_rng(3)
    noise_reactivations = sum(
        1
        for _ in range(20)
        if detector.is_speech(rng.normal(0, 3000, 512).astype(np.int16).tobytes())
    )
    assert noise_reactivations == 0
