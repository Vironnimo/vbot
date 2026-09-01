"""Tests for WakewordWorker state machine and lifecycle."""

from __future__ import annotations

import io
import sys
import time
import types
import wave
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import numpy as np
import pytest

from desktop.wakeword.engine import MockWakewordEngine, WakewordMatch
from desktop.wakeword.worker import SpeechDetector


class FakeBridge:
    """Captures published states for test assertions."""

    def __init__(self) -> None:
        self.states: list[str] = []
        self.errors: list[str | None] = []
        self.active_microphone: dict[str, object] | None = None

    def publish_state(self, state: str, error_code: str | None = None) -> None:
        self.states.append(state)
        self.errors.append(error_code)

    def publish_runtime_details(self, *, active_microphone: dict[str, object] | None) -> None:
        self.active_microphone = active_microphone


class FakeStream:
    """Fake PyAudio stream that yields chunks from a list."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self._stopped = False

    def read(self, frame_size: int, exception_on_overflow: bool = False) -> bytes:
        if self._index >= len(self._chunks) or self._stopped:
            return b"\x00" * frame_size
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    def stop_stream(self) -> None:
        self._stopped = True

    def close(self) -> None:
        self._stopped = True


class FakePyAudio:
    def __init__(self, stream: FakeStream | None = None) -> None:
        self._stream = stream or FakeStream([])

    def open(self, **kwargs: object) -> FakeStream:
        return self._stream

    def terminate(self) -> None:
        pass

    @staticmethod
    def get_device_count() -> int:
        return 0


class FakeSounddeviceBuffer:
    def __init__(self, value: bytes) -> None:
        self._value = value

    def tobytes(self) -> bytes:
        return self._value


class FakeSounddeviceStream:
    def __init__(self, chunks: list[bytes], *, on_read: Callable[[], None] | None = None) -> None:
        self._chunks = list(chunks)
        self._on_read = on_read
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def read(self, _frame_size: int) -> tuple[FakeSounddeviceBuffer, bool]:
        if callable(self._on_read):
            self._on_read()
        if not self._chunks:
            return FakeSounddeviceBuffer(_make_silence_chunk()), False
        return FakeSounddeviceBuffer(self._chunks.pop(0)), False

    def read_pcm16(self, frame_size: int) -> bytes:
        return self.read(frame_size)[0].tobytes()

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FailingReadStream(FakeSounddeviceStream):
    def read(self, _frame_size: int) -> tuple[FakeSounddeviceBuffer, bool]:
        raise RuntimeError("input overflowed")


class DetectOnceEngine:
    def __init__(self) -> None:
        self.calls = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def detect(self, _chunk: bytes, *, speech_present: bool = True) -> WakewordMatch | None:
        self.calls += 1
        if self.calls == 1:
            return WakewordMatch("builtin/okay_nabu", 1.0, 0.5)
        return None


class SequenceEngine:
    def __init__(self, scores: list[float], on_detect: Callable[[int], None] | None = None) -> None:
        self._scores = list(scores)
        self._on_detect = on_detect
        self.calls = 0
        self._armed = True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def detect(self, _chunk: bytes, *, speech_present: bool = True) -> WakewordMatch | None:
        self.calls += 1
        if callable(self._on_detect):
            self._on_detect(self.calls)
        if not self._scores:
            return None
        score = self._scores.pop(0)
        if score < 0.5:
            self._armed = True
            return None
        if not self._armed:
            return None
        self._armed = False
        return WakewordMatch("builtin/okay_nabu", score, 0.5)


@pytest.fixture
def fake_bridge() -> FakeBridge:
    return FakeBridge()


@pytest.fixture(autouse=True)
def ready_speech_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., str | None]:
    """Keep existing worker-state tests independent from a real server."""

    from desktop.wakeword import worker as worker_module

    original_checker = worker_module.check_speech_to_text_readiness
    monkeypatch.setattr(
        worker_module,
        "check_speech_to_text_readiness",
        lambda _server_url: None,
    )
    return original_checker


@pytest.fixture(autouse=True)
def no_real_neural_speech_detector(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], SpeechDetector | None]:
    """Keep worker tests from loading the real ONNX speech detector.

    Tests that need neural behavior inject a scripted detector explicitly;
    everything else must exercise the fail-open fallback instead of silently
    depending on the bundled model file. Yields the real factory so the
    integration test can restore it.
    """
    from desktop.wakeword.worker import SpeechDetector

    original_factory = SpeechDetector.create
    monkeypatch.setattr(SpeechDetector, "create", staticmethod(lambda: None))
    return original_factory


def _make_silence_chunk(samples: int = 1280) -> bytes:
    """Generate a near-silent PCM chunk that VAD classifies as non-speech."""
    import struct

    values = [0] * samples
    return struct.pack(f"<{samples}h", *values)


def _make_speech_chunk(samples: int = 512) -> bytes:
    """Generate a louder PCM chunk (16 kHz mono, 32 ms)."""
    import struct

    values = [1000] * samples
    return struct.pack(f"<{samples}h", *values)


def _wait_for_state(bridge: FakeBridge, state: str, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while state not in bridge.states and time.monotonic() < deadline:
        time.sleep(0.01)


def test_worker_lifecycle_start_stop(fake_bridge: FakeBridge) -> None:
    """Worker should start, enter listening, and stop cleanly."""
    from desktop.wakeword.worker import WakewordWorker

    engine = MockWakewordEngine()
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker._open_stream = lambda: setattr(  # type: ignore[method-assign]
        worker,
        "_stream",
        FakeSounddeviceStream([_make_silence_chunk()], on_read=worker._running.clear),
    )

    assert not worker.is_running()
    worker.start()
    _wait_for_state(fake_bridge, "listening")
    worker.stop()
    assert not worker.is_running()


def test_worker_publishes_error_when_engine_start_fails(
    fake_bridge: FakeBridge,
) -> None:
    """Worker should publish error state when engine.start() raises."""
    from desktop.wakeword.worker import WakewordWorker

    engine = MagicMock()
    engine.start.side_effect = RuntimeError("No model available")

    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    # A target agent is configured so start() passes the fail-fast gate and the
    # test actually exercises engine-start failure.
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker.start()
    _wait_for_state(fake_bridge, "error")

    assert "error" in fake_bridge.states
    assert fake_bridge.errors[-1] == "engine_start_failed"
    engine.start.assert_called_once()


def test_mock_engine_works_with_worker(fake_bridge: FakeBridge) -> None:
    """Mock engine with low scores should not trigger detection.

    Skips when pyaudio is unavailable since the worker opens a real mic stream.
    """
    try:
        import pyaudio  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        pytest.skip("pyaudio not installed")

    from desktop.wakeword.worker import WakewordWorker

    engine = MockWakewordEngine(score_sequence=[0.0])
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]

    worker.start()
    # Let it run for at least a few detection cycles
    import time

    time.sleep(0.3)
    worker.stop()

    # Should have entered listening at least
    assert "listening" in fake_bridge.states
    # With zero scores, should not have triggered a detection
    assert "wakeword_detected" not in fake_bridge.states
    assert "recording" not in fake_bridge.states


def test_encode_wav_produces_valid_container() -> None:
    """WAV encoding should produce a playable header with correct PCM data."""
    import io
    import wave

    from desktop.wakeword.worker import _encode_wav

    raw = _make_silence_chunk(1600)  # 100ms of silence
    wav_bytes = _encode_wav(raw)

    buffer = io.BytesIO(wav_bytes)
    with wave.open(buffer, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.readframes(wf.getnframes()) == raw


def test_detection_loop_passes_the_last_four_chunks_as_pre_roll(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import CapturedAudioFrame, WakewordWorker

    chunks = [bytes([value]) * 2560 for value in range(1, 6)]

    class DetectFifthEngine:
        def __init__(self) -> None:
            self.calls = 0

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def detect(self, _chunk: bytes, *, speech_present: bool = True) -> WakewordMatch | None:
            self.calls += 1
            if self.calls == 5:
                return WakewordMatch("builtin/hey_nabu", 0.8, 0.5)
            return None

    worker = WakewordWorker(
        engine=DetectFifthEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    captured: list[bytes] = []
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker._open_stream = lambda: setattr(  # type: ignore[method-assign]
        worker, "_stream", FakeSounddeviceStream(chunks)
    )

    def handle(pre_roll: bytes | tuple[CapturedAudioFrame, ...]) -> None:
        if isinstance(pre_roll, bytes):
            captured.append(pre_roll)
        else:
            captured.append(b"".join(frame.detection_pcm16 for frame in pre_roll))
        worker._running.clear()

    worker._handle_detection = handle  # type: ignore[assignment,method-assign]
    worker._running.set()

    worker._run()

    assert captured == [b"".join(chunks[-4:])]
    assert fake_bridge.states == ["listening", "wakeword_detected"]


def test_calibration_suppresses_wakeword_activation(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    engine = DetectOnceEngine()
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        calibration_checker=lambda: True,
    )
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker._open_stream = lambda: setattr(  # type: ignore[method-assign]
        worker,
        "_stream",
        FakeSounddeviceStream([_make_silence_chunk()], on_read=worker._running.clear),
    )
    worker._running.set()

    worker._run()

    assert engine.calls == 1
    assert "wakeword_detected" not in fake_bridge.states
    assert fake_bridge.states == ["listening"]


def test_recording_prepends_end_aligned_pre_roll_when_new_speech_arrives(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    class SpeechFirstDetector:
        def __init__(self) -> None:
            self.calls = 0

        def reset(self) -> None:
            self.calls = 0

        def is_speech(self, _frame: bytes) -> bool:
            self.calls += 1
            return self.calls == 1

    frame_bytes = 512 * 2
    aligned_pre_roll = b"".join(bytes([value]) * frame_bytes for value in range(1, 11))
    pre_roll = b"x" * 64 + aligned_pre_roll
    speech = b"s" * frame_bytes
    silence = b"\0" * frame_bytes
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=SpeechFirstDetector(),
    )
    worker._stream = FakeSounddeviceStream([speech, *([silence] * 50)])
    worker._running.set()

    wav_bytes = worker._record_until_silence(pre_roll)

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        recorded = wav_file.readframes(wav_file.getnframes())
    assert recorded.startswith(aligned_pre_roll + speech)


def test_pre_roll_alone_does_not_become_a_command(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    class SilentDetector:
        def reset(self) -> None:
            pass

        def is_speech(self, _frame: bytes) -> bool:
            return False

    monkeypatch.setattr(worker_module, "_SPEECH_START_FRAME_COUNT", 2)
    wake_phrase_audio = b"w" * (512 * 2 * 10)
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=SilentDetector(),
    )
    worker._running.set()

    # _record_until_silence consumes 512-sample PCM frames from the stream.
    class EndlessSilenceStream:
        def read_pcm16(self, _frame_size: int) -> bytes:
            return b"\0" * 1024

    worker._stream = EndlessSilenceStream()

    assert worker._record_until_silence(wake_phrase_audio) is None


def test_recording_exceeds_15_seconds_when_speech_continues(
    fake_bridge: FakeBridge,
) -> None:
    """Continuous speech must not be truncated by any fixed duration cap.

    Regression for the retired 15-second hard limit: a user who keeps talking
    is recorded until the utterance really ends; only the speech upload size
    budget (or silence) ends the capture.
    """
    from desktop.wakeword.worker import (
        _SILENCE_FRAME_COUNT,
        _VAD_FRAME_DURATION_MS,
        WakewordWorker,
    )

    class AlwaysSpeechDetector:
        def reset(self) -> None:
            pass

        def is_speech(self, _frame: bytes) -> bool:
            return True

    frames_for_20_seconds = int(20.0 / (_VAD_FRAME_DURATION_MS / 1000))
    speech_chunk = _make_speech_chunk()
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=AlwaysSpeechDetector(),
    )
    worker._stream = FakeSounddeviceStream([speech_chunk] * frames_for_20_seconds)
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        duration_seconds = wav_file.getnframes() / wav_file.getframerate()
    assert duration_seconds >= 20.0 - _SILENCE_FRAME_COUNT * (_VAD_FRAME_DURATION_MS / 1000)


def test_recording_stops_at_upload_budget_when_speech_never_ends(
    fake_bridge: FakeBridge,
) -> None:
    """An endless "speech" classification ends at the upload budget, not never.

    The budget is the conservative payload slice of the active server speech
    upload limit, measured in native-rate PCM bytes; the recording may never
    produce a payload the server would reject as oversize.
    """
    from desktop.wakeword.worker import (
        _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION,
        _UPLOAD_BUDGET_FALLBACK_BYTES,
        _WAV_HEADER_BYTES,
        WakewordWorker,
    )

    class AlwaysSpeechDetector:
        def reset(self) -> None:
            pass

        def is_speech(self, _frame: bytes) -> bool:
            return True

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=AlwaysSpeechDetector(),
    )
    worker._rpc_call = lambda _method, _params: {  # type: ignore[assignment]
        "setting": {"value": _UPLOAD_BUDGET_FALLBACK_BYTES}
    }
    worker._stream = EndlessSpeechStream()
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        payload_bytes = wav_file.getnframes() * wav_file.getsampwidth()
    assert payload_bytes <= int(
        (_UPLOAD_BUDGET_FALLBACK_BYTES - _WAV_HEADER_BYTES)
        * _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION
    )


def test_upload_budget_asks_the_server_for_its_active_limit(
    fake_bridge: FakeBridge,
) -> None:
    """The recording budget derives from the server's configured upload limit."""
    from desktop.wakeword.worker import (
        _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION,
        _WAV_HEADER_BYTES,
        WakewordWorker,
    )

    server_limit = 10 * 1024 * 1024
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._rpc_call = lambda _method, _params: {  # type: ignore[assignment]
        "setting": {"value": server_limit}
    }

    budget = worker._resolve_upload_budget_bytes()

    assert budget == int(
        (server_limit - _WAV_HEADER_BYTES) * _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION
    )


def test_stop_recording_ends_capture_and_keeps_audio(
    fake_bridge: FakeBridge,
) -> None:
    """A user stop closes the capture early but keeps the audio recorded so far.

    The captured frames must still flow through the normal pipeline (the caller
    transcribes and sends them), so the recording returns WAV bytes instead of
    discarding the utterance.
    """
    from desktop.wakeword.worker import WakewordWorker

    class AlwaysSpeechDetector:
        def reset(self) -> None:
            pass

        def is_speech(self, _frame: bytes) -> bool:
            return True

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=AlwaysSpeechDetector(),
    )
    reads = {"count": 0}

    def stop_after_few_reads() -> None:
        reads["count"] += 1
        if reads["count"] >= 5:
            worker.stop_recording()

    worker._stream = FakeSounddeviceStream(
        [_make_speech_chunk()] * 1000,
        on_read=stop_after_few_reads,
    )
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        duration_seconds = wav_file.getnframes() / wav_file.getframerate()
    # A handful of frames plus pre-roll, far below the endless stream.
    assert duration_seconds < 1.0


def test_stop_recording_request_does_not_leak_into_next_recording(
    fake_bridge: FakeBridge,
) -> None:
    """A stale stop request must not cut the next recording short.

    The stop event is cleared before every recording starts, so a stop that
    already ended one utterance never breaks the following one.
    """
    from unittest.mock import MagicMock

    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stop_recording.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()] * 10)
    worker._transcribe = lambda _audio: "test command"  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock(return_value="session-1")  # type: ignore[method-assign]
    worker._send_transcript = MagicMock(return_value=True)  # type: ignore[method-assign]
    worker._running.set()

    outcome = worker._handle_detection()

    assert outcome == "sent"
    worker._send_transcript.assert_called_once()


def test_recording_ends_during_continuous_noise_not_at_max_duration(
    fake_bridge: FakeBridge,
) -> None:
    """Continuous ambient noise must close the recording after silence, not hold it.

    Regression for wind/rain/traffic keeping the channel open: the neural
    detector classifies every ambient frame as non-speech, so after the
    wake-word-adjacent speech the recording ends at the silence timeout even
    though noise continues forever.
    """
    from desktop.wakeword.worker import (
        _SILENCE_FRAME_COUNT,
        WakewordWorker,
    )

    class NoiseDetector:
        """Classifies only the first two frames as speech, then pure noise."""

        def __init__(self) -> None:
            self.calls = 0

        def reset(self) -> None:
            self.calls = 0

        def is_speech(self, _frame: bytes) -> bool:
            self.calls += 1
            return self.calls <= 2

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=NoiseDetector(),
    )
    # Endless ambient noise after the initial speech; the recording must still
    # end at the silence timeout.
    worker._stream = EndlessNoiseStream(_make_speech_chunk())
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        frame_count = wav_file.getnframes()
    # 2 speech frames + a full silence window; never the endless noise tail.
    expected_max_samples = (2 + _SILENCE_FRAME_COUNT + 1) * 512
    assert frame_count <= expected_max_samples


class EndlessNoiseStream:
    """Yields endless 32 ms noise chunks for recording-loop tests."""

    def __init__(self, chunk: bytes) -> None:
        self._noise_chunk = chunk

    def read_pcm16(self, _frame_size: int) -> bytes:
        return self._noise_chunk


class EndlessSpeechStream:
    """Yields endless 32 ms loud chunks: the detector face-classifies as speech."""

    def read_pcm16(self, _frame_size: int) -> bytes:
        return _make_speech_chunk()


def test_single_noise_blip_does_not_reset_silence_timer(
    fake_bridge: FakeBridge,
) -> None:
    """One noise frame among trailing silence must not restart the recording.

    The neural endpoint needs consecutive-ish non-speech evidence; an isolated
    false-positive frame resets the silence counter in the legacy loop, so a
    noisy tail would extend the recording indefinitely. The detector's
    hysteresis closes once below the negative threshold and stays closed.
    """
    from desktop.wakeword.worker import (
        _SILENCE_DURATION_SECONDS,
        _VAD_FRAME_DURATION_MS,
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
    from desktop.wakeword.worker import _chunk_contains_speech

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
    from desktop.wakeword.worker import _chunk_contains_speech

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
    from unittest.mock import MagicMock

    from desktop.wakeword.worker import _chunk_contains_speech

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
    from desktop.wakeword.worker import _chunk_contains_speech

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
    from desktop.wakeword.worker import _chunk_contains_speech, _create_detection_vad

    vad = _create_detection_vad()

    assert vad is not None
    assert _chunk_contains_speech(_make_silence_chunk(), None, vad) is False
    assert _chunk_contains_speech(_make_speech_chunk(1280), None, vad) is True


def test_detection_loop_gates_engine_detection_on_speech_presence(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

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


@pytest.mark.parametrize(
    ("transcript", "cancelled"),
    [
        ("Abbrechen.", True),
        ("Mach das Licht aus, ach nein, vergiss es!", True),
        ("Bitte erkläre mir diesen Satz", False),
        ("Abbrechen und danach fortfahren", False),
    ],
)
def test_voice_cancel_phrase_requires_reserved_ending(transcript: str, cancelled: bool) -> None:
    from desktop.wakeword.worker import _is_voice_cancel_phrase

    assert _is_voice_cancel_phrase(transcript) is cancelled


def test_resampling_stream_normalizes_native_rate_to_wakeword_pcm() -> None:
    import numpy as np

    pytest.importorskip("soxr")
    from desktop.wakeword.worker import CaptureFormat, ResamplingInputStream

    class NativeStream:
        @staticmethod
        def read(frame_count: int) -> tuple[object, bool]:
            return np.linspace(-0.5, 0.5, frame_count, dtype=np.float32)[:, None], False

    stream = ResamplingInputStream(
        NativeStream(),  # type: ignore[arg-type]
        CaptureFormat(device=4, name="Studio mic", sample_rate=48000, dtype="float32"),
    )

    frame = stream.read_capture_frame(1280)

    assert len(frame.detection_pcm16) == 1280 * 2
    assert len(frame.recording_pcm16) == 3840 * 2
    assert frame.recording_sample_rate == 48000
    samples = np.frombuffer(frame.detection_pcm16, dtype=np.int16)
    assert samples[0] < 0
    assert samples[-1] > 0


def test_resampling_stream_attenuates_out_of_band_tones() -> None:
    import numpy as np

    pytest.importorskip("soxr")
    from desktop.wakeword.worker import CaptureFormat, ResamplingInputStream

    class SineNativeStream:
        def __init__(self, frequency: int, rate: int, amplitude: float = 0.5) -> None:
            self._frequency = frequency
            self._rate = rate
            self._amplitude = amplitude
            self._position = 0

        def read(self, frame_count: int) -> tuple[object, bool]:
            end = self._position + frame_count
            samples = (
                np.sin(2 * np.pi * self._frequency * np.arange(self._position, end) / self._rate)
                * self._amplitude
            )
            self._position = end
            return samples.astype(np.float32)[:, None], False

    def detection_rms(pcm16: bytes) -> float:
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
        return float(np.sqrt(np.mean(samples**2)))

    def read_detection_rms(frequency: int) -> float:
        stream = ResamplingInputStream(
            SineNativeStream(frequency, 48000),  # type: ignore[arg-type]
            CaptureFormat(device=4, name="Studio mic", sample_rate=48000, dtype="float32"),
        )
        frames = [stream.read_capture_frame(1280) for _ in range(12)]
        # Skip the resampler's startup latency before measuring.
        return detection_rms(b"".join(frame.detection_pcm16 for frame in frames[2:]))

    # A 10 kHz tone is out of band for the 16 kHz detector and must be filtered
    # away instead of aliasing into the detector's spectrum; 1 kHz must survive.
    out_of_band_rms = read_detection_rms(10000)
    in_band_rms = read_detection_rms(1000)

    assert in_band_rms > 8000
    assert out_of_band_rms < in_band_rms / 8


def test_resampling_stream_returns_exact_detection_length_every_read() -> None:
    import numpy as np

    pytest.importorskip("soxr")
    from desktop.wakeword.worker import CaptureFormat, ResamplingInputStream

    class NoisyNativeStream:
        def __init__(self, rate: int) -> None:
            self._rate = rate
            self._position = 0

        def read(self, frame_count: int) -> tuple[object, bool]:
            end = self._position + frame_count
            samples = np.sin(2 * np.pi * 440 * np.arange(self._position, end) / self._rate)
            self._position = end
            return samples.astype(np.float32)[:, None], False

    stream = ResamplingInputStream(
        NoisyNativeStream(44100),  # type: ignore[arg-type]
        CaptureFormat(device=4, name="Studio mic", sample_rate=44100, dtype="float32"),
    )

    lengths = {len(stream.read_capture_frame(1280).detection_pcm16) for _ in range(24)}

    assert lengths == {1280 * 2}


def test_resample_pcm16_filters_out_of_band_audio() -> None:
    import numpy as np

    pytest.importorskip("soxr")
    from desktop.wakeword.worker import _resample_pcm16

    def sine_pcm16(frequency: int, rate: int) -> bytes:
        samples = (np.sin(2 * np.pi * frequency * np.arange(rate) / rate) * 12000).astype(np.int16)
        return samples.tobytes()

    def rms(pcm16: bytes) -> float:
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
        return float(np.sqrt(np.mean(samples**2)))

    in_band = _resample_pcm16(sine_pcm16(1000, 48000), 48000, 16000)
    out_of_band = _resample_pcm16(sine_pcm16(10000, 48000), 48000, 16000)

    assert len(in_band) == 16000 * 2
    assert rms(in_band) > 8000
    assert rms(out_of_band) < rms(in_band) / 8


def test_command_audio_keeps_native_capture_rate_before_server_normalization() -> None:
    from desktop.wakeword.worker import CapturedAudioFrame, _encode_captured_audio

    native_audio = b"\x01\x00" * 1440
    wav_bytes = _encode_captured_audio(
        [
            CapturedAudioFrame(
                detection_pcm16=b"\x01\x00" * 480,
                recording_pcm16=native_audio,
                recording_sample_rate=48000,
            )
        ]
    )

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getframerate() == 48000
        assert wav_file.readframes(wav_file.getnframes()) == native_audio


def test_handle_detection_discards_voice_cancel_before_session_resolution(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]
    worker._transcribe = lambda _audio: "Mach das Licht aus, vergiss es."  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    outcome = worker._handle_detection()

    assert outcome == "cancelled"
    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()


def test_worker_does_not_record_without_target_agent(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._running.set()
    worker._read_config = lambda: {"target_agent_id": None}  # type: ignore[method-assign]

    worker._handle_detection()

    assert fake_bridge.states == ["error"]
    assert not worker._running.is_set()


def test_worker_start_fails_fast_without_target_agent(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    engine = MagicMock()
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._read_config = lambda: {"target_agent_id": None}  # type: ignore[method-assign]

    worker.start()
    _wait_for_state(fake_bridge, "error")
    assert worker._thread is not None
    worker._thread.join()

    # No engine loaded, no microphone opened: the misconfiguration surfaces the
    # moment listening is enabled, not on the first wake word.
    assert fake_bridge.states == ["starting", "error"]
    assert fake_bridge.errors[-1] == "missing_target_agent"
    assert not worker.is_running()
    engine.start.assert_not_called()


def test_worker_start_fails_before_engine_or_microphone_without_speech_to_text(
    fake_bridge: FakeBridge,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    engine = MagicMock()
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_readiness_checker=lambda _server_url: "speech_to_text_unconfigured",
    )
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = MagicMock(return_value=True)  # type: ignore[assignment,method-assign]
    worker._open_stream = MagicMock()  # type: ignore[method-assign]
    worker._running.set()

    with caplog.at_level("WARNING", logger="vbot.desktop.wakeword.worker"):
        worker._run()

    assert fake_bridge.states == ["error"]
    assert fake_bridge.errors == ["speech_to_text_unconfigured"]
    assert "speech_to_text_unconfigured" in caplog.text
    engine.start.assert_not_called()
    worker._open_stream.assert_not_called()
    worker._target_agent_available.assert_not_called()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"configured": False, "usable": False}, "speech_to_text_unconfigured"),
        ({"configured": True, "usable": False}, "speech_to_text_unavailable"),
        ({"configured": True, "usable": True}, None),
    ],
)
def test_speech_to_text_readiness_uses_server_task_model_status(
    result: dict[str, bool],
    expected: str | None,
    ready_speech_to_text: Callable[..., str | None],
) -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": True, "result": result}

    calls: list[tuple[str, dict[str, object], float, bool]] = []

    def post(url: str, *, json: dict[str, object], timeout: float, trust_env: bool) -> Any:
        calls.append((url, json, timeout, trust_env))
        return Response()

    assert ready_speech_to_text("http://pi.lan:8420/", post=post) == expected
    assert calls[0][0] == "http://pi.lan:8420/api/rpc"
    assert calls[0][1] == {
        "method": "task_model.status",
        "params": {"task_type": "speech_to_text"},
    }
    assert calls[0][3] is False


def test_worker_stop_during_target_validation_never_opens_engine(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    engine = MagicMock()
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]

    def validate_then_stop(_agent_id: str) -> bool:
        worker._running.clear()
        return True

    worker._target_agent_available = validate_then_stop  # type: ignore[assignment,method-assign]
    worker._running.set()

    worker._run()

    engine.start.assert_not_called()
    assert "error" not in fake_bridge.states


def test_microphone_start_failure_enters_recovery_instead_of_error(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import MicrophoneUnavailableError, WakewordWorker

    engine = MagicMock()
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker._open_stream = MagicMock(  # type: ignore[method-assign]
        side_effect=MicrophoneUnavailableError("unsupported")
    )

    def stop_while_waiting(_running, _duration_seconds: float) -> None:
        worker._running.clear()

    monkeypatch.setattr(worker_module, "_sleep_while_running", stop_while_waiting)
    worker._running.set()

    worker._run()

    engine.start.assert_called_once()
    engine.stop.assert_called_once()
    assert fake_bridge.states == ["microphone_disconnected"]
    assert fake_bridge.errors == ["microphone_unavailable"]
    assert "error" not in fake_bridge.states


def test_microphone_start_failure_recovers_when_microphone_appears(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import MicrophoneUnavailableError, WakewordWorker

    engine = MagicMock()
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    open_attempts = 0
    refresh_calls = 0
    reconnect_waits: list[float] = []

    def open_stream() -> None:
        nonlocal open_attempts
        open_attempts += 1
        if open_attempts == 1:
            raise MicrophoneUnavailableError("unsupported")
        worker._stream = FakeSounddeviceStream(
            [_make_silence_chunk()],
            on_read=worker._running.clear,
        )

    def refresh_microphone_devices() -> bool:
        nonlocal refresh_calls
        refresh_calls += 1
        return True

    def wait_for_reconnect(_running, duration_seconds: float) -> None:
        reconnect_waits.append(duration_seconds)

    worker._open_stream = open_stream  # type: ignore[method-assign]
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    monkeypatch.setattr(worker_module, "_sleep_while_running", wait_for_reconnect)
    monkeypatch.setattr(worker_module, "refresh_microphone_devices", refresh_microphone_devices)
    worker._running.set()

    worker._run()

    engine.start.assert_called_once()
    engine.stop.assert_called_once()
    assert open_attempts == 2
    assert refresh_calls == 1
    assert reconnect_waits == [30.0]
    assert fake_bridge.states == ["microphone_disconnected", "listening"]
    assert fake_bridge.errors == ["microphone_unavailable", None]
    assert "error" not in fake_bridge.states


def test_handle_detection_closes_microphone_before_network_calls(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._stream = stream
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]

    def transcribe(_audio_data: bytes) -> str:
        assert worker._stream is None
        return "hello"

    def resolve_session(_agent_id: str, _behavior: str) -> str:
        assert worker._stream is None
        return "session-one"

    def send_transcript(_transcript: str, _agent_id: str, _session_id: str) -> bool:
        assert worker._stream is None
        return True

    worker._transcribe = transcribe  # type: ignore[assignment,method-assign]
    worker._resolve_session = resolve_session  # type: ignore[assignment,method-assign]
    worker._send_transcript = send_transcript  # type: ignore[assignment,method-assign]

    worker._handle_detection()

    assert stream.stopped is True
    assert stream.closed is True
    assert fake_bridge.states == ["recording", "transcribing", "sending"]
    assert worker._running.is_set()


def test_handle_detection_empty_transcript_returns_to_listening(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._stream = stream
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]
    worker._transcribe = lambda _audio_data: "   "  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    worker._handle_detection()

    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()
    assert fake_bridge.states == ["recording", "transcribing"]
    assert worker._running.is_set()


def test_handle_detection_transcription_failure_returns_to_listening(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]
    worker._transcribe = lambda _audio_data: None  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    worker._handle_detection()

    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()
    assert fake_bridge.states == ["recording", "transcribing"]
    assert worker._running.is_set()


@pytest.mark.parametrize("transcript", [None, "   "])
def test_handle_detection_discards_failed_outcome_when_stopped_during_transcription(
    fake_bridge: FakeBridge,
    transcript: str | None,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]

    def transcribe_then_stop(_audio_data: bytes) -> str | None:
        worker._running.clear()
        return transcript

    worker._transcribe = transcribe_then_stop  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    outcome = worker._handle_detection()

    assert outcome is None
    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()
    assert fake_bridge.states == ["recording", "transcribing"]


@pytest.mark.parametrize("outcome", [None, "transcription_failed"])
def test_prepare_next_listen_publishes_nothing_after_stop(
    fake_bridge: FakeBridge,
    outcome: str | None,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    fake_bridge.publish_state("off")

    worker._prepare_next_listen(outcome)

    assert fake_bridge.states == ["off"]


def test_handle_detection_skips_network_when_stopped_during_recording(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }

    def record_then_stop(_pre_roll: bytes = b"") -> bytes:
        worker._running.clear()  # disabled/reconfigured mid-recording
        return b"audio"

    worker._record_until_silence = record_then_stop  # type: ignore[assignment,method-assign]
    worker._transcribe = MagicMock()  # type: ignore[method-assign]

    worker._handle_detection()

    # Stopped during recording: no transcription round-trip at all.
    worker._transcribe.assert_not_called()
    assert fake_bridge.states == ["recording"]


def test_handle_detection_discards_transcript_when_stopped_during_transcription(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]

    def transcribe_then_stop(_audio_data: bytes) -> str:
        worker._running.clear()  # disabled mid-transcription
        return "turn on the lights"

    worker._transcribe = transcribe_then_stop  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    worker._handle_detection()

    # A stop between capture and send must not fire a now-stale command.
    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()
    assert fake_bridge.states == ["recording", "transcribing"]


def test_detection_loop_reopens_microphone_after_successful_turn(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    monkeypatch.setattr(worker_module, "_POST_DETECTION_LISTENING_HOLD_SECONDS", 0.0)

    worker = WakewordWorker(
        engine=DetectOnceEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    opened_streams: list[FakeSounddeviceStream] = []

    def open_stream() -> None:
        on_read = worker._running.clear if opened_streams else None
        stream = FakeSounddeviceStream([_make_silence_chunk()], on_read=on_read)
        opened_streams.append(stream)
        worker._stream = stream

    worker._open_stream = open_stream  # type: ignore[method-assign]
    worker._handle_detection = lambda _pre_roll=b"": None  # type: ignore[assignment,method-assign]
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker._running.set()

    worker._run()

    assert len(opened_streams) == 2
    assert opened_streams[0].stopped is True
    assert opened_streams[0].closed is True
    assert opened_streams[1].stopped is True
    assert opened_streams[1].closed is True
    assert fake_bridge.states == ["listening", "wakeword_detected", "listening"]
    assert not worker._running.is_set()


def test_detection_loop_requires_score_drop_before_retrigger(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    monkeypatch.setattr(worker_module, "_POST_DETECTION_LISTENING_HOLD_SECONDS", 0.0)

    worker: WakewordWorker

    def stop_after_third_detection(call_count: int) -> None:
        if call_count >= 3:
            worker._running.clear()

    worker = WakewordWorker(
        engine=SequenceEngine([1.0, 1.0, 0.0], on_detect=stop_after_third_detection),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    detection_count = 0
    opened_streams: list[FakeSounddeviceStream] = []

    def open_stream() -> None:
        stream = FakeSounddeviceStream([_make_silence_chunk(), _make_silence_chunk()])
        opened_streams.append(stream)
        worker._stream = stream

    def handle_detection(_pre_roll: bytes = b"") -> None:
        nonlocal detection_count
        detection_count += 1

    worker._open_stream = open_stream  # type: ignore[method-assign]
    worker._handle_detection = handle_detection  # type: ignore[assignment,method-assign]
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker._running.set()

    worker._run()

    assert detection_count == 1
    assert len(opened_streams) == 2
    assert fake_bridge.states == ["listening", "wakeword_detected", "listening"]
    assert not worker._running.is_set()


def test_detection_loop_recovers_single_microphone_read_error(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(score_sequence=[0.0]),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    opened_streams: list[FakeSounddeviceStream] = []

    def open_stream() -> None:
        stream: FakeSounddeviceStream
        if not opened_streams:
            stream = FailingReadStream([])
        else:
            stream = FakeSounddeviceStream(
                [_make_silence_chunk()],
                on_read=worker._running.clear,
            )
        opened_streams.append(stream)
        worker._stream = stream

    worker._open_stream = open_stream  # type: ignore[method-assign]
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker._running.set()

    worker._run()

    assert len(opened_streams) == 2
    assert fake_bridge.states == ["listening", "listening"]
    assert "error" not in fake_bridge.states
    assert not worker._running.is_set()


def test_detection_loop_recovers_when_running_microphone_reconnects(
    fake_bridge: FakeBridge,
    monkeypatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(score_sequence=[0.0]),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    open_attempts = 0
    refresh_calls = 0
    reconnect_waits: list[float] = []

    def open_stream() -> None:
        nonlocal open_attempts
        open_attempts += 1
        if open_attempts == 1:
            worker._stream = FailingReadStream([])
            return
        if open_attempts == 2:
            raise RuntimeError("microphone disconnected")
        worker._stream = FakeSounddeviceStream(
            [_make_silence_chunk()],
            on_read=worker._running.clear,
        )

    def refresh_microphone_devices() -> bool:
        nonlocal refresh_calls
        refresh_calls += 1
        return True

    def wait_for_reconnect(_running, duration_seconds: float) -> None:
        reconnect_waits.append(duration_seconds)

    worker._open_stream = open_stream  # type: ignore[method-assign]
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    monkeypatch.setattr(worker_module, "_sleep_while_running", wait_for_reconnect)
    monkeypatch.setattr(worker_module, "refresh_microphone_devices", refresh_microphone_devices)
    worker._running.set()

    worker._run()

    assert open_attempts == 3
    assert refresh_calls == 1
    assert reconnect_waits == [30.0]
    assert fake_bridge.states == [
        "listening",
        "microphone_disconnected",
        "listening",
    ]
    assert fake_bridge.errors == [None, "microphone_read_failed", None]
    assert "error" not in fake_bridge.states
    assert not worker._running.is_set()


def test_resolve_session_uses_agent_current_session(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    calls: list[tuple[str, dict[str, object]]] = []
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    def rpc_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        if method == "agent.get":
            return {"current_session_id": "current-one"}
        raise AssertionError(f"unexpected method: {method}")

    worker._rpc_call = rpc_call  # type: ignore[method-assign]

    assert worker._resolve_session("main", "active") == "current-one"
    assert calls == [("agent.get", {"id": "main"})]


def test_resolve_session_falls_back_to_latest_activity(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    calls: list[tuple[str, dict[str, object]]] = []
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    def rpc_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        if method == "agent.get":
            return {"current_session_id": ""}
        if method == "session.list":
            return {
                "sessions": [
                    {"id": "older", "last_active_at": "2026-05-30T10:00:00+00:00"},
                    {"id": "newer", "last_active_at": "2026-05-31T10:00:00+00:00"},
                ]
            }
        raise AssertionError(f"unexpected method: {method}")

    worker._rpc_call = rpc_call  # type: ignore[method-assign]

    assert worker._resolve_session("main", "active") == "newer"
    assert calls == [
        ("agent.get", {"id": "main"}),
        (
            "session.list",
            {
                "agent_id": "main",
                "limit": 1,
                "include_subagents": True,
                "include_memory_reflections": True,
                "include_skill_reflections": True,
                "include_cron": True,
            },
        ),
    ]


def test_send_transcript_uses_streaming_rpc(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    calls: list[tuple[str, dict[str, object]]] = []
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    def rpc_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"run_id": "run-one", "sse_url": "/api/runs/run-one/events"}

    worker._rpc_call = rpc_call  # type: ignore[method-assign]

    sent = worker._send_transcript("hello", "main", "session-one")

    assert sent is True
    assert calls == [
        (
            "chat.stream",
            {
                "agent_id": "main",
                "session_id": "session-one",
                "content": "hello",
                "input_origin": "speech_transcription",
            },
        )
    ]


def test_worker_http_calls_ignore_environment_proxies(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": True, "result": {"id": "main"}, "text": "hello"}

    def post(_url: str, **kwargs: object) -> FakeResponse:
        calls.append(kwargs)
        return FakeResponse()

    monkeypatch.setattr(worker_module.httpx, "post", post)
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    assert worker._transcribe(b"audio") == "hello"
    assert worker._rpc_call("agent.get", {"id": "main"}) == {"id": "main"}
    assert len(calls) == 2
    assert all(call["trust_env"] is False for call in calls)


def test_rpc_call_returns_empty_for_rpc_error(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": False, "error": {"message": "bad request"}}

    monkeypatch.setattr(worker_module.httpx, "post", lambda *args, **kwargs: FakeResponse())
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    assert worker._rpc_call("agent.get", {"id": "main"}) == {}


@pytest.mark.parametrize("method", ["session.create", "chat.stream"])
def test_rpc_call_does_not_retry_mutation_after_ambiguous_transport_failure(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    calls = 0

    def ambiguous_failure(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "http://127.0.0.1:8420/api/rpc")
        raise httpx.ReadTimeout("response lost after send", request=request)

    monkeypatch.setattr(worker_module.httpx, "post", ambiguous_failure)
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._running.set()

    assert worker._rpc_call(method, {}) == {}
    assert calls == 1


def test_rpc_call_retries_safe_read_after_transport_failure(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    calls = 0

    class SuccessfulResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": True, "result": {"id": "main"}}

    def post(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "http://127.0.0.1:8420/api/rpc")
            raise httpx.ReadTimeout("temporary read failure", request=request)
        return SuccessfulResponse()

    monkeypatch.setattr(worker_module.httpx, "post", post)
    monkeypatch.setattr(worker_module, "_backoff_sleep", lambda *_args: None)
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._running.set()

    assert worker._rpc_call("agent.get", {"id": "main"}) == {"id": "main"}
    assert calls == 2


def test_mock_worker_start_stop_lifecycle(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import MockWakewordWorker

    worker = MockWakewordWorker(
        bridge=fake_bridge,
        engine=MockWakewordEngine(score_sequence=[0.0]),
    )

    assert not worker.is_running()
    worker.start()
    assert worker.is_running()
    worker.stop()
    assert not worker.is_running()
    # Even a never-triggering mock enters the visible listening state.
    assert "listening" in fake_bridge.states


def test_unavailable_worker_reports_stable_error_without_simulation(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import UnavailableWakewordWorker

    worker = UnavailableWakewordWorker(fake_bridge)
    worker.start()

    assert fake_bridge.states == ["error"]
    assert fake_bridge.errors == ["voice_stack_unavailable"]
    assert worker.is_running() is False


def test_mock_worker_walks_full_state_cycle_on_spike(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import MockWakewordWorker

    monkeypatch.setattr(worker_module, "_MOCK_FRAME_SECONDS", 0.0)
    monkeypatch.setattr(worker_module, "_MOCK_STAGE_SECONDS", 0.0)

    worker = MockWakewordWorker(bridge=fake_bridge)
    calls = {"n": 0}

    class SpikeThenStopEngine:
        def start(self) -> None:
            pass

        def detect(self, _chunk: bytes, *, speech_present: bool = True) -> WakewordMatch | None:
            calls["n"] += 1
            if calls["n"] == 1:
                return WakewordMatch("builtin/okay_nabu", 1.0, 0.5)
            worker._running.clear()
            return None

    worker._engine = SpikeThenStopEngine()  # type: ignore[assignment]
    worker._running.set()

    worker._run()

    # The mock drives the same state names the real worker publishes.
    assert fake_bridge.states == [
        "listening",
        "wakeword_detected",
        "recording",
        "transcribing",
        "sending",
        "sent",
        "listening",
    ]


def test_mock_worker_simulate_cycle_publishes_all_stages(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import MockWakewordWorker

    monkeypatch.setattr(worker_module, "_MOCK_STAGE_SECONDS", 0.0)

    worker = MockWakewordWorker(bridge=fake_bridge)
    worker._running.set()

    worker._simulate_cycle()

    assert fake_bridge.states == [
        "wakeword_detected",
        "recording",
        "transcribing",
        "sending",
        "sent",
    ]


def test_backoff_sleep_returns_immediately_when_not_running() -> None:
    import threading
    import time

    from desktop.wakeword.worker import _backoff_sleep

    running = threading.Event()  # cleared → the interruptible sleep returns at once

    start = time.monotonic()
    _backoff_sleep(6, running)  # ~64s exponential, clamped to 10s without the flag

    assert time.monotonic() - start < 1.0


def test_list_microphones_graceful_when_no_sounddevice(monkeypatch) -> None:
    """list_microphones should return empty list when sounddevice unavailable."""
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", None)

    from desktop.wakeword.worker import list_microphones

    devices = list_microphones()
    assert devices == []


def test_refresh_microphone_devices_reinitializes_portaudio_for_hotplug(monkeypatch) -> None:
    """A retry refresh makes a microphone connected after startup discoverable."""

    class HotplugSoundDevice:
        def __init__(self) -> None:
            self.connected_devices: list[dict[str, object]] = []
            self.cached_devices: list[dict[str, object]] = []
            self.lifecycle: list[str] = []

        def query_devices(self, device: int | None = None):
            if device is None:
                return list(self.cached_devices)
            return self.cached_devices[device]

        def check_input_settings(self, **_kwargs) -> None:
            return

        def _terminate(self) -> None:
            self.lifecycle.append("terminate")

        def _initialize(self) -> None:
            self.lifecycle.append("initialize")
            self.cached_devices = list(self.connected_devices)

    sounddevice = HotplugSoundDevice()
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", sounddevice)

    from desktop.wakeword.worker import list_microphones, refresh_microphone_devices

    assert list_microphones() == []
    sounddevice.connected_devices = [
        {
            "name": "USB microphone",
            "max_input_channels": 1,
            "default_samplerate": 48000,
        }
    ]
    assert list_microphones() == []

    assert refresh_microphone_devices() is True

    assert sounddevice.lifecycle == ["terminate", "initialize"]
    assert list_microphones() == [
        {
            "index": 0,
            "name": "USB microphone",
            "default_sample_rate": 48000,
            "supported": True,
            "capture_sample_rate": 16000,
        }
    ]
