"""Bridge: configuration behavior."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from desktop.wakeword.bridge import DesktopBridge
from desktop.wakeword.engine import (
    DEFAULT_WAKEWORD_MODEL_IDS,
)
from tests.desktop.bridge_helpers import (
    FakeWorker,
    _write_settings,
)


def test_server_url_normalizes_trailing_slash(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(
        settings_path=tmp_path / "settings.json",
        server_url="http://pi.lan:9000/",
    )

    assert bridge.server_url == "http://pi.lan:9000"


def test_set_server_url_rebuilds_running_worker(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    workers: list[FakeWorker] = []

    def worker_factory(bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        # Record the URL the factory saw so we can prove it reads the current one.
        worker.server_url = bridge.server_url  # type: ignore[attr-defined]
        workers.append(worker)
        return worker

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        server_url="http://a.lan:8420/",
    )
    bridge.setWakewordEnabled(True)
    assert workers[0].server_url == "http://a.lan:8420"  # type: ignore[attr-defined]

    bridge.set_server_url("http://b.lan:9000/")

    # The running worker is rebuilt against the new server.
    assert len(workers) == 2
    assert workers[0].stopped is True
    assert workers[1].started is True
    assert workers[1].server_url == "http://b.lan:9000"  # type: ignore[attr-defined]


def test_set_server_url_noop_when_unchanged(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    workers: list[FakeWorker] = []

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        workers.append(worker)
        return worker

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        server_url="http://a.lan:8420/",
    )
    bridge.setWakewordEnabled(True)

    # Same target (trailing slash normalized away) → no needless rebuild, so the
    # launch auto-connect to the already-open server does not restart the worker.
    bridge.set_server_url("http://a.lan:8420")

    assert len(workers) == 1


def test_set_server_url_stores_for_next_start_when_no_worker_running(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)  # wakeword disabled → nothing running
    workers: list[FakeWorker] = []

    def worker_factory(bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        worker.server_url = bridge.server_url  # type: ignore[attr-defined]
        workers.append(worker)
        return worker

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        server_url="",
    )

    # First-run shape: connect happens before voice is enabled. Nothing is built
    # now, but the URL is stored so the next start targets the right server.
    bridge.set_server_url("http://pi.lan:9000/")
    assert workers == []
    assert bridge.server_url == "http://pi.lan:9000"

    bridge.setWakewordEnabled(True)
    assert workers[0].server_url == "http://pi.lan:9000"  # type: ignore[attr-defined]


def test_set_wakeword_enabled_toggles_and_persists(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(
        settings_path=settings_file,
        server_url="http://127.0.0.1:8420",
    )

    bridge.setWakewordEnabled(True)
    status = bridge.getWakewordStatus()
    assert status["enabled"] is True

    bridge.setWakewordEnabled(False)
    status = bridge.getWakewordStatus()
    assert status["enabled"] is False


def test_set_wakeword_enabled_rejects_missing_speech_to_text_before_persisting(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    worker = FakeWorker()
    bridge = DesktopBridge(
        settings_path=settings_file,
        worker=worker,
        server_url="http://127.0.0.1:8420",
        speech_readiness_checker=lambda _server_url: "speech_to_text_unconfigured",
    )

    result = bridge.setWakewordEnabled(True)

    assert result == {
        "enabled": False,
        "error_code": "speech_to_text_unconfigured",
    }
    assert bridge.getWakewordStatus()["enabled"] is False
    assert bridge.getWakewordStatus()["state"] == "error"
    assert bridge.getWakewordStatus()["error_code"] == "speech_to_text_unconfigured"
    assert worker.started is False


def test_set_wakeword_enabled_uses_worker_factory(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    workers: list[FakeWorker] = []

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        workers.append(worker)
        return worker

    bridge = DesktopBridge(settings_path=settings_file, worker_factory=worker_factory)

    bridge.setWakewordEnabled(True)

    assert len(workers) == 1
    assert workers[0].started is True
    assert bridge.getWakewordStatus()["enabled"] is True


def test_parallel_wakeword_enable_disable_is_one_serialized_worker_lifecycle(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": False})
    factory_entered = threading.Event()
    release_factory = threading.Event()
    workers: list[FakeWorker] = []
    failures: list[BaseException] = []

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        workers.append(worker)
        factory_entered.set()
        assert release_factory.wait(timeout=1)
        return worker

    bridge = DesktopBridge(settings_path=settings_file, worker_factory=worker_factory)

    def invoke(enabled: bool) -> None:
        try:
            bridge.setWakewordEnabled(enabled)
        except BaseException as error:
            failures.append(error)

    enable_thread = threading.Thread(target=invoke, args=(True,))
    disable_thread = threading.Thread(target=invoke, args=(False,))
    enable_thread.start()
    assert factory_entered.wait(timeout=1)
    disable_thread.start()

    assert disable_thread.is_alive()
    assert len(workers) == 1

    release_factory.set()
    enable_thread.join(timeout=1)
    disable_thread.join(timeout=1)

    assert failures == []
    assert not enable_thread.is_alive()
    assert not disable_thread.is_alive()
    assert len(workers) == 1
    assert workers[0].stopped is True
    assert bridge.getWakewordStatus()["enabled"] is False


def test_disable_waits_for_inflight_readiness_and_wins(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": False})
    readiness_entered = threading.Event()
    release_readiness = threading.Event()
    workers: list[FakeWorker] = []

    def check_readiness(_server_url: str) -> None:
        readiness_entered.set()
        assert release_readiness.wait(timeout=1)

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        workers.append(worker)
        return worker

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        server_url="http://127.0.0.1:8420",
        speech_readiness_checker=check_readiness,
    )
    enable_thread = threading.Thread(target=bridge.setWakewordEnabled, args=(True,))
    disable_thread = threading.Thread(target=bridge.setWakewordEnabled, args=(False,))

    enable_thread.start()
    assert readiness_entered.wait(timeout=1)
    disable_thread.start()

    assert disable_thread.is_alive()
    release_readiness.set()
    enable_thread.join(timeout=1)
    disable_thread.join(timeout=1)

    assert len(workers) == 1
    assert workers[0].stopped is True
    assert bridge.getWakewordStatus()["enabled"] is False


def test_reenable_replaces_factory_worker_that_did_not_stop_in_time(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": False})

    class StuckWorker(FakeWorker):
        def stop(self) -> None:
            self.stopped = True

    workers: list[StuckWorker] = []

    def worker_factory(_bridge: DesktopBridge) -> StuckWorker:
        worker = StuckWorker()
        workers.append(worker)
        return worker

    bridge = DesktopBridge(settings_path=settings_file, worker_factory=worker_factory)

    bridge.setWakewordEnabled(True)
    bridge.setWakewordEnabled(False)
    bridge.setWakewordEnabled(True)

    assert len(workers) == 2
    assert workers[0].stopped is True
    assert workers[1].started is True


def test_set_wakeword_config_recreates_running_worker(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    workers: list[FakeWorker] = []

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        workers.append(worker)
        return worker

    bridge = DesktopBridge(settings_path=settings_file, worker_factory=worker_factory)
    bridge.setWakewordEnabled(True)

    bridge.setWakewordConfig({"model_sensitivities": {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.9}})

    assert len(workers) == 2
    assert workers[0].stopped is True
    assert workers[1].started is True
    assert bridge.getWakewordStatus()["model_sensitivities"][DEFAULT_WAKEWORD_MODEL_IDS[0]] == 0.9


def test_set_wakeword_config_partial_update(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(
        settings_path=settings_file,
        server_url="http://127.0.0.1:8420",
    )
    bridge.setWakewordConfig(
        {
            "model_sensitivities": {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.9},
            "target_agent_id": "agent-1",
        }
    )

    status = bridge.getWakewordStatus()
    assert status["model_sensitivities"][DEFAULT_WAKEWORD_MODEL_IDS[0]] == 0.9
    assert status["target_agent_id"] == "agent-1"


def test_retry_refreshes_microphones_before_rebuilding_real_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    previous_worker = FakeWorker()
    previous_worker.start()
    lifecycle: list[str] = []
    replacement_worker = FakeWorker()

    def refresh_microphone_devices() -> bool:
        lifecycle.append("refresh")
        return True

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        lifecycle.append("factory")
        return replacement_worker

    monkeypatch.setattr(
        "desktop.wakeword.worker.refresh_microphone_devices",
        refresh_microphone_devices,
    )
    bridge = DesktopBridge(
        settings_path=settings_file,
        worker=previous_worker,
        worker_factory=worker_factory,
    )

    bridge.retryWakeword()

    assert previous_worker.stopped is True
    assert lifecycle == ["refresh", "factory"]
    assert replacement_worker.started is True


def test_stop_wakeword_recording_delegates_to_worker_while_recording(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    worker = FakeWorker()
    bridge = DesktopBridge(settings_path=settings_file, worker=worker)
    bridge.publish_state("recording")

    bridge.stopWakewordRecording()

    assert worker.stop_recording_calls == 1


def test_stop_wakeword_recording_is_noop_outside_recording(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    worker = FakeWorker()
    bridge = DesktopBridge(settings_path=settings_file, worker=worker)
    bridge.publish_state("listening")

    bridge.stopWakewordRecording()

    assert worker.stop_recording_calls == 0
