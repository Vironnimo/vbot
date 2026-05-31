"""Tests for WakewordWorker state machine and lifecycle."""

from __future__ import annotations

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


def test_list_microphones_graceful_when_no_pyaudio(monkeypatch) -> None:
    """list_microphones should return empty list when PyAudio unavailable."""
    monkeypatch.setitem(__import__("sys").modules, "pyaudio", None)

    from desktop.wakeword.worker import list_microphones

    devices = list_microphones()
    assert devices == []
