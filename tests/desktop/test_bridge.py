"""The pywebview ``js_api`` facade: capabilities, system actions, Voice, servers, hotkey."""

from __future__ import annotations

import base64
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from desktop.bridge import DesktopBridge
from desktop.connection import PreparedConnection, ServerEntry
from desktop.main import DesktopProbeResult, DesktopTarget
from desktop.page_events import PageEventDispatcher
from desktop.system_actions import DesktopSystemActions
from desktop.wakeword.controller import VoiceController
from desktop.wakeword.engine import MAX_CUSTOM_WAKEWORD_MODEL_BYTES, WakewordModelError

VOICE_METHODS = {
    "getVoiceStatus",
    "setVoiceEnabled",
    "updateVoiceConfig",
    "listMicrophones",
    "listWakewordModels",
    "importWakewordModel",
    "deleteWakewordModel",
    "retryVoice",
    "stopVoiceRecording",
    "startVoiceCalibration",
    "restartVoiceCalibration",
    "stopVoiceCalibration",
}


class FakeVoice:
    """Records the Voice controller calls and returns a marker per method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any) -> Any:
            self.calls.append((name, args))
            return {"from": name}

        return call


@dataclass
class FakeConnection:
    """Records the connection calls the bridge's server methods delegate to."""

    connect_status: str = "webui_available"
    active_url: str | None = None
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
                result=result, navigation_url="http://pi.lan:9000/?accessor=desktop"
            )
        return PreparedConnection(
            result=result, error_title="Server unreachable", error_body="Try again."
        )

    def add_server(self, host: str, port: Any, label: str | None = None) -> ServerEntry:
        self.add_calls.append((host, port, label))
        return ServerEntry(host=host, port=port, label=label)

    def remove_server(self, host: str, port: Any) -> bool:
        self.remove_calls.append((host, port))
        return self.remove_result

    def list_servers(self) -> list[ServerEntry]:
        return list(self.servers)

    def active_server_url(self) -> str | None:
        return self.active_url


class FakeHotkey:
    def __init__(self, *, supported: bool = True) -> None:
        self.supported = supported
        self.updates: list[Any] = []

    def status(self) -> dict[str, Any]:
        return {"supported": self.supported, "enabled": False, "hotkey": None, "error_code": None}

    def update(self, changes: Any) -> dict[str, Any]:
        self.updates.append(changes)
        return {**self.status(), "enabled": True}


def _bridge(**kwargs: Any) -> tuple[DesktopBridge, FakeVoice]:
    voice = FakeVoice()
    return DesktopBridge(voice=cast(VoiceController, voice), **kwargs), voice


# -- Exposure and capabilities -----------------------------------------------------


def test_pywebview_sees_only_the_bridge_methods() -> None:
    bridge, _ = _bridge()

    public_attributes = [name for name in vars(bridge) if not name.startswith("_")]
    public_methods = {
        name for name, member in inspect.getmembers(bridge, callable) if not name.startswith("_")
    }

    assert public_attributes == []
    assert public_methods == VOICE_METHODS | {
        "getDesktopCapabilities",
        "setClipboardText",
        "getClipboardText",
        "openExternalUrl",
        "getLiveHotkey",
        "setLiveHotkey",
        "connect",
        "listServers",
        "addServer",
        "removeServer",
        "selectServer",
    }


def test_capabilities_announce_the_voice_bridge_version() -> None:
    bridge, _ = _bridge(
        live_hotkey=FakeHotkey(), secure_origins=("http://a.lan:8420", "http://pi.lan:9000")
    )

    assert bridge.getDesktopCapabilities() == {
        "wakeword": True,
        "voiceApi": 2,
        "serverSelection": True,
        "contextMenu": True,
        "liveHotkey": True,
        "secureOrigins": ["http://a.lan:8420", "http://pi.lan:9000"],
    }


@pytest.mark.parametrize("hotkey", [None, FakeHotkey(supported=False)])
def test_capabilities_report_a_missing_or_unsupported_hotkey(hotkey: FakeHotkey | None) -> None:
    bridge, _ = _bridge(live_hotkey=hotkey)

    assert bridge.getDesktopCapabilities()["liveHotkey"] is False


def test_system_actions_validate_and_delegate() -> None:
    copied: list[str] = []
    opened: list[str] = []

    def open_url(url: str) -> bool:
        opened.append(url)
        return True

    bridge, _ = _bridge(
        system_actions=DesktopSystemActions(
            clipboard_writer=copied.append,
            clipboard_reader=lambda: "paste me",
            external_url_opener=open_url,
        )
    )

    assert bridge.setClipboardText("copy me") == {"copied": True}
    assert bridge.getClipboardText() == "paste me"
    assert bridge.openExternalUrl("https://example.com/path?q=1") == {"opened": True}
    assert copied == ["copy me"]
    assert opened == ["https://example.com/path?q=1"]


# -- Voice -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "args", "controller_method"),
    [
        ("getVoiceStatus", (), "status"),
        ("setVoiceEnabled", (True,), "set_enabled"),
        ("updateVoiceConfig", ({"echo_cancellation": False},), "update_config"),
        ("listMicrophones", (), "list_microphones"),
        ("listWakewordModels", (), "list_models"),
        ("deleteWakewordModel", ("custom/computer",), "delete_model"),
        ("retryVoice", (), "retry"),
        ("stopVoiceRecording", (), "stop_recording"),
        ("startVoiceCalibration", ("builtin/okay_nabu",), "start_calibration"),
        ("restartVoiceCalibration", (), "restart_calibration"),
        ("stopVoiceCalibration", (), "stop_calibration"),
    ],
)
def test_voice_methods_delegate_to_the_controller(
    method: str, args: tuple[Any, ...], controller_method: str
) -> None:
    bridge, voice = _bridge()

    result = getattr(bridge, method)(*args)

    assert result == {"from": controller_method}
    assert voice.calls == [(controller_method, args)]


def test_model_import_decodes_the_base64_content() -> None:
    bridge, voice = _bridge()

    result = bridge.importWakewordModel(
        "computer.tflite", base64.b64encode(b"tflite-model").decode("ascii")
    )

    assert result == {"from": "import_model"}
    assert voice.calls == [("import_model", ("computer.tflite", b"tflite-model"))]


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        (None, "AAAA"),
        ("model.tflite", None),
        ("model.tflite", "not base64!"),
        ("model.tflite", "A" * (4 * ((MAX_CUSTOM_WAKEWORD_MODEL_BYTES + 2) // 3) + 4)),
    ],
    ids=["filename", "content-type", "invalid-base64", "oversized"],
)
def test_model_import_rejects_invalid_content_before_the_controller(
    filename: Any, content: Any
) -> None:
    bridge, voice = _bridge()

    with pytest.raises(WakewordModelError):
        bridge.importWakewordModel(filename, content)
    assert voice.calls == []


def test_voice_methods_reach_a_real_controller(tmp_path: Path) -> None:
    page_events = PageEventDispatcher()
    voice = VoiceController(
        settings_path=tmp_path / "settings.json",
        server_url="",
        sink=page_events,
        live_requests=page_events.request_live,
    )
    bridge = DesktopBridge(voice=voice)
    try:
        assert bridge.getVoiceStatus()["state"] == "off"
        assert bridge.setVoiceEnabled(False) == {"enabled": False, "error_code": None}
    finally:
        voice.close()
        page_events.close()


# -- Live voice hotkey ---------------------------------------------------------------


def test_live_hotkey_methods_delegate() -> None:
    hotkey = FakeHotkey()
    bridge, _ = _bridge(live_hotkey=hotkey)

    assert bridge.getLiveHotkey()["supported"] is True
    assert bridge.setLiveHotkey({"enabled": True})["enabled"] is True
    assert hotkey.updates == [{"enabled": True}]


def test_live_hotkey_methods_raise_without_a_hotkey() -> None:
    bridge, _ = _bridge()

    with pytest.raises(RuntimeError, match="not available"):
        bridge.getLiveHotkey()
    with pytest.raises(RuntimeError, match="not available"):
        bridge.setLiveHotkey({"enabled": True})


# -- Server selection ------------------------------------------------------------------


def test_connect_prepares_the_result_for_javascript_navigation() -> None:
    connection = FakeConnection()
    bridge, _ = _bridge(connection=connection)

    result = bridge.connect("pi.lan", 9000)

    assert connection.prepare_calls == [("pi.lan", 9000)]
    assert result == {"status": "webui_available", "url": "http://pi.lan:9000/?accessor=desktop"}


def test_connect_reports_a_failure_status() -> None:
    bridge, _ = _bridge(connection=FakeConnection(connect_status="server_unreachable"))

    assert bridge.connect("pi.lan", 9000) == {
        "status": "server_unreachable",
        "error_title": "Server unreachable",
        "error_body": "Try again.",
    }


@pytest.mark.parametrize(
    ("port", "expected"),
    [("9000", 9000), (" 9000 ", 9000), ("not-a-port", "not-a-port"), (True, 1), (9000, 9000)],
)
def test_ports_are_coerced_where_possible(port: Any, expected: Any) -> None:
    connection = FakeConnection()
    bridge, _ = _bridge(connection=connection)

    bridge.selectServer("pi.lan", port)

    # Non-numeric input is left for the controller to reject with a clear message.
    assert connection.prepare_calls == [("pi.lan", expected)]


def test_list_servers_marks_the_active_server() -> None:
    connection = FakeConnection(
        servers=[ServerEntry("pi.lan", 9000, "Pi"), ServerEntry("10.0.0.5", 8500)],
        active_url="http://pi.lan:9000/",
    )
    bridge, _ = _bridge(connection=connection)

    assert bridge.listServers() == [
        {"host": "pi.lan", "port": 9000, "label": "Pi", "active": True},
        {"host": "10.0.0.5", "port": 8500, "active": False},
    ]


def test_list_servers_without_an_active_server() -> None:
    connection = FakeConnection(servers=[ServerEntry("pi.lan", 9000)])
    bridge, _ = _bridge(connection=connection)

    assert bridge.listServers() == [{"host": "pi.lan", "port": 9000, "active": False}]


def test_add_and_remove_server_delegate() -> None:
    connection = FakeConnection(remove_result=True)
    bridge, _ = _bridge(connection=connection)

    assert bridge.addServer("pi.lan", "9000", "Pi") == {
        "host": "pi.lan",
        "port": 9000,
        "label": "Pi",
    }
    bridge.addServer("pi.lan", 9000, "")
    assert bridge.removeServer("pi.lan", 9000) == {"removed": True}
    assert connection.add_calls == [("pi.lan", 9000, "Pi"), ("pi.lan", 9000, None)]
    assert connection.remove_calls == [("pi.lan", 9000)]


def test_server_methods_raise_without_a_connection() -> None:
    bridge, _ = _bridge()

    for call in (
        lambda: bridge.connect("pi.lan", 9000),
        bridge.listServers,
        lambda: bridge.addServer("pi.lan", 9000),
        lambda: bridge.removeServer("pi.lan", 9000),
        lambda: bridge.selectServer("pi.lan", 9000),
    ):
        with pytest.raises(RuntimeError, match="not available"):
            call()
