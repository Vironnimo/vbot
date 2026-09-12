"""Bridge: connections behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from desktop.connection import PreparedConnection, ServerEntry
from desktop.main import DesktopProbeResult, DesktopTarget
from desktop.wakeword.bridge import DesktopBridge
from tests.desktop.bridge_helpers import (
    _write_settings,
)


@dataclass
class FakeController:
    """Records the controller calls the bridge connection methods delegate to.

    Stands in for :class:`desktop.connection.ConnectionController` so the bridge
    can be tested without a pywebview window: it captures the host/port (and the
    Python types they arrive as) and returns canned results.
    """

    connect_status: str = "webui_available"
    prepare_calls: list[tuple[str, Any]] = field(default_factory=list)
    add_calls: list[tuple[str, Any, str | None]] = field(default_factory=list)
    remove_calls: list[tuple[str, Any]] = field(default_factory=list)
    servers: list[ServerEntry] = field(default_factory=list)
    remove_result: bool = True

    def prepare_connect(self, host: str, port: Any, label: str | None = None) -> PreparedConnection:
        self.prepare_calls.append((host, port))
        target = DesktopTarget(host=str(host), port=port if isinstance(port, int) else 0, url="")
        result = DesktopProbeResult(status=self.connect_status, target=target)
        if self.connect_status == "webui_available":
            return PreparedConnection(
                result=result,
                navigation_url="http://pi.lan:9000/?accessor=desktop",
            )
        return PreparedConnection(
            result=result,
            error_title="Server unreachable",
            error_body="Try again.",
        )

    def add_server(self, host: str, port: Any, label: str | None = None) -> ServerEntry:
        self.add_calls.append((host, port, label))
        return ServerEntry(host=host, port=port, label=label)

    def remove_server(self, host: str, port: Any) -> bool:
        self.remove_calls.append((host, port))
        return self.remove_result

    def list_servers(self) -> list[ServerEntry]:
        return list(self.servers)


# -- Connection methods (server selection delegated to the controller) -------
def test_connect_prepares_controller_result_for_javascript_navigation(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(connect_status="webui_available")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    result = bridge.connect("pi.lan", 9000)

    assert controller.prepare_calls == [("pi.lan", 9000)]
    assert result == {
        "status": "webui_available",
        "url": "http://pi.lan:9000/?accessor=desktop",
    }


def test_connect_coerces_string_port_to_int(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    bridge.connect("pi.lan", "9000")

    # The screen may hand a string through; the controller receives a real int.
    assert controller.prepare_calls == [("pi.lan", 9000)]


def test_connect_passes_non_numeric_port_through_for_controller_validation(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    bridge.connect("pi.lan", "not-a-port")

    # Non-numeric input is left for the controller to reject with a clear message.
    assert controller.prepare_calls == [("pi.lan", "not-a-port")]


def test_connect_reports_failure_status(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(connect_status="server_unreachable")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    result = bridge.connect("pi.lan", 9000)

    assert result == {
        "status": "server_unreachable",
        "error_title": "Server unreachable",
        "error_body": "Try again.",
    }


def test_list_servers_returns_plain_payloads(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(
        servers=[ServerEntry("pi.lan", 9000, "Pi"), ServerEntry("10.0.0.5", 8500)]
    )
    bridge = DesktopBridge(
        settings_path=tmp_path / "settings.json",
        connection=controller,
        server_url="http://pi.lan:9000/",
    )

    assert bridge.listServers() == [
        {"host": "pi.lan", "port": 9000, "label": "Pi", "active": True},
        {"host": "10.0.0.5", "port": 8500, "active": False},
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


def test_select_server_prepares_bridge_safe_navigation(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(connect_status="webui_available")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    result = bridge.selectServer("pi.lan", 9000)

    assert controller.prepare_calls == [("pi.lan", 9000)]
    assert result == {
        "status": "webui_available",
        "url": "http://pi.lan:9000/?accessor=desktop",
    }


def test_connection_methods_raise_without_controller(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with pytest.raises(RuntimeError):
        bridge.connect("pi.lan", 9000)
    with pytest.raises(RuntimeError):
        bridge.listServers()


def test_wakeword_and_connection_methods_share_one_bridge(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json", {"enabled": False})
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    # The same bridge object serves both surfaces (it is the window's single
    # js_api across load_url navigation): wakeword status and server connect.
    assert bridge.getWakewordStatus()["state"] == "off"
    assert bridge.getDesktopCapabilities() == {
        "wakeword": True,
        "serverSelection": True,
        "contextMenu": True,
    }
    bridge.connect("pi.lan", 9000)
    assert controller.prepare_calls == [("pi.lan", 9000)]
