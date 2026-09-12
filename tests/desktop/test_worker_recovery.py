"""Worker: recovery behavior."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from desktop.wakeword.engine import MockWakewordEngine, WakewordMatch
from tests.desktop.worker_helpers import (
    DetectOnceEngine,
    FakeBridge,
    FakeSounddeviceBuffer,
    FakeSounddeviceStream,
    _make_silence_chunk,
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


class FailingReadStream(FakeSounddeviceStream):
    def read(self, _frame_size: int) -> tuple[FakeSounddeviceBuffer, bool]:
        raise RuntimeError("input overflowed")


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
