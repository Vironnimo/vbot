"""Worker: lifecycle behavior."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from desktop.wakeword.engine import MockWakewordEngine
from tests.desktop.worker_helpers import (
    FakeBridge,
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


def test_worker_does_not_reactivate_a_thread_that_timed_out_during_stop(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    class StuckThread:
        def __init__(self) -> None:
            self.join_timeouts: list[float] = []

        @staticmethod
        def is_alive() -> bool:
            return True

        def join(self, timeout: float) -> None:
            self.join_timeouts.append(timeout)

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    thread = StuckThread()
    worker._thread = cast("Any", thread)
    worker._running.set()

    worker.stop()
    worker.start()

    assert thread.join_timeouts == [3.0]
    assert worker._thread is thread
    assert not worker._running.is_set()
    assert "starting" not in fake_bridge.states


def test_unexpected_pipeline_failure_enters_stable_error(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._running.set()
    worker._run_pipeline = MagicMock(side_effect=RuntimeError("unexpected"))  # type: ignore[method-assign]

    worker._run()

    assert fake_bridge.states == ["error"]
    assert fake_bridge.errors == ["pipeline_failed"]
    assert not worker._running.is_set()


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
