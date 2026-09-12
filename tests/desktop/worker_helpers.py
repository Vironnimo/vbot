"""Shared fixtures and fakes for worker behavior tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from desktop.wakeword.engine import WakewordMatch
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


class EndlessNoiseStream:
    """Yields endless 32 ms noise chunks for recording-loop tests."""

    def __init__(self, chunk: bytes) -> None:
        self._noise_chunk = chunk

    def read_pcm16(self, _frame_size: int) -> bytes:
        return self._noise_chunk
