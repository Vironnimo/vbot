"""The pywebview ``js_api`` facade: capabilities, system actions, Voice, servers, hotkey."""

from __future__ import annotations

import base64
import inspect
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from desktop import connection as desktop_connection
from desktop.bridge import BridgeError, DesktopBridge
from desktop.connection import ConnectionController
from desktop.main import DesktopProbeResult, DesktopTarget
from desktop.page_events import PageEventDispatcher
from desktop.system_actions import DesktopSystemActions
from desktop.wakeword.config import VoiceConfigError
from desktop.wakeword.controller import VoiceControlError, VoiceController
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
    """Records the Voice controller calls and returns a marker per method (or raises ``error``)."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.error = error

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any) -> Any:
            self.calls.append((name, args))
            if self.error is not None:
                raise self.error
            return {"from": name}

        return call


class FakeHotkey:
    def __init__(self, *, supported: bool = True) -> None:
        self.supported = supported
        self.updates: list[Any] = []

    def status(self) -> dict[str, Any]:
        return {"supported": self.supported, "enabled": False, "hotkey": None, "error_code": None}

    def update(self, changes: Any) -> dict[str, Any]:
        self.updates.append(changes)
        return {**self.status(), "enabled": True}


def _bridge(*, error: Exception | None = None, **kwargs: Any) -> tuple[DesktopBridge, FakeVoice]:
    voice = FakeVoice(error)
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


def test_pywebview_reads_the_real_parameter_names() -> None:
    bridge, _ = _bridge()

    # pywebview generates the JavaScript stubs from ``getfullargspec(...).args[1:]``.
    assert inspect.getfullargspec(bridge.importWakewordModel).args[1:] == [
        "filename",
        "content_base64",
    ]
    assert inspect.getfullargspec(bridge.addServer).args[1:] == ["host", "port", "label"]


# -- Error contract --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code", "logged"),
    [
        (
            VoiceConfigError("Sensitivity must be a number", field="model_sensitivities"),
            "voice_config_invalid",
            "field=model_sensitivities): Sensitivity must be a number",
        ),
        (
            WakewordModelError("Deactivate it first.", error_code="wakeword_model_active"),
            "wakeword_model_active",
            "Deactivate it first.",
        ),
        (
            VoiceControlError("No calibration is running.", error_code="calibration_inactive"),
            "calibration_inactive",
            "No calibration is running.",
        ),
    ],
)
def test_a_known_failure_rejects_with_exactly_its_error_code(
    error: Exception, code: str, logged: str, caplog: pytest.LogCaptureFixture
) -> None:
    bridge, _ = _bridge(error=error)

    with (
        caplog.at_level(logging.WARNING, logger="vbot.desktop.bridge"),
        pytest.raises(BridgeError) as rejected,
    ):
        bridge.updateVoiceConfig({"echo_cancellation": True})

    assert str(rejected.value) == code
    assert rejected.value.__cause__ is error
    assert f"Desktop bridge updateVoiceConfig failed (error_code={code}" in caplog.text
    assert logged in caplog.text


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (OSError("disk full"), "disk full"),
        (RuntimeError("timeout"), "The Desktop could not complete retryVoice"),
    ],
    ids=["keeps-its-message", "never-looks-like-an-error-code"],
)
def test_an_unexpected_failure_logs_its_traceback_and_rejects_without_an_error_code(
    error: Exception, message: str, caplog: pytest.LogCaptureFixture
) -> None:
    bridge, _ = _bridge(error=error)

    with (
        caplog.at_level(logging.ERROR, logger="vbot.desktop.bridge"),
        pytest.raises(type(error)) as rejected,
    ):
        bridge.retryVoice()

    assert str(rejected.value) == message
    assert caplog.records[-1].exc_info is not None


@pytest.mark.parametrize(
    ("hotkey", "live_hotkey"),
    [(FakeHotkey(), True), (None, False), (FakeHotkey(supported=False), False)],
    ids=["supported-hotkey", "no-hotkey", "unsupported-hotkey"],
)
def test_capabilities_announce_the_voice_bridge_version_and_optional_services(
    hotkey: FakeHotkey | None, live_hotkey: bool
) -> None:
    bridge, _ = _bridge(
        live_hotkey=hotkey, secure_origins=("http://a.lan:8420", "http://pi.lan:9000")
    )

    assert bridge.getDesktopCapabilities() == {
        "wakeword": True,
        "voiceApi": 2,
        "serverSelection": True,
        "contextMenu": True,
        "liveHotkey": live_hotkey,
        "secureOrigins": ["http://a.lan:8420", "http://pi.lan:9000"],
    }


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

    with pytest.raises(BridgeError, match="^wakeword_model_invalid$") as rejected:
        bridge.importWakewordModel(filename, content)
    assert isinstance(rejected.value.__cause__, WakewordModelError)
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


# -- Server selection ------------------------------------------------------------------


def _server_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str = "webui_available"
) -> tuple[DesktopBridge, list[tuple[str, int]]]:
    """A bridge over a real connection controller whose probe records its targets."""

    monkeypatch.setattr(desktop_connection, "uuid4", lambda: SimpleNamespace(hex="s1"))
    probed: list[tuple[str, int]] = []

    def probe(target: DesktopTarget) -> DesktopProbeResult:
        if target.configuration_error is not None:
            return DesktopProbeResult(status="invalid_target", target=target)
        probed.append((target.host, target.port))
        return DesktopProbeResult(status=status, target=target)

    controller = ConnectionController(settings_file=tmp_path / "settings.json", probe=probe)
    bridge, _ = _bridge(connection=controller)
    return bridge, probed


@pytest.mark.parametrize("method", ["connect", "selectServer"])
def test_connecting_returns_the_prepared_result_for_javascript_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    bridge, probed = _server_bridge(tmp_path, monkeypatch)
    failing, _ = _server_bridge(tmp_path / "failing", monkeypatch, "server_unreachable")

    assert getattr(bridge, method)("pi.lan", 9000) == {
        "status": "webui_available",
        "url": "http://pi.lan:9000/?accessor=desktop&desktop_session=s1",
    }
    assert probed == [("pi.lan", 9000)]
    failure = getattr(failing, method)("pi.lan", 9000)
    assert set(failure) == {"status", "error_title", "error_body"}
    assert failure["status"] == "server_unreachable"


@pytest.mark.parametrize(
    ("port", "probed_port"),
    [("9000", 9000), (" 9000 ", 9000), (True, 1), (9000, 9000), ("not-a-port", None)],
)
def test_ports_are_coerced_where_possible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: Any, probed_port: int | None
) -> None:
    bridge, probed = _server_bridge(tmp_path, monkeypatch)

    result = bridge.selectServer("pi.lan", port)

    # Non-numeric input reaches the controller, which rejects it with a clear message.
    if probed_port is None:
        assert probed == []
        assert result["status"] == "invalid_target"
    else:
        assert probed == [("pi.lan", probed_port)]


def test_servers_are_remembered_listed_with_the_active_one_and_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, probed = _server_bridge(tmp_path, monkeypatch)
    assert bridge.addServer("10.0.0.5", 8500, "") == {"host": "10.0.0.5", "port": 8500}
    assert bridge.listServers() == [{"host": "10.0.0.5", "port": 8500, "active": False}]

    bridge.connect("pi.lan", 9000)
    assert bridge.addServer("pi.lan", "9000", "Pi") == {
        "host": "pi.lan",
        "port": 9000,
        "label": "Pi",
    }

    assert bridge.listServers() == [
        {"host": "10.0.0.5", "port": 8500, "active": False},
        {"host": "pi.lan", "port": 9000, "label": "Pi", "active": True},
    ]
    assert bridge.removeServer("10.0.0.5", "8500") == {"removed": True}
    assert bridge.removeServer("10.0.0.5", 8500) == {"removed": False}
    assert [server["host"] for server in bridge.listServers()] == ["pi.lan"]
    assert probed == [("pi.lan", 9000)]


def test_optional_services_raise_without_their_owner() -> None:
    bridge, _ = _bridge()

    hotkey_calls: tuple[Callable[[], object], ...] = (
        bridge.getLiveHotkey,
        lambda: bridge.setLiveHotkey({"enabled": True}),
    )
    for call in hotkey_calls:
        with pytest.raises(RuntimeError, match="hotkey is not available"):
            call()
    server_calls: tuple[Callable[[], object], ...] = (
        lambda: bridge.connect("pi.lan", 9000),
        bridge.listServers,
        lambda: bridge.addServer("pi.lan", 9000),
        lambda: bridge.removeServer("pi.lan", 9000),
        lambda: bridge.selectServer("pi.lan", 9000),
    )
    for call in server_calls:
        with pytest.raises(RuntimeError, match="Server selection is not available"):
            call()
