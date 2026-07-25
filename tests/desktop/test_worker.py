"""Tests for WakewordWorker state machine and lifecycle."""

from __future__ import annotations

import io
import sys
import time
import types
import wave
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from desktop.wakeword.engine import MockWakewordEngine, WakewordMatch


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

    def detect(self, _chunk: bytes) -> WakewordMatch | None:
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

    def detect(self, _chunk: bytes) -> WakewordMatch | None:
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


def _make_silence_chunk(samples: int = 1280) -> bytes:
    """Generate a near-silent PCM chunk that VAD classifies as non-speech."""
    import struct

    values = [0] * samples
    return struct.pack(f"<{samples}h", *values)


def _make_speech_chunk(samples: int = 480) -> bytes:
    """Generate a louder PCM chunk that VAD classifies as speech."""
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

        def detect(self, _chunk: bytes) -> WakewordMatch | None:
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

    class FakeVad:
        def __init__(self, _mode: int) -> None:
            self.calls = 0

        def is_speech(self, _frame: bytes, _sample_rate: int) -> bool:
            self.calls += 1
            return self.calls == 1

    monkeypatch.setitem(sys.modules, "webrtcvad", types.SimpleNamespace(Vad=FakeVad))
    frame_bytes = 480 * 2
    aligned_pre_roll = b"".join(bytes([value]) * frame_bytes for value in range(1, 11))
    pre_roll = b"x" * 640 + aligned_pre_roll
    speech = b"s" * frame_bytes
    silence = b"\0" * frame_bytes
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
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

    class SilentVad:
        def __init__(self, _mode: int) -> None:
            pass

        def is_speech(self, _frame: bytes, _sample_rate: int) -> bool:
            return False

    monkeypatch.setitem(sys.modules, "webrtcvad", types.SimpleNamespace(Vad=SilentVad))
    monkeypatch.setattr(worker_module, "_SPEECH_START_FRAME_COUNT", 2)
    wake_phrase_audio = b"w" * (480 * 2 * 10)
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([b"\0" * 960, b"\0" * 960])
    worker._running.set()

    assert worker._record_until_silence(wake_phrase_audio) is None


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

    calls: list[tuple[str, dict[str, object], float]] = []

    def post(url: str, *, json: dict[str, object], timeout: float) -> Any:
        calls.append((url, json, timeout))
        return Response()

    assert ready_speech_to_text("http://pi.lan:8420/", post=post) == expected
    assert calls[0][0] == "http://pi.lan:8420/api/rpc"
    assert calls[0][1] == {
        "method": "task_model.status",
        "params": {"task_type": "speech_to_text"},
    }


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


def test_microphone_start_failure_releases_loaded_engine(
    fake_bridge: FakeBridge,
) -> None:
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
    worker._running.set()

    worker._run()

    engine.start.assert_called_once()
    engine.stop.assert_called_once()
    assert fake_bridge.errors[-1] == "microphone_unavailable"


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

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    def rpc_call(method: str, params: dict[str, object]) -> dict[str, object]:
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

        def detect(self, _chunk: bytes) -> WakewordMatch | None:
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
