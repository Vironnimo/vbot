"""The pywebview ``js_api`` facade: capabilities, system actions, microphone, Voice, servers."""

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
from desktop.bridge import BridgeError, DesktopBridge, configure_bridge_logging
from desktop.connection import ConnectionController
from desktop.page_events import PageEventDispatcher
from desktop.page_permissions import PagePermissions
from desktop.probe import DesktopProbeResult, DesktopTarget
from desktop.restart import RestartError
from desktop.speech.microphone import MicrophoneService
from desktop.system_actions import DesktopSystemActions
from desktop.wakeword.config import VoiceConfigError
from desktop.wakeword.contracts import VoiceControlError
from desktop.wakeword.controller import VoiceController
from desktop.wakeword.engine import MAX_CUSTOM_WAKEWORD_MODEL_BYTES, WakewordModelError

VOICE_METHODS = {
    "getVoiceStatus",
    "setVoiceEnabled",
    "updateVoiceConfig",
    "listWakewordModels",
    "importWakewordModel",
    "deleteWakewordModel",
    "retryVoice",
    "stopVoiceRecording",
    "startVoiceCalibration",
    "restartVoiceCalibration",
    "stopVoiceCalibration",
}


MICROPHONE_METHODS = {"getMicrophone", "setMicrophone", "listMicrophones"}
OWN_SERVER = "https://own.example.com"


class FakeVoice:
    """Records the owner's calls and returns a marker per method (or raises ``error``)."""

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


def _page_permissions(
    url: Callable[[], str | None] = lambda: OWN_SERVER,
    confirm: Callable[[str, str], bool] = lambda _title, _message: False,
) -> PagePermissions:
    """Permissions for the loaded page at ``url``; the installation's own server is OWN_SERVER."""
    permissions = PagePermissions(
        page_url=url, own_server=lambda page: page == OWN_SERVER, confirm=confirm
    )
    permissions.page_loaded()
    return permissions


def _bridge(*, error: Exception | None = None, **kwargs: Any) -> tuple[DesktopBridge, FakeVoice]:
    """A bridge whose page is the installation's own server's, unless ``permissions`` say."""
    voice = FakeVoice(error)
    kwargs.setdefault("microphone", cast(MicrophoneService, FakeVoice()))
    kwargs.setdefault("permissions", _page_permissions())
    return DesktopBridge(voice=cast(VoiceController, voice), **kwargs), voice


# -- Exposure and capabilities -----------------------------------------------------


def test_pywebview_sees_only_the_bridge_methods() -> None:
    bridge, _ = _bridge()

    public_attributes = [name for name in vars(bridge) if not name.startswith("_")]
    public_methods = {
        name for name, member in inspect.getmembers(bridge, callable) if not name.startswith("_")
    }

    assert public_attributes == []
    assert public_methods == VOICE_METHODS | MICROPHONE_METHODS | {
        "getDesktopCapabilities",
        "setClipboardText",
        "getClipboardText",
        "openExternalUrl",
        "getLiveHotkey",
        "setLiveHotkey",
        "getDictation",
        "setDictation",
        "getZoom",
        "setZoom",
        "connect",
        "startPairing",
        "listServers",
        "addServer",
        "removeServer",
        "selectServer",
        "cancelPairing",
        "forgetSignIn",
        "getDesktopUpdate",
        "restartDesktop",
        "getComputerSharing",
        "setComputerSharing",
    }


def test_pywebview_reads_the_real_parameter_names() -> None:
    bridge, _ = _bridge()

    # pywebview generates the JavaScript stubs from ``getfullargspec(...).args[1:]``.
    assert inspect.getfullargspec(bridge.importWakewordModel).args[1:] == [
        "filename",
        "content_base64",
    ]
    assert inspect.getfullargspec(bridge.addServer).args[1:] == ["host", "port", "label"]
    assert inspect.getfullargspec(bridge.connect).args[1:] == ["host", "port", "screen"]
    assert inspect.getfullargspec(bridge.startPairing).args[1:] == [
        "host",
        "port",
        "name",
        "screen",
    ]


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
    "error", [VoiceControlError("Unavailable", error_code="unavailable"), OSError("disk full")]
)
def test_pywebview_does_not_repeat_a_bridge_failure(
    error: Exception, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = logging.getLogger("pywebview")
    monkeypatch.setattr(native, "filters", [])
    configure_bridge_logging()
    bridge, _ = _bridge(error=error)
    with caplog.at_level(logging.WARNING):
        try:
            bridge.retryVoice()
        except Exception:
            native.error("Native bridge traceback")
        native.error("Unrelated native failure")
    assert len([record for record in caplog.records if record.name == "vbot.desktop.bridge"]) == 1
    assert [record.getMessage() for record in caplog.records if record.name == "pywebview"] == [
        "Unrelated native failure"
    ]


@pytest.mark.parametrize(
    ("hotkey", "available"),
    [(FakeHotkey(), True), (None, False), (FakeHotkey(supported=False), False)],
    ids=["supported-shortcuts", "no-shortcuts", "unsupported-shortcuts"],
)
def test_capabilities_announce_the_voice_bridge_version_and_optional_services(
    hotkey: FakeHotkey | None, available: bool
) -> None:
    bridge, _ = _bridge(
        live_hotkey=hotkey,
        dictation=hotkey,
        zoom=hotkey,
        secure_origins=("http://a.lan:8420", "http://pi.lan:9000"),
    )

    assert bridge.getDesktopCapabilities() == {
        "wakeword": True,
        "voiceApi": 3,
        "serverSelection": True,
        "contextMenu": True,
        "microphone": True,
        "liveHotkey": available,
        "dictation": available,
        "zoom": available,
        "secureOrigins": ["http://a.lan:8420", "http://pi.lan:9000"],
        "restart": False,
        "signIn": False,
        "computerSharing": False,
    }


class FakeRestart:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.restarts = 0

    def status(self) -> dict[str, bool]:
        return {"pending": True, "restarting": False, "failed": False}

    def restart(self) -> None:
        self.restarts += 1
        if self.error is not None:
            raise self.error


def test_the_update_restart_is_offered_only_by_a_packaged_desktop() -> None:
    source, _ = _bridge()
    assert source.getDesktopUpdate() == {"pending": False, "restarting": False, "failed": False}
    with pytest.raises(BridgeError, match="^restart_unavailable$"):
        source.restartDesktop()

    restart = FakeRestart()
    packaged, _ = _bridge(restart=restart)
    assert packaged.getDesktopCapabilities()["restart"] is True
    assert packaged.getDesktopUpdate() == restart.status()
    assert packaged.restartDesktop() == {"accepted": True}
    assert restart.restarts == 1

    current, _ = _bridge(
        restart=FakeRestart(RestartError("current", error_code="no_update_pending"))
    )
    with pytest.raises(BridgeError, match="^no_update_pending$"):
        current.restartDesktop()


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


# -- Microphone --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "args", "service_method"),
    [
        ("getMicrophone", (), "status"),
        ("setMicrophone", ({"echo_cancellation": False},), "update"),
        ("listMicrophones", (), "list_devices"),
    ],
)
def test_microphone_methods_delegate_to_the_service(
    method: str, args: tuple[Any, ...], service_method: str
) -> None:
    microphone = FakeVoice()
    bridge, _ = _bridge(microphone=cast(MicrophoneService, microphone))

    result = getattr(bridge, method)(*args)

    assert result == {"from": service_method}
    assert microphone.calls == [(service_method, args)]


# -- Voice -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "args", "controller_method"),
    [
        ("getVoiceStatus", (), "status"),
        ("setVoiceEnabled", (True,), "set_enabled"),
        ("updateVoiceConfig", ({"default_session_behavior": "new"},), "update_config"),
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
    microphone = MicrophoneService(settings_path=tmp_path / "settings.json")
    voice = VoiceController(
        settings_path=tmp_path / "settings.json",
        microphone=microphone,
        server_url="",
        sink=page_events,
        live_requests=page_events.request_live,
    )
    bridge = DesktopBridge(voice=voice, microphone=microphone)
    try:
        assert bridge.getVoiceStatus()["state"] == "off"
        assert bridge.setVoiceEnabled(False) == {"enabled": False, "error_code": None}
    finally:
        voice.close()
        page_events.close()


# -- Live voice hotkey and Desktop dictation -----------------------------------------


@pytest.mark.parametrize(
    ("owner", "get", "set"),
    [
        ("live_hotkey", "getLiveHotkey", "setLiveHotkey"),
        ("dictation", "getDictation", "setDictation"),
    ],
)
def test_shortcut_methods_delegate(owner: str, get: str, set: str) -> None:
    hotkey = FakeHotkey()
    bridge, _ = _bridge(**{owner: cast(Any, hotkey)})

    assert getattr(bridge, get)()["supported"] is True
    assert getattr(bridge, set)({"enabled": True})["enabled"] is True
    assert hotkey.updates == [{"enabled": True}]


# -- Server selection ------------------------------------------------------------------


def _server_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str = "webui_available",
    **kwargs: Any,
) -> tuple[DesktopBridge, list[tuple[str, int]], ConnectionController]:
    """A bridge over a real connection controller whose probe records its targets."""

    monkeypatch.setattr(desktop_connection, "uuid4", lambda: SimpleNamespace(hex="s1"))
    probed: list[tuple[str, int]] = []

    def probe(target: DesktopTarget) -> DesktopProbeResult:
        if target.configuration_error is not None:
            return DesktopProbeResult(status="invalid_target", target=target)
        probed.append((target.host, target.port))
        return DesktopProbeResult(status=status, target=target)

    controller = ConnectionController(settings_file=tmp_path / "settings.json", probe=probe)
    bridge, _ = _bridge(connection=controller, **kwargs)
    return bridge, probed, controller


@pytest.mark.parametrize("method", ["connect", "selectServer"])
def test_connecting_returns_the_prepared_result_for_javascript_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    bridge, probed, controller = _server_bridge(tmp_path, monkeypatch)
    failing, failing_probed, failing_controller = _server_bridge(
        tmp_path / "failing", monkeypatch, "server_unreachable"
    )

    def call(target: DesktopBridge, owner: ConnectionController) -> dict[str, Any]:
        # The connection screen sends its key with connect.
        screen = (owner.screen_key,) if method == "connect" else ()
        result: dict[str, Any] = getattr(target, method)("pi.lan", 9000, *screen)
        return result

    assert call(bridge, controller) == {
        "status": "webui_available",
        "url": "http://pi.lan:9000/?accessor=desktop&desktop_session=s1",
    }
    assert probed == [("pi.lan", 9000)]
    failure = call(failing, failing_controller)
    assert set(failure) == {"status", "error_title", "error_body"}
    assert failure["status"] == "server_unreachable"
    # A connect the user starts fails at once; only the launch connect waits.
    assert failing_probed == [("pi.lan", 9000)]


@pytest.mark.parametrize(
    ("port", "probed_port"),
    [("9000", 9000), (" 9000 ", 9000), (True, 1), (9000, 9000), ("not-a-port", None)],
)
def test_ports_are_coerced_where_possible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: Any, probed_port: int | None
) -> None:
    bridge, probed, _ = _server_bridge(tmp_path, monkeypatch)

    result = bridge.selectServer("pi.lan", port)

    # Non-numeric input reaches the controller, which rejects it with a clear message.
    if probed_port is None:
        assert probed == []
        assert result["status"] == "invalid_target"
    else:
        assert probed == [("pi.lan", probed_port)]


class FakeSignInConnection:
    """The sign-in part of the connection delegate, recording what the bridge asks."""

    SCREEN = "screen-key"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def is_screen(self, key: Any) -> bool:
        return isinstance(key, str) and key == self.SCREEN

    def is_remembered(self, host: Any, port: Any) -> bool:
        return True

    def prepare_connect(
        self, host: str, port: Any, label: str | None = None, *, on_screen: bool = False
    ) -> SimpleNamespace:
        self.calls.append(("prepare_connect", host, port, on_screen))
        return SimpleNamespace(
            to_bridge_payload=lambda: {"status": "pairing_required", "offer": {"name": "PC"}}
        )

    def start_pairing(self, host: str, port: Any, name: Any = None) -> SimpleNamespace:
        self.calls.append(("start_pairing", host, port, name))
        return SimpleNamespace(
            to_bridge_payload=lambda: {"status": "pairing_required", "pairing": {"code": "K7M2"}}
        )

    def cancel_pairing(self) -> bool:
        self.calls.append(("cancel_pairing",))
        return True

    def forget_sign_in(self, host: str, port: Any) -> bool:
        self.calls.append(("forget_sign_in", host, port))
        return False


def test_only_the_connection_screen_connects_pairs_and_forgets_a_sign_in() -> None:
    connection = FakeSignInConnection()
    bridge, _ = _bridge(connection=connection)
    server, screen = "https://vbot.example.com", FakeSignInConnection.SCREEN

    # The screen offers pairing and asks for the code; a WebUI page knows only the offer's start.
    assert bridge.connect(server, None, screen)["offer"] == {"name": "PC"}
    assert bridge.startPairing(server, "443", "Work laptop", screen)["pairing"] == {"code": "K7M2"}
    assert bridge.selectServer(server, None)["status"] == "pairing_required"
    assert bridge.cancelPairing() == {"cancelled": True}
    assert bridge.forgetSignIn(server, None, screen) == {"forgotten": False}
    # A page without the screen's key is refused before anything happens.
    for refused in (
        lambda: bridge.connect(server, None),
        lambda: bridge.startPairing(server, 443, "Work laptop", "guessed"),
        lambda: bridge.forgetSignIn(server, None, None),
    ):
        with pytest.raises(BridgeError, match="^connection_screen_only$"):
            refused()
    assert connection.calls == [
        ("prepare_connect", server, None, True),
        ("start_pairing", server, 443, "Work laptop"),
        ("prepare_connect", server, None, False),
        ("cancel_pairing",),
        ("forget_sign_in", server, None),
    ]


class ScriptedConfirm:
    """The native yes/no dialog: records each question and answers from a script."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.questions: list[tuple[str, str]] = []

    def __call__(self, title: str, message: str) -> bool:
        self.questions.append((title, message))
        return self.answers.pop(0)


def test_another_servers_page_asks_the_user_before_it_acts_on_the_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = ["https://other.example.com/chat"]
    confirm = ScriptedConfirm(False, True, True, False, True, False)
    permissions = _page_permissions(lambda: page[0], confirm)
    bridge, probed, controller = _server_bridge(
        tmp_path,
        monkeypatch,
        permissions=permissions,
        system_actions=DesktopSystemActions(
            clipboard_writer=lambda _text: None, clipboard_reader=lambda: "secret"
        ),
    )
    model = base64.b64encode(b"tflite-model").decode("ascii")

    # Selecting a server that is not remembered adds it: declined, nothing happens.
    with pytest.raises(BridgeError, match="^declined$"):
        bridge.selectServer("pi.lan", 9000)
    assert (probed, controller.list_servers()) == ([], [])
    # Allowed, it connects; a remembered server is selected without asking.
    assert bridge.selectServer("pi.lan", 9000)["status"] == "webui_available"
    bridge.selectServer("pi.lan", 9000)
    # The clipboard is asked for once per origin while the Desktop runs.
    assert bridge.getClipboardText() == "secret"
    assert bridge.getClipboardText() == "secret"
    # Removing a server and installing a wake word model are asked each time.
    with pytest.raises(BridgeError, match="^declined$"):
        bridge.removeServer("pi.lan", 9000)
    assert [entry.host for entry in controller.list_servers()] == ["pi.lan"]
    assert bridge.importWakewordModel("evil\u202e\nname.tflite", model) == {"from": "import_model"}
    # Connecting belongs to the connection screen alone; no question is asked.
    with pytest.raises(BridgeError, match="^connection_screen_only$"):
        bridge.connect("pi.lan", 9000)

    titles = [title for title, _ in confirm.questions]
    assert titles == [
        "Add a server",
        "Add a server",
        "Paste from the clipboard",
        "Remove a server",
        "Install a wake word model",
    ]
    # Each question names the asking page and what it asks for, in plain text.
    assert confirm.questions[0][1].startswith(
        "The page of https://other.example.com asks to add pi.lan:9000"
    )
    assert "model “evil name.tflite”" in confirm.questions[4][1]

    # A page that sends the window to the own server right after its call is still asked.
    page[0] = f"{OWN_SERVER}/"
    with pytest.raises(BridgeError, match="^declined$"):
        bridge.removeServer("pi.lan", 9000)
    assert confirm.questions[5][1].startswith("A page asks to remove pi.lan:9000")
    # The installation's own server's page, once loaded, is trusted like the Desktop.
    permissions.page_loaded()
    assert bridge.removeServer("pi.lan", 9000) == {"removed": True}
    assert len(confirm.questions) == 6
    assert probed == [("pi.lan", 9000), ("pi.lan", 9000)]


def test_without_a_way_to_ask_another_servers_page_is_refused() -> None:
    def failing_dialog(_title: str, _message: str) -> bool:
        raise RuntimeError("no window")

    asking, _ = _bridge(
        permissions=_page_permissions(lambda: "https://other.example.com", failing_dialog),
        system_actions=DesktopSystemActions(clipboard_reader=lambda: "secret"),
    )
    unasked = DesktopBridge(
        voice=cast(VoiceController, FakeVoice()),
        microphone=cast(MicrophoneService, FakeVoice()),
        system_actions=DesktopSystemActions(clipboard_reader=lambda: "secret"),
    )

    for bridge in (asking, unasked):
        with pytest.raises(BridgeError, match="^declined$"):
            bridge.getClipboardText()


@pytest.mark.parametrize("outcome", ["allow", "decline", "navigate"])
def test_computer_grants_need_native_consent_for_the_unchanged_selected_server(
    outcome: str,
) -> None:
    page = ["https://remote.example/"]
    applied: list[Any] = []
    questions: list[str] = []

    def confirm(_title: str, message: str) -> bool:
        questions.append(message)
        if outcome == "navigate":
            page[0] = OWN_SERVER
        return outcome != "decline"

    status = {
        "server_url": "https://remote.example/",
        "capabilities": {"terminal": False, "computer_use": False},
    }

    def update(changes: Any, *, expected_server: str, confirm: Any = None) -> dict[str, Any]:
        applied.append((changes, expected_server))
        return status

    sharing = SimpleNamespace(status=lambda: status, update=update)
    bridge, _ = _bridge(
        computer_sharing=sharing,
        permissions=_page_permissions(lambda: page[0], confirm),
    )
    assert bridge.getDesktopCapabilities()["computerSharing"] is True
    assert bridge.getComputerSharing() == status
    if outcome == "allow":
        bridge.setComputerSharing({"terminal": True})
        assert applied == [({"terminal": True}, "https://remote.example/")]
    else:
        with pytest.raises(BridgeError) as raised:
            bridge.setComputerSharing({"terminal": True})
        assert str(raised.value) == (
            "declined" if outcome == "decline" else "computer_server_changed"
        )
        assert applied == []
    assert len(questions) == 1


def test_servers_are_remembered_listed_with_the_active_one_and_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, probed, controller = _server_bridge(tmp_path, monkeypatch)
    assert bridge.getDesktopCapabilities()["signIn"] is True
    assert bridge.addServer("10.0.0.5", 8500, "") == {
        "host": "10.0.0.5",
        "port": 8500,
        "scheme": "http",
    }
    assert bridge.listServers() == [
        {"host": "10.0.0.5", "port": 8500, "scheme": "http", "active": False, "signed_in": False}
    ]

    bridge.connect("pi.lan", 9000, controller.screen_key)
    assert bridge.addServer("pi.lan", "9000", "Pi") == {
        "host": "pi.lan",
        "port": 9000,
        "scheme": "http",
        "label": "Pi",
    }
    # An HTTPS server is passed by its address; its port follows the address.
    assert bridge.addServer("https://vbot.example.com", None, "Cloud") == {
        "host": "https://vbot.example.com",
        "port": 443,
        "scheme": "https",
        "label": "Cloud",
    }

    assert bridge.listServers() == [
        {"host": "10.0.0.5", "port": 8500, "scheme": "http", "active": False, "signed_in": False},
        {
            "host": "pi.lan",
            "port": 9000,
            "scheme": "http",
            "label": "Pi",
            "active": True,
            "signed_in": False,
        },
        {
            "host": "https://vbot.example.com",
            "port": 443,
            "scheme": "https",
            "label": "Cloud",
            "active": False,
            "signed_in": False,
        },
    ]
    assert bridge.removeServer("10.0.0.5", "8500") == {"removed": True}
    assert bridge.removeServer("10.0.0.5", 8500) == {"removed": False}
    assert bridge.removeServer("https://vbot.example.com", None) == {"removed": True}
    assert [server["host"] for server in bridge.listServers()] == ["pi.lan"]
    assert probed == [("pi.lan", 9000)]


def test_optional_services_raise_without_their_owner() -> None:
    bridge, _ = _bridge()

    hotkey_calls: tuple[Callable[[], object], ...] = (
        bridge.getLiveHotkey,
        lambda: bridge.setLiveHotkey({"enabled": True}),
        bridge.getDictation,
        lambda: bridge.setDictation({"enabled": True}),
        bridge.getZoom,
        lambda: bridge.setZoom(125),
    )
    for call in hotkey_calls:
        with pytest.raises(RuntimeError, match="is not available in this Desktop"):
            call()
    server_calls: tuple[Callable[[], object], ...] = (
        lambda: bridge.connect("pi.lan", 9000),
        bridge.listServers,
        lambda: bridge.addServer("pi.lan", 9000),
        lambda: bridge.removeServer("pi.lan", 9000),
        lambda: bridge.selectServer("pi.lan", 9000),
        bridge.cancelPairing,
        lambda: bridge.forgetSignIn("pi.lan", 9000),
    )
    for call in server_calls:
        with pytest.raises(RuntimeError, match="Server selection is not available"):
            call()
