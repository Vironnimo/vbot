"""Worker: vad behavior."""

from __future__ import annotations

import io
import sys
import types
import wave
from collections.abc import Callable
from typing import cast
from unittest.mock import MagicMock

import numpy as np
import pytest

from desktop.wakeword.engine import MockWakewordEngine
from desktop.wakeword.worker import SpeechDetector
from tests.desktop.worker_helpers import (
    EndlessNoiseStream,
    FakeBridge,
    FakeSounddeviceStream,
    _make_silence_chunk,
    _make_speech_chunk,
)
from tests.desktop.worker_helpers import (
    fake_bridge as fake_bridge,
)
from tests.desktop.worker_helpers import (
    no_real_neural_speech_detector as no_real_neural_speech_detector,
)
from tests.desktop.worker_helpers import (
    ready_speech_to_text as ready_speech_to_text,
)


def test_single_noise_blip_does_not_reset_silence_timer(
    fake_bridge: FakeBridge,
) -> None:
    """One noise frame among trailing silence must not restart the recording.

    The neural endpoint needs consecutive-ish non-speech evidence; an isolated
    false-positive frame resets the silence counter in the legacy loop, so a
    noisy tail would extend the recording indefinitely. The detector's
    hysteresis closes once below the negative threshold and stays closed.
    """
    from desktop.wakeword._worker_constants import (
        _SILENCE_DURATION_SECONDS,
        _VAD_FRAME_DURATION_MS,
    )
    from desktop.wakeword.worker import (
        WakewordWorker,
    )

    silence_frame_count = int(_SILENCE_DURATION_SECONDS / (_VAD_FRAME_DURATION_MS / 1000))

    class OneBlipDetector:
        """Speech for the opening, then silence with one classified blip."""

        def __init__(self) -> None:
            self.calls = 0

        def reset(self) -> None:
            self.calls = 0

        def is_speech(self, _frame: bytes) -> bool:
            self.calls += 1
            return self.calls <= 2 or self.calls == 10

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=OneBlipDetector(),
    )
    worker._stream = EndlessNoiseStream(_make_speech_chunk())
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        frame_count = wav_file.getnframes()
    # The blip resets the counter once, then a full silence window must close
    # regardless of where the blip landed (samples, not detector frames).
    assert frame_count <= (2 + silence_frame_count + 1 + silence_frame_count) * 512


def test_speech_detector_hysteresis_needs_conclusive_close() -> None:
    """Below the negative threshold closes; a mid-band probe stays active."""
    from desktop.wakeword.worker import SpeechDetector

    class ScriptedSession:
        """Returns the scripted probability, tracking model state calls."""

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

    detector = SpeechDetector(ScriptedSession([0.9, 0.4, 0.45, 0.2, 0.2]))

    frame = np.zeros(512, dtype=np.int16).tobytes()
    assert detector.is_speech(frame) is True  # 0.9 opens
    assert detector.is_speech(frame) is True  # 0.4 in the hysteresis band stays open
    assert detector.is_speech(frame) is True  # 0.45 still above negative threshold
    assert detector.is_speech(frame) is False  # 0.2 conclusively closes
    assert detector.is_speech(frame) is False  # closed state does not reopen mid-band


def test_speech_detector_reset_reopens_the_threshold() -> None:
    from desktop.wakeword.worker import SpeechDetector

    class ScriptedSession:
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

    session = ScriptedSession([0.9, 0.2, 0.9, 0.9])
    detector = SpeechDetector(session)

    frame = np.zeros(512, dtype=np.int16).tobytes()
    assert detector.is_speech(frame) is True
    assert detector.is_speech(frame) is False
    detector.reset()
    assert detector.is_speech(frame) is True  # a fresh utterance opens high again


def test_recording_falls_back_to_webrtc_when_neural_detector_absent(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the neural model the legacy WebRTC VAD must still gate recording."""
    from desktop.wakeword.worker import WakewordWorker

    class SpeechThenSilentVad:
        def __init__(self, _mode: int) -> None:
            self.calls = 0

        def is_speech(self, _frame: bytes, _sample_rate: int) -> bool:
            self.calls += 1
            return self.calls <= 2

    monkeypatch.setitem(
        sys.modules,
        "webrtcvad",
        types.SimpleNamespace(Vad=SpeechThenSilentVad),
    )
    noise_frames = [_make_speech_chunk() for _ in range(40)]
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=None,
    )
    worker._stream = FakeSounddeviceStream(noise_frames)
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None


def test_recording_counts_as_speech_when_no_vad_loads_at_all(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a totally broken decision stack recording must never go mute."""
    from desktop.wakeword.worker import WakewordWorker

    class ExplodingVad:
        def __init__(self, _mode: int) -> None:
            pass

        def is_speech(self, _frame: bytes, _sample_rate: int) -> bool:
            raise RuntimeError("vad exploded")

    monkeypatch.setitem(sys.modules, "webrtcvad", types.SimpleNamespace(Vad=ExplodingVad))
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=None,
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    # The frame was counted as speech (fail-open) and thus recorded.
    assert wav_bytes is not None


def test_real_speech_detector_endpoints_speech_and_ignores_noise(
    no_real_neural_speech_detector: Callable[[], SpeechDetector | None],
) -> None:
    """Integration: the bundled model separates real speech from noise tails.

    Uses the okay_nabu fixture (real spoken wake word, quiet lead-in/out) and
    asserts the detector both opens on the phrase and closes after it, then
    refuses to reopen on continuing noise.
    """
    pytest.importorskip("onnxruntime")
    pytest.importorskip("soxr")
    import soxr

    detector = no_real_neural_speech_detector()
    assert detector is not None
    with wave.open("tests/fixtures/wakeword/okay_nabu.wav", "rb") as wav_file:
        rate = wav_file.getframerate()
        raw = wav_file.readframes(wav_file.getnframes())
    samples = soxr.resample(np.frombuffer(raw, dtype=np.int16), rate, 16000).astype(np.float32)
    samples /= 32768.0

    active_frames = 0
    for position in range(0, len(samples) - 512, 512):
        frame = (samples[position : position + 512] * 32767).astype(np.int16).tobytes()
        if detector.is_speech(frame):
            active_frames += 1

    assert active_frames > 10  # real phrase audio opens the detector

    # After the utterance closed, pure noise must not reopen it.
    rng = np.random.default_rng(3)
    noise_reactivations = sum(
        1
        for _ in range(20)
        if detector.is_speech((rng.normal(0, 3000, 512)).astype(np.int16).tobytes())
    )
    assert noise_reactivations == 0


def test_detection_gate_requires_two_speech_frames_per_chunk() -> None:
    from desktop.wakeword._speech_detection import (
        _chunk_contains_speech,
    )

    class ScriptedVad:
        def __init__(self, speech_frames: set[int]) -> None:
            self._speech_frames = speech_frames
            self.frame_index = 0

        def is_speech(self, _frame: bytes, _sample_rate: int) -> bool:
            is_speech = self.frame_index in self._speech_frames
            self.frame_index += 1
            return is_speech

    chunk = b"\x10\x20" * 1280  # 2560 bytes = eight 10 ms frames

    assert _chunk_contains_speech(chunk, None, ScriptedVad({0})) is False
    assert _chunk_contains_speech(chunk, None, ScriptedVad({0, 3})) is True


def test_detection_gate_fails_open_when_it_cannot_judge() -> None:
    from desktop.wakeword._speech_detection import (
        _chunk_contains_speech,
    )

    class RaisingVad:
        @staticmethod
        def is_speech(_frame: bytes, _sample_rate: int) -> bool:
            raise RuntimeError("vad exploded")

    chunk = b"\x10\x20" * 1280

    assert _chunk_contains_speech(chunk, None, None) is True
    assert _chunk_contains_speech(b"\x10\x20" * 10, None, RaisingVad()) is True
    assert _chunk_contains_speech(chunk, None, RaisingVad()) is True


def test_detection_gate_neural_detector_gates_noise_and_admits_speech(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a neural detector, chunks are gated on speech probability."""

    from desktop.wakeword._speech_detection import (
        _chunk_contains_speech,
    )

    detector = MagicMock()
    detector.speech_probability.return_value = 0.0
    noise = b"\x10\x20" * 1280

    # A high absolute score counts as speech regardless of the legacy VAD.
    detector.speech_probability.return_value = 0.9
    assert _chunk_contains_speech(noise, detector, None) is True

    # A low score is non-speech; the legacy VAD is not consulted anymore.
    detector.speech_probability.return_value = 0.1
    assert _chunk_contains_speech(noise, detector, None) is False


def test_detection_gate_neural_scoring_failure_falls_back_to_webrtc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword._speech_detection import (
        _chunk_contains_speech,
    )

    class SilentVad:
        @staticmethod
        def is_speech(_frame: bytes, _sample_rate: int) -> bool:
            return False

    class ExplodingDetector:
        @staticmethod
        def speech_probability(_chunk: bytes) -> float:
            raise RuntimeError("model exploded")

    chunk = b"\x10\x20" * 1280
    exploding = cast(SpeechDetector, ExplodingDetector())

    assert _chunk_contains_speech(chunk, exploding, None) is True
    assert _chunk_contains_speech(chunk, exploding, SilentVad()) is False


def test_detection_gate_matches_silence_and_speech_with_real_fallback_vad() -> None:
    pytest.importorskip("webrtcvad")
    from desktop.wakeword._speech_detection import (
        _chunk_contains_speech,
        _create_detection_vad,
    )

    vad = _create_detection_vad()

    assert vad is not None
    assert _chunk_contains_speech(_make_silence_chunk(), None, vad) is False
    assert _chunk_contains_speech(_make_speech_chunk(1280), None, vad) is True


def test_detection_loop_gates_engine_detection_on_speech_presence(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from desktop.wakeword.worker import WakewordWorker

    class AlternatingDetector:
        """Opens the gate from the second detection chunk onward."""

        call_count = 0

        @classmethod
        def speech_probability(cls, _chunk: bytes) -> float:
            cls.call_count += 1
            return 0.9 if cls.call_count > 1 else 0.0

    engine = MagicMock()
    engine.detect.return_value = None
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=AlternatingDetector,
    )
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    reads = {"count": 0}

    def stop_after_second_read() -> None:
        reads["count"] += 1
        if reads["count"] >= 2:
            worker._running.clear()

    worker._open_stream = lambda: setattr(  # type: ignore[method-assign]
        worker,
        "_stream",
        FakeSounddeviceStream(
            [_make_silence_chunk(), _make_speech_chunk(1280)],
            on_read=stop_after_second_read,
        ),
    )
    worker._running.set()

    worker._run()

    gate_flags = [call.kwargs["speech_present"] for call in engine.detect.call_args_list]
    assert gate_flags == [False, True]
