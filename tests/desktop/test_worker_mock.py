"""Worker: mock behavior."""

from __future__ import annotations

import time

import pytest

from desktop.wakeword.engine import MockWakewordEngine, WakewordMatch
from tests.desktop.worker_helpers import (
    FakeBridge,
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
    from desktop.wakeword import _worker_modes as worker_module
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
    from desktop.wakeword import _worker_modes as worker_module
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

    from desktop.wakeword._worker_support import (
        _backoff_sleep,
    )

    running = threading.Event()  # cleared → the interruptible sleep returns at once

    start = time.monotonic()
    _backoff_sleep(6, running)  # ~64s exponential, clamped to 10s without the flag

    assert time.monotonic() - start < 1.0
