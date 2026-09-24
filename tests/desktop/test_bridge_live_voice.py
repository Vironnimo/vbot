"""Bridge: Live voice coexistence (pause, hands-free requests, hotkey, capabilities)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from desktop.wakeword.bridge import DesktopBridge
from tests.desktop.bridge_helpers import (
    FakeWorker,
    _write_settings,
)


class PublishingWorker(FakeWorker):
    """Publishes ``starting`` on start, like the real worker."""

    def __init__(self, bridge: DesktopBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.server_url = bridge.server_url

    def start(self) -> None:
        super().start()
        self.bridge.publish_state("starting")


def _bridge(
    tmp_path: Path,
    *,
    enabled: bool,
    **kwargs: Any,
) -> tuple[DesktopBridge, list[PublishingWorker]]:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": enabled})
    workers: list[PublishingWorker] = []

    def worker_factory(bridge: DesktopBridge) -> PublishingWorker:
        worker = PublishingWorker(bridge)
        workers.append(worker)
        return worker

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        server_url="http://a.lan:8420/",
        **kwargs,
    )
    return bridge, workers


def _running(workers: list[PublishingWorker]) -> list[PublishingWorker]:
    return [worker for worker in workers if worker.started]


# -- Pause during Live voice ---------------------------------------------------


def test_live_voice_pauses_and_resumes_an_enabled_worker(tmp_path: Path) -> None:
    bridge, workers = _bridge(tmp_path, enabled=True)
    bridge._start_worker()
    assert len(_running(workers)) == 1

    assert bridge.setLiveVoiceActive(True) == {"active": True}

    status = bridge.getWakewordStatus()
    assert workers[0].stopped is True
    assert _running(workers) == []
    assert status["enabled"] is True
    assert status["state"] == "paused"
    assert status["pause_reason"] == "live_voice"
    assert status["error_code"] is None

    assert bridge.setLiveVoiceActive(False) == {"active": False}

    assert len(workers) == 2
    assert _running(workers) == [workers[1]]
    assert bridge.getWakewordStatus()["state"] == "starting"
    assert bridge.getWakewordStatus()["pause_reason"] is None


def test_repeated_pause_requests_are_idempotent(tmp_path: Path) -> None:
    bridge, workers = _bridge(tmp_path, enabled=True)
    bridge._start_worker()

    bridge.setLiveVoiceActive(True)
    bridge.setLiveVoiceActive(True)
    bridge.setLiveVoiceActive(False)
    bridge.setLiveVoiceActive(False)

    assert len(workers) == 2
    assert _running(workers) == [workers[1]]


def test_pause_is_only_a_flag_while_voice_is_disabled(tmp_path: Path) -> None:
    bridge, workers = _bridge(tmp_path, enabled=False)

    bridge.setLiveVoiceActive(True)
    assert bridge.getWakewordStatus()["state"] == "off"
    bridge.setLiveVoiceActive(False)

    assert workers == []
    assert bridge.getWakewordStatus()["state"] == "off"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_only_boolean_true_pauses(tmp_path: Path, value: object) -> None:
    bridge, workers = _bridge(tmp_path, enabled=True)
    bridge._start_worker()

    assert bridge.setLiveVoiceActive(value) == {"active": False}
    assert _running(workers) == [workers[0]]


def test_enabling_voice_during_a_call_starts_paused(tmp_path: Path) -> None:
    bridge, workers = _bridge(tmp_path, enabled=False)
    bridge.setLiveVoiceActive(True)

    assert bridge.setWakewordEnabled(True) == {"enabled": True, "error_code": None}

    assert workers == []
    assert bridge.getWakewordStatus()["state"] == "paused"

    bridge.setLiveVoiceActive(False)
    assert _running(workers) == [workers[0]]


def test_config_changes_and_retry_stay_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from desktop.wakeword import worker as worker_module

    monkeypatch.setattr(worker_module, "refresh_microphone_devices", lambda: True)
    bridge, workers = _bridge(tmp_path, enabled=True)
    bridge._start_worker()
    bridge.setLiveVoiceActive(True)

    bridge.setWakewordConfig({"microphone": None})
    bridge.retryWakeword()

    assert _running(workers) == []
    assert bridge.getWakewordStatus()["state"] == "paused"


def test_disabling_during_a_call_turns_voice_off_and_resume_keeps_it_off(
    tmp_path: Path,
) -> None:
    bridge, workers = _bridge(tmp_path, enabled=True)
    bridge._start_worker()
    bridge.setLiveVoiceActive(True)

    bridge.setWakewordEnabled(False)
    assert bridge.getWakewordStatus()["state"] == "off"
    bridge.setLiveVoiceActive(False)

    assert _running(workers) == []
    assert bridge.getWakewordStatus()["state"] == "off"


@pytest.mark.parametrize("url", ["http://a.lan:8420/", "http://b.lan:9000/"])
def test_a_server_connection_ends_the_pause(tmp_path: Path, url: str) -> None:
    bridge, workers = _bridge(tmp_path, enabled=True)
    bridge._start_worker()
    bridge.setLiveVoiceActive(True)

    # Every successful connect replaces the page and with it any Live call.
    bridge.set_server_url(url)

    running = _running(workers)
    assert len(running) == 1
    assert running[0].server_url == url.rstrip("/")
    assert bridge.getWakewordStatus()["state"] != "paused"
    # The flag is cleared too: a later rebuild is not paused.
    bridge.setWakewordConfig({"microphone": None})
    assert len(_running(workers)) == 1
    assert bridge.getWakewordStatus()["state"] != "paused"


# -- Hands-free Live voice requests -------------------------------------------


def test_live_voice_requests_are_forwarded_to_the_page_dispatcher(tmp_path: Path) -> None:
    requests: list[tuple[str, str]] = []
    bridge, _workers = _bridge(
        tmp_path,
        enabled=False,
        live_requests=lambda action, source: requests.append((action, source)),
    )

    bridge.request_live_voice("start", "wakeword")

    assert requests == [("start", "wakeword")]


def test_live_voice_request_without_dispatcher_is_dropped(tmp_path: Path) -> None:
    bridge, _workers = _bridge(tmp_path, enabled=False)

    bridge.request_live_voice("start", "wakeword")


# -- Hotkey and capabilities ---------------------------------------------------


class FakeHotkeyController:
    supported = True

    def __init__(self) -> None:
        self.updates: list[Any] = []

    def status(self) -> dict[str, Any]:
        return {"supported": True, "enabled": False, "hotkey": {}, "error_code": None}

    def update(self, changes: Any) -> dict[str, Any]:
        self.updates.append(changes)
        return {"supported": True, "enabled": True, "hotkey": {}, "error_code": None}


def test_hotkey_methods_delegate_to_the_controller(tmp_path: Path) -> None:
    hotkey = FakeHotkeyController()
    bridge, _workers = _bridge(tmp_path, enabled=False, live_hotkey=hotkey)

    assert bridge.getLiveHotkey()["enabled"] is False
    assert bridge.setLiveHotkey({"enabled": True})["enabled"] is True
    assert hotkey.updates == [{"enabled": True}]


def test_hotkey_methods_fail_clearly_without_a_controller(tmp_path: Path) -> None:
    bridge, _workers = _bridge(tmp_path, enabled=False)

    with pytest.raises(RuntimeError):
        bridge.getLiveHotkey()


def test_capabilities_report_live_voice_integration(tmp_path: Path) -> None:
    bridge, _workers = _bridge(
        tmp_path,
        enabled=False,
        live_hotkey=FakeHotkeyController(),
        secure_origins=("http://a.lan:8420", "http://b.lan:9000"),
    )

    capabilities = bridge.getDesktopCapabilities()

    assert capabilities["liveWakeword"] is True
    assert capabilities["liveHotkey"] is True
    assert capabilities["secureOrigins"] == ["http://a.lan:8420", "http://b.lan:9000"]
