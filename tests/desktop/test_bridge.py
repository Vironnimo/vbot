"""Tests for DesktopBridge API shape and state management."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from desktop.connection import ServerEntry
from desktop.main import DesktopProbeResult, DesktopTarget
from desktop.wakeword.bridge import DesktopBridge


def _write_settings(path: Path, wakeword_config: dict | None = None) -> None:
    data = {"host": "127.0.0.1", "port": 8420}
    if wakeword_config is not None:
        data["wakeword"] = wakeword_config
    path.write_text(json.dumps(data), encoding="utf-8")


class FakeWorker:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        self.started = False

    def is_running(self) -> bool:
        return self.started


@dataclass
class FakeController:
    """Records the controller calls the bridge connection methods delegate to.

    Stands in for :class:`desktop.connection.ConnectionController` so the bridge
    can be tested without a pywebview window: it captures the host/port (and the
    Python types they arrive as) and returns canned results.
    """

    connect_status: str = "webui_available"
    connect_calls: list[tuple[str, Any]] = field(default_factory=list)
    switch_calls: list[tuple[str, Any]] = field(default_factory=list)
    add_calls: list[tuple[str, Any, str | None]] = field(default_factory=list)
    remove_calls: list[tuple[str, Any]] = field(default_factory=list)
    servers: list[ServerEntry] = field(default_factory=list)
    remove_result: bool = True

    def connect(self, host: str, port: Any, label: str | None = None) -> DesktopProbeResult:
        self.connect_calls.append((host, port))
        target = DesktopTarget(host=str(host), port=port if isinstance(port, int) else 0, url="")
        return DesktopProbeResult(status=self.connect_status, target=target)

    def switch_to(self, host: str, port: Any, label: str | None = None) -> DesktopProbeResult:
        self.switch_calls.append((host, port))
        target = DesktopTarget(host=str(host), port=port if isinstance(port, int) else 0, url="")
        return DesktopProbeResult(status=self.connect_status, target=target)

    def add_server(self, host: str, port: Any, label: str | None = None) -> ServerEntry:
        self.add_calls.append((host, port, label))
        return ServerEntry(host=host, port=port, label=label)

    def remove_server(self, host: str, port: Any) -> bool:
        self.remove_calls.append((host, port))
        return self.remove_result

    def list_servers(self) -> list[ServerEntry]:
        return list(self.servers)


def test_get_desktop_capabilities(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    capabilities = bridge.getDesktopCapabilities()

    assert capabilities == {"wakeword": True}


def test_get_wakeword_status_shape(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json", {"enabled": True, "sensitivity": 0.7})

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")
    status = bridge.getWakewordStatus()

    assert status["enabled"] is True
    assert status["sensitivity"] == 0.7
    assert status["state"] == "off"
    assert "engine" in status
    assert "microphone" in status
    assert "target_agent_id" in status
    assert "session_behavior" in status
    assert "wake_phrase" in status


def test_set_wakeword_enabled_toggles_and_persists(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(settings_path=settings_file)

    bridge.setWakewordEnabled(True)
    status = bridge.getWakewordStatus()
    assert status["enabled"] is True

    bridge.setWakewordEnabled(False)
    status = bridge.getWakewordStatus()
    assert status["enabled"] is False


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

    bridge.setWakewordConfig({"sensitivity": 0.9})

    assert len(workers) == 2
    assert workers[0].stopped is True
    assert workers[1].started is True
    assert bridge.getWakewordStatus()["sensitivity"] == 0.9


def test_set_wakeword_config_partial_update(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(settings_path=settings_file)
    bridge.setWakewordConfig({"sensitivity": 0.9, "target_agent_id": "agent-1"})

    status = bridge.getWakewordStatus()
    assert status["sensitivity"] == 0.9
    assert status["target_agent_id"] == "agent-1"


def test_set_wakeword_config_rejects_non_dict(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(settings_path=settings_file)
    # Non-dict input should be silently ignored
    bridge.setWakewordConfig({"not": "applicable"})

    status = bridge.getWakewordStatus()
    assert status["sensitivity"] == 0.5  # Default unchanged


def test_publish_state_updates_state(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    bridge.publish_state("listening")
    assert bridge.getWakewordStatus()["state"] == "listening"

    bridge.publish_state("recording")
    assert bridge.getWakewordStatus()["state"] == "recording"

    bridge.publish_state("error")
    assert bridge.getWakewordStatus()["state"] == "error"


def test_publish_state_rejects_invalid_state(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with pytest.raises(ValueError, match="Invalid wakeword state"):
        bridge.publish_state("nonexistent")


def test_bridge_thread_safety_concurrent_access(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")
    errors: list[Exception] = []

    def reader() -> None:
        for _ in range(50):
            try:
                bridge.getWakewordStatus()
            except Exception as exc:
                errors.append(exc)

    def writer() -> None:
        for i in range(50):
            try:
                bridge.publish_state("listening" if i % 2 == 0 else "recording")
                bridge.setWakewordConfig({"sensitivity": 0.5 + (i % 10) * 0.05})
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(3)] + [
        threading.Thread(target=writer) for _ in range(2)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert len(errors) == 0


# -- Connection methods (server selection delegated to the controller) -------


def test_connect_delegates_to_controller(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(connect_status="webui_available")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    result = bridge.connect("pi.lan", 9000)

    assert controller.connect_calls == [("pi.lan", 9000)]
    assert result == {"status": "webui_available"}


def test_connect_coerces_string_port_to_int(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    bridge.connect("pi.lan", "9000")

    # The screen may hand a string through; the controller receives a real int.
    assert controller.connect_calls == [("pi.lan", 9000)]


def test_connect_passes_non_numeric_port_through_for_controller_validation(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    bridge.connect("pi.lan", "not-a-port")

    # Non-numeric input is left for the controller to reject with a clear message.
    assert controller.connect_calls == [("pi.lan", "not-a-port")]


def test_connect_reports_failure_status(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(connect_status="server_unreachable")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    result = bridge.connect("pi.lan", 9000)

    assert result == {"status": "server_unreachable"}


def test_list_servers_returns_plain_payloads(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(
        servers=[ServerEntry("pi.lan", 9000, "Pi"), ServerEntry("10.0.0.5", 8500)]
    )
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    assert bridge.listServers() == [
        {"host": "pi.lan", "port": 9000, "label": "Pi"},
        {"host": "10.0.0.5", "port": 8500},
    ]


def test_add_server_delegates_and_returns_entry(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    payload = bridge.addServer("pi.lan", "9000", "Pi")

    assert controller.add_calls == [("pi.lan", 9000, "Pi")]
    assert payload == {"host": "pi.lan", "port": 9000, "label": "Pi"}


def test_add_server_normalizes_empty_label_to_none(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    bridge.addServer("pi.lan", 9000, "")

    assert controller.add_calls == [("pi.lan", 9000, None)]


def test_remove_server_reports_outcome(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(remove_result=True)
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    assert bridge.removeServer("pi.lan", 9000) == {"removed": True}
    assert controller.remove_calls == [("pi.lan", 9000)]


def test_select_server_switches_via_controller(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(connect_status="webui_available")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    result = bridge.selectServer("pi.lan", 9000)

    assert controller.switch_calls == [("pi.lan", 9000)]
    assert result == {"status": "webui_available"}


def test_connection_methods_raise_without_controller(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with pytest.raises(RuntimeError, match="no connection controller"):
        bridge.connect("pi.lan", 9000)
    with pytest.raises(RuntimeError, match="no connection controller"):
        bridge.listServers()


def test_wakeword_and_connection_methods_share_one_bridge(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json", {"enabled": False})
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    # The same bridge object serves both surfaces (it is the window's single
    # js_api across load_url navigation): wakeword status and server connect.
    assert bridge.getWakewordStatus()["state"] == "off"
    assert bridge.getDesktopCapabilities() == {"wakeword": True}
    bridge.connect("pi.lan", 9000)
    assert controller.connect_calls == [("pi.lan", 9000)]
