"""Tests for WakewordWorker state machine and lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from desktop.wakeword.engine import MockWakewordEngine


class FakeBridge:
    """Captures published states for test assertions."""

    def __init__(self) -> None:
        self.states: list[str] = []

    def publish_state(self, state: str) -> None:
        self.states.append(state)


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

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FailingReadStream(FakeSounddeviceStream):
    def read(self, _frame_size: int) -> tuple[FakeSounddeviceBuffer, bool]:
        raise RuntimeError("input overflowed")


class DetectOnceEngine:
    threshold = 0.5

    def __init__(self) -> None:
        self.calls = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def detect(self, _chunk: bytes) -> float:
        self.calls += 1
        return 1.0 if self.calls == 1 else 0.0


@pytest.fixture
def fake_bridge() -> FakeBridge:
    return FakeBridge()


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


def test_worker_lifecycle_start_stop(fake_bridge: FakeBridge) -> None:
    """Worker should start, enter listening, and stop cleanly."""
    from desktop.wakeword.worker import WakewordWorker

    engine = MockWakewordEngine()
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    assert not worker.is_running()
    worker.start()
    assert worker.is_running()
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
    worker.start()

    assert "error" in fake_bridge.states


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
    worker._record_until_silence = lambda: b"audio"  # type: ignore[method-assign]

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


def test_detection_loop_reopens_microphone_after_successful_turn(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

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
    worker._handle_detection = lambda: None  # type: ignore[method-assign]
    worker._running.set()

    worker._run()

    assert len(opened_streams) == 2
    assert opened_streams[0].stopped is True
    assert opened_streams[0].closed is True
    assert opened_streams[1].stopped is True
    assert opened_streams[1].closed is True
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
            {"agent_id": "main", "session_id": "session-one", "content": "hello"},
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


def test_list_microphones_graceful_when_no_sounddevice(monkeypatch) -> None:
    """list_microphones should return empty list when sounddevice unavailable."""
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", None)

    from desktop.wakeword.worker import list_microphones

    devices = list_microphones()
    assert devices == []
