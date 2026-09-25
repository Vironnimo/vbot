"""Tests for the WebRTC VAD fallback in ``desktop.wakeword._speech_detection``."""

from __future__ import annotations

import pytest

from desktop.wakeword._audio_capture import CapturedAudioFrame
from desktop.wakeword._speech_detection import _chunk_contains_speech, _frame_is_speech

_SAMPLE_RATE = 16000
_SLICE_BYTES = 320  # 10 ms of 16 kHz PCM16
_RECORDING_FRAME = b"\x10\x00" * 512  # one 32 ms endpointing frame
_DETECTION_CHUNK = b"\x10\x00" * 1280  # one 80 ms detection chunk


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


def _recording_frame(pcm16: bytes = _RECORDING_FRAME) -> CapturedAudioFrame:
    return CapturedAudioFrame(
        detection_pcm16=pcm16, recording_pcm16=pcm16, recording_sample_rate=_SAMPLE_RATE
    )


def test_recording_fallback_judges_a_silent_frame_as_silence() -> None:
    """Regression: a 32 ms frame used to crash WebRTC VAD and fail open as speech."""
    vad = StrictVad()

    assert _frame_is_speech(_recording_frame(), None, vad) is False
    assert [len(frame) for frame in vad.frames] == [_SLICE_BYTES] * 3


@pytest.mark.parametrize(
    ("speech_slices", "expected"),
    [({0, 2}, True), ({1, 2}, True), ({1}, False), (set(), False)],
)
def test_recording_fallback_needs_two_speech_slices(
    speech_slices: set[int], expected: bool
) -> None:
    assert _frame_is_speech(_recording_frame(), None, StrictVad(speech_slices)) is expected


def test_recording_fallback_fails_open_when_it_cannot_judge() -> None:
    class BrokenVad:
        def is_speech(self, _frame: bytes, _sample_rate: int) -> bool:
            raise RuntimeError("vad exploded")

    assert _frame_is_speech(_recording_frame(), None, BrokenVad()) is True
    assert _frame_is_speech(_recording_frame(b"\x10\x00" * 100), None, StrictVad()) is True
    assert _frame_is_speech(_recording_frame(), None, None) is True


def test_a_single_slice_needs_one_speech_verdict() -> None:
    single_slice = _recording_frame(b"\x10\x00" * 160)

    assert _frame_is_speech(single_slice, None, StrictVad({0})) is True
    assert _frame_is_speech(single_slice, None, StrictVad()) is False


def test_detection_gate_feeds_only_valid_slices_to_the_vad() -> None:
    vad = StrictVad()

    assert _chunk_contains_speech(_DETECTION_CHUNK, None, vad) is False
    assert [len(frame) for frame in vad.frames] == [_SLICE_BYTES] * 8


def test_real_webrtc_vad_judges_a_silent_recording_frame() -> None:
    webrtcvad = pytest.importorskip("webrtcvad")

    assert _frame_is_speech(_recording_frame(b"\x00\x00" * 512), None, webrtcvad.Vad(1)) is False
