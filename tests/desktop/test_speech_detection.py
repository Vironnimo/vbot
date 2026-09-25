"""Tests for the speech gate and endpointing detectors in ``desktop.wakeword._speech_detection``."""

from __future__ import annotations

import wave
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import numpy as np
import pytest

from desktop.wakeword._speech_detection import (
    SpeechDetector,
    SpeechGate,
    chunk_contains_speech,
    create_fallback_vad,
    frame_is_speech,
)

_SAMPLE_RATE = 16000
_SLICE_BYTES = 320  # 10 ms of 16 kHz PCM16
_RECORDING_FRAME = b"\x10\x00" * 512  # one 32 ms endpointing frame
_DETECTION_CHUNK = b"\x10\x00" * 1280  # one 80 ms detection chunk
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "wakeword" / "okay_nabu.wav"


class StrictVad:
    """Behaves like ``webrtcvad.Vad``: only 10, 20 or 30 ms frames are valid."""

    def __init__(self, speech_slices: set[int] | None = None) -> None:
        self._speech_slices = speech_slices or set()
        self.frames: list[bytes] = []

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        if sample_rate != _SAMPLE_RATE or len(frame) not in {320, 640, 960}:
            raise ValueError("Error while processing frame")
        index = len(self.frames)
        self.frames.append(frame)
        return index in self._speech_slices


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


# -- Endpointing fallback (frame_is_speech) ----------------------------------------


def test_recording_fallback_judges_a_silent_frame_as_silence() -> None:
    """Regression: a 32 ms frame used to crash WebRTC VAD and fail open as speech."""
    vad = StrictVad()

    assert frame_is_speech(_RECORDING_FRAME, None, vad) is False
    assert [len(frame) for frame in vad.frames] == [_SLICE_BYTES] * 3


@pytest.mark.parametrize(
    ("speech_slices", "expected"),
    [({0, 2}, True), ({1, 2}, True), ({1}, False), (set(), False)],
)
def test_recording_fallback_needs_two_speech_slices(
    speech_slices: set[int], expected: bool
) -> None:
    assert frame_is_speech(_RECORDING_FRAME, None, StrictVad(speech_slices)) is expected


def test_recording_fallback_fails_open_when_it_cannot_judge() -> None:
    class BrokenVad:
        def is_speech(self, _frame: bytes, _sample_rate: int) -> bool:
            raise RuntimeError("vad exploded")

    assert frame_is_speech(_RECORDING_FRAME, None, BrokenVad()) is True
    assert frame_is_speech(b"\x10\x00" * 100, None, StrictVad()) is True
    assert frame_is_speech(_RECORDING_FRAME, None, None) is True


def test_a_single_slice_needs_one_speech_verdict() -> None:
    single_slice = b"\x10\x00" * 160

    assert frame_is_speech(single_slice, None, StrictVad({0})) is True
    assert frame_is_speech(single_slice, None, StrictVad()) is False


def test_real_webrtc_vad_judges_a_silent_recording_frame() -> None:
    webrtcvad = pytest.importorskip("webrtcvad")

    assert frame_is_speech(b"\x00\x00" * 512, None, webrtcvad.Vad(1)) is False


# -- Detection gate (chunk_contains_speech) ----------------------------------------


def test_detection_gate_feeds_only_valid_slices_to_the_vad() -> None:
    vad = StrictVad()

    assert chunk_contains_speech(_DETECTION_CHUNK, None, vad) is False
    assert [len(frame) for frame in vad.frames] == [_SLICE_BYTES] * 8


def test_detection_gate_requires_two_speech_slices_per_chunk() -> None:
    assert chunk_contains_speech(_DETECTION_CHUNK, None, StrictVad({0})) is False
    assert chunk_contains_speech(_DETECTION_CHUNK, None, StrictVad({0, 3})) is True


def test_detection_gate_fails_open_when_it_cannot_judge() -> None:
    class RaisingVad:
        @staticmethod
        def is_speech(_frame: bytes, _sample_rate: int) -> bool:
            raise RuntimeError("vad exploded")

    assert chunk_contains_speech(_DETECTION_CHUNK, None, None) is True
    assert chunk_contains_speech(b"\x10\x20" * 10, None, RaisingVad()) is True
    assert chunk_contains_speech(_DETECTION_CHUNK, None, RaisingVad()) is True


def test_detection_gate_uses_the_neural_probability_when_available() -> None:
    detector = MagicMock()

    detector.speech_probability.return_value = 0.9
    assert chunk_contains_speech(_DETECTION_CHUNK, detector, None) is True

    detector.speech_probability.return_value = 0.1
    assert chunk_contains_speech(_DETECTION_CHUNK, detector, None) is False


def test_detection_gate_falls_back_to_webrtc_when_neural_scoring_fails() -> None:
    class ExplodingDetector:
        @staticmethod
        def speech_probability(_chunk: bytes) -> float:
            raise RuntimeError("model exploded")

    exploding = cast(SpeechDetector, ExplodingDetector())

    assert chunk_contains_speech(_DETECTION_CHUNK, exploding, None) is True
    assert chunk_contains_speech(_DETECTION_CHUNK, exploding, StrictVad()) is False


def test_detection_gate_separates_silence_and_speech_with_the_real_fallback_vad() -> None:
    pytest.importorskip("webrtcvad")
    vad = create_fallback_vad()

    assert vad is not None
    assert chunk_contains_speech(b"\x00\x00" * 1280, None, vad) is False
    assert chunk_contains_speech(np.full(1280, 1000, np.int16).tobytes(), None, vad) is True


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
    gate = SpeechGate(cast(SpeechDetector, detector), None)

    assert _admitted(gate, 16) == [False] * 10 + [True] * 3 + [False] * 3


def test_speech_gate_stays_closed_until_four_chunks_of_history_exist() -> None:
    gate = SpeechGate(cast(SpeechDetector, ScriptedChunkDetector([0.9] * 8)), None)

    assert _admitted(gate, 8) == [False] * 4 + [True] * 4


def test_speech_gate_reset_forgets_the_history_and_resets_the_detector() -> None:
    detector = ScriptedChunkDetector([0.9] * 6 + [0.0] * 6)
    gate = SpeechGate(cast(SpeechDetector, detector), None)
    _admitted(gate, 6)

    gate.reset()

    assert _admitted(gate, 6) == [False] * 6
    assert detector.resets == 1


def test_speech_gate_uses_the_fallback_vad_without_a_neural_detector() -> None:
    class LoudVad:
        @staticmethod
        def is_speech(frame: bytes, _sample_rate: int) -> bool:
            return any(frame)

    gate = SpeechGate(None, LoudVad())
    silent_chunk = b"\x00\x00" * 1280

    admitted = [gate.admits(_DETECTION_CHUNK)] + [gate.admits(silent_chunk) for _ in range(8)]

    assert admitted == [False] * 4 + [True] * 3 + [False] * 2


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
