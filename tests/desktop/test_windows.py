"""Desktop DPI initialization and logical geometry regressions without a GUI."""

from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest

from desktop import _windows, main


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_do_not_load_windows_dependencies(monkeypatch, platform):
    monkeypatch.setattr(_windows.sys, "platform", platform)
    importer = Mock(side_effect=AssertionError("Windows dependency loaded"))
    monkeypatch.setattr(_windows.importlib, "import_module", importer)

    _windows.configure_dpi()
    _windows.configure_winforms()
    _windows.bind_window_dpi(object(), (800, 600))
    assert _windows.primary_scale() == 1.0
    importer.assert_not_called()


@pytest.mark.parametrize(
    ("accepted", "already_v2", "warns"),
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_windows_selects_v2_and_reports_conflicting_host_mode(
    monkeypatch, caplog, accepted, already_v2, warns
):
    monkeypatch.setattr(_windows.sys, "platform", "win32")
    user32 = SimpleNamespace(
        SetProcessDpiAwarenessContext=Mock(return_value=accepted),
        GetThreadDpiAwarenessContext=Mock(return_value=123),
        AreDpiAwarenessContextsEqual=Mock(return_value=already_v2),
    )
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=user32), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)

    _windows.configure_dpi()

    requested = user32.SetProcessDpiAwarenessContext.call_args.args[0]
    assert requested.value == ctypes.c_void_p(-4).value
    assert user32.SetProcessDpiAwarenessContext.argtypes == [ctypes.c_void_p]
    assert bool(caplog.records) is warns
    if not accepted:
        assert user32.AreDpiAwarenessContextsEqual.call_args.args[0] == 123


@pytest.mark.parametrize("runtime_major", [4, 8])
def test_framework_opt_in_is_complete_and_does_not_reconfigure_modern_dotnet(
    monkeypatch, runtime_major
):
    monkeypatch.setattr(_windows.sys, "platform", "win32")
    domain = Mock()
    system = SimpleNamespace(
        Environment=SimpleNamespace(Version=SimpleNamespace(Major=runtime_major)),
        AppDomain=SimpleNamespace(CurrentDomain=domain),
    )
    importer = Mock(side_effect=[object(), system])
    monkeypatch.setattr(_windows.importlib, "import_module", importer)

    _windows.configure_winforms()

    assert [call.args[0] for call in importer.call_args_list] == ["clr", "System"]
    if runtime_major == 4:
        values = dict(call.args for call in domain.SetData.call_args_list)
        assert values["TargetFrameworkName"] == ".NETFramework,Version=v4.8"
        config = Path(values["APP_CONFIG_FILE"])
        assert config.is_absolute()
        section = ElementTree.parse(config).find(
            "System.Windows.Forms.ApplicationConfigurationSection"
        )
        assert section is not None
        assert {item.attrib["key"]: item.attrib["value"] for item in section} == {
            "DpiAwareness": "PerMonitorV2"
        }
    else:
        domain.SetData.assert_not_called()


def test_dpi_and_framework_are_configured_before_screen_discovery(monkeypatch):
    events = []
    webview = object()
    monkeypatch.setattr(_windows, "configure_dpi", lambda: events.append("dpi"))
    monkeypatch.setattr(_windows, "configure_winforms", lambda: events.append("framework"))

    def import_webview(name):
        assert name == "webview"
        events.append("webview")
        return webview

    monkeypatch.setattr(main.importlib, "import_module", import_webview)
    assert main.load_webview() is webview
    assert events == ["dpi", "webview", "framework"]


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
def test_physical_primary_screen_is_converted_once_for_layout_and_placement(monkeypatch, scale):
    monkeypatch.setattr(_windows, "primary_scale", lambda: scale)
    physical = SimpleNamespace(
        x=0,
        y=0,
        width=2560,
        height=1440,
        scale=1.0,
        frame=SimpleNamespace(X=0, Y=0, Width=2560, Height=1380),
    )
    screen = main._primary_screen(SimpleNamespace(screens=[physical]))
    assert screen.width == int(2560 / scale)
    assert screen.height == int(1440 / scale)
    assert screen.scale == scale
    layout = main.resolve_window_layout((3000, 2000), screen)
    assert layout.width * scale <= 2560
    assert layout.height * scale <= 1380
    assert physical.width == 2560
    assert physical.frame.Height == 1380
    assert layout.screen is screen


def test_primary_scale_uses_windows_dpi_instead_of_pywebview_pixel_ratio(monkeypatch):
    monkeypatch.setattr(_windows.sys, "platform", "win32")
    user32 = SimpleNamespace(GetDpiForSystem=Mock(return_value=120))
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=user32), raising=False)
    assert _windows.primary_scale() == 1.25


def test_monitor_changes_preserve_logical_resize_floor_after_framework_layout(monkeypatch):
    monkeypatch.setattr(_windows.sys, "platform", "win32")
    monkeypatch.setattr(
        _windows.importlib,
        "import_module",
        lambda _: SimpleNamespace(Action=lambda callback: callback),
    )

    class Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, callback):
            self.handlers.append(callback)
            return self

    @dataclass
    class Size:
        Width: int
        Height: int

    pending = []
    native = SimpleNamespace(
        MinimumSize=Size(1000, 750),
        DpiChanged=Event(),
        BeginInvoke=pending.append,
        IsDisposed=False,
    )
    window = SimpleNamespace(events=SimpleNamespace(before_show=Event()))
    _windows.bind_window_dpi(window, (800, 600))
    # The native window does not exist until before_show.
    window.native = native
    window.events.before_show.handlers[0]()
    assert len(native.DpiChanged.handlers) == 1
    handler = native.DpiChanged.handlers[0]

    for dpi, expected in [(96, Size(800, 600)), (120, Size(1000, 750)), (96, Size(800, 600))]:
        handler(native, SimpleNamespace(DeviceDpiNew=dpi))
        assert native.MinimumSize == expected
        native.MinimumSize = Size(1000, 700)  # Framework rewrites it after the event.
        pending.pop()()
        assert native.MinimumSize == expected

    handler(native, SimpleNamespace(DeviceDpiNew=120))
    native.IsDisposed = True
    native.MinimumSize = Size(0, 0)
    pending.pop()()
    assert native.MinimumSize == Size(0, 0)


# -- Single instance -----------------------------------------------------------


class FakeInstanceApi:
    """In-process stand-in for named kernel objects shared by two launches."""

    def __init__(self, registry: dict[str, int] | None = None, *, fail_wait: bool = False):
        self.registry = registry if registry is not None else {}
        self.fail_wait = fail_wait
        self.signaled: set[int] = set()
        self.closed: list[int] = []
        self.events: list[str] = []
        self.handles = iter(range(100, 1000))

    def create_mutex(self, name: str) -> tuple[int, bool]:
        existed = name in self.registry
        self.registry.setdefault(name, next(self.handles))
        self.events.append(f"mutex:{name}")
        return next(self.handles), existed

    def create_event(self, name: str) -> int:
        handle = self.registry.setdefault(name, next(self.handles))
        self.events.append(f"event:{name}")
        return handle

    def signal(self, handle: int) -> bool:
        self.signaled.add(handle)
        self.events.append("signal")
        return True

    def wait(self, handle: int, timeout_ms: int) -> bool | None:
        if self.fail_wait:
            return None
        if handle in self.signaled:
            self.signaled.discard(handle)
            return True
        threading.Event().wait(0.01)
        return False

    def allow_foreground(self) -> None:
        self.events.append("allow_foreground")

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_other_platforms_have_no_instance_guard_or_permission_hook(monkeypatch, tmp_path):
    monkeypatch.setattr(_windows.sys, "platform", "linux")
    api = FakeInstanceApi()
    window = SimpleNamespace(events=SimpleNamespace(before_show=[]))

    instance = _windows.claim_desktop_instance(tmp_path, api=api)
    _windows.allow_server_microphone(window, lambda: None)

    assert instance is not None
    instance.listen(lambda: None)
    instance.close()
    assert api.events == []
    assert window.events.before_show == []


def test_instance_scope_is_stable_per_config_directory(tmp_path):
    first = _windows.instance_scope(tmp_path / "a")
    assert first == _windows.instance_scope(tmp_path / "a" / ".." / "a")
    assert first != _windows.instance_scope(tmp_path / "b")
    assert first.startswith("Local\\vBot.Desktop.")


def test_second_launch_signals_the_first_and_releases_its_handles(monkeypatch, tmp_path):
    monkeypatch.setattr(_windows.sys, "platform", "win32")
    registry: dict[str, int] = {}
    first_api = FakeInstanceApi(registry)
    second_api = FakeInstanceApi(registry)
    activated = threading.Event()

    first = _windows.claim_desktop_instance(tmp_path, api=first_api)
    assert first is not None
    first.listen(activated.set)
    second = _windows.claim_desktop_instance(tmp_path, api=second_api)
    # The shared event is one kernel object in both processes.
    first_api.signaled |= second_api.signaled

    assert second is None
    assert second_api.events[-2:] == ["allow_foreground", "signal"]
    assert len(second_api.closed) == 2
    assert activated.wait(timeout=2)
    first.close()
    assert len(first_api.closed) == 2
    first.close()
    assert len(first_api.closed) == 2


def test_activation_failures_do_not_stop_the_listener(monkeypatch, tmp_path):
    monkeypatch.setattr(_windows.sys, "platform", "win32")
    api = FakeInstanceApi()
    instance = _windows.claim_desktop_instance(tmp_path, api=api)
    assert instance is not None
    first_call = threading.Event()
    second_call = threading.Event()

    def on_activate() -> None:
        if not first_call.is_set():
            first_call.set()
            raise RuntimeError("window gone")
        second_call.set()

    instance.listen(on_activate)
    event = next(handle for name, handle in api.registry.items() if name.endswith(".activate"))
    api.signaled.add(event)
    assert first_call.wait(timeout=2)
    api.signaled.add(event)

    assert second_call.wait(timeout=2)
    instance.close()


def test_failed_wait_ends_the_listener(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(_windows.sys, "platform", "win32")
    api = FakeInstanceApi(fail_wait=True)
    instance = _windows.claim_desktop_instance(tmp_path, api=api)
    assert instance is not None

    instance.listen(lambda: None)
    instance.close()

    assert any("waiting failed" in record.getMessage() for record in caplog.records)


def test_guard_creation_failure_keeps_the_launch_unguarded(monkeypatch, tmp_path):
    monkeypatch.setattr(_windows.sys, "platform", "win32")
    api = Mock()
    api.create_mutex.side_effect = OSError("denied")

    instance = _windows.claim_desktop_instance(tmp_path, api=api)

    assert isinstance(instance, _windows.DesktopInstance)
    instance.listen(lambda: None)
    instance.close()


# -- WebView2 browser arguments ------------------------------------------------


def test_trusted_origins_skip_loopback_and_are_sorted_and_unique():
    origins = _windows.trusted_http_origins(
        [
            ("192.168.1.20", 8420),
            ("localhost", 8420),
            ("127.0.0.1", 9000),
            ("127.5.0.1", 9000),
            ("vbot.localhost", 8420),
            ("::1", 8420),
            ("NAS.lan", 8420),
            ("nas.lan", 8420),
            ("fe80::1", 8420),
            ("web.lan", 80),
            ("bad host", 8420),
            ("evil<script>", 8420),
        ]
    )

    assert origins == (
        "http://192.168.1.20:8420",
        "http://[fe80::1]:8420",
        "http://nas.lan:8420",
        "http://web.lan",
    )


def test_browser_arguments_add_fixed_switches_and_origins():
    value = _windows.build_browser_arguments(None, ["http://b.lan:8420", "http://a.lan:8420"])

    assert value == (
        "--autoplay-policy=no-user-gesture-required "
        "--unsafely-treat-insecure-origin-as-secure=http://a.lan:8420,http://b.lan:8420 "
        "--disable-features=ElasticOverscroll"
    )


def test_browser_arguments_merge_a_pre_existing_value():
    existing = (
        "--remote-debugging-port=9222 "
        "--unsafely-treat-insecure-origin-as-secure=http://c.lan:1,http://a.lan:8420 "
        "--disable-features=Translate --autoplay-policy=user-gesture-required"
    )

    value = _windows.build_browser_arguments(existing, ["http://a.lan:8420", "http://b.lan:2"])

    assert value.split() == [
        "--remote-debugging-port=9222",
        "--autoplay-policy=user-gesture-required",
        "--unsafely-treat-insecure-origin-as-secure=http://a.lan:8420,http://b.lan:2,http://c.lan:1",
        "--disable-features=Translate,ElasticOverscroll",
    ]


def test_browser_arguments_without_origins_have_no_origin_switch():
    value = _windows.build_browser_arguments("", [])

    assert "--unsafely-treat-insecure-origin-as-secure" not in value
    assert "--autoplay-policy=no-user-gesture-required" in value


def test_apply_browser_arguments_exports_only_on_windows(monkeypatch):
    monkeypatch.setenv(_windows.WEBVIEW2_BROWSER_ARGUMENTS_ENV, "--lang=de")
    monkeypatch.setattr(_windows.sys, "platform", "linux")
    _windows.apply_browser_arguments(["http://a.lan:8420"])
    assert _windows.os.environ[_windows.WEBVIEW2_BROWSER_ARGUMENTS_ENV] == "--lang=de"
    assert _windows.webview_secure_origins([("a.lan", 8420)]) == ()

    monkeypatch.setattr(_windows.sys, "platform", "win32")
    _windows.apply_browser_arguments(["http://a.lan:8420"])
    value = _windows.os.environ[_windows.WEBVIEW2_BROWSER_ARGUMENTS_ENV]
    assert value.startswith("--lang=de --autoplay-policy=no-user-gesture-required")
    assert "--unsafely-treat-insecure-origin-as-secure=http://a.lan:8420" in value
    assert _windows.webview_secure_origins([("a.lan", 8420)]) == ("http://a.lan:8420",)


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://192.168.1.20:8420/?accessor=desktop", "http://192.168.1.20:8420"),
        ("http://NAS.lan:80/app", "http://nas.lan"),
        ("https://vbot.example:443/", "https://vbot.example"),
        ("http://[fe80::1]:8420/", "http://[fe80::1]:8420"),
        ("about:blank", None),
        ("data:text/html,hi", None),
        ("http://bad:port/", None),
        ("", None),
        (None, None),
    ],
)
def test_url_origin_serializes_like_browsers(url, origin):
    assert _windows.url_origin(url) == origin


# -- WebView2 microphone permission --------------------------------------------


class _DotnetEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self


def _permission_window(monkeypatch):
    monkeypatch.setattr(_windows.sys, "platform", "win32")
    core = SimpleNamespace(
        CoreWebView2PermissionKind=SimpleNamespace(Microphone="mic", Camera="camera"),
        CoreWebView2PermissionState=SimpleNamespace(Default="default", Allow="allow"),
    )
    monkeypatch.setattr(_windows.importlib, "import_module", lambda name: core)
    control = SimpleNamespace(CoreWebView2InitializationCompleted=_DotnetEvent())
    window = SimpleNamespace(events=SimpleNamespace(before_show=_DotnetEvent()))
    return window, control


def _attach_permission_hook(window, control, allowed):
    _windows.allow_server_microphone(window, allowed)
    window.native = SimpleNamespace(browser=SimpleNamespace(webview=control))
    window.events.before_show.handlers[0]()
    sender = SimpleNamespace(CoreWebView2=SimpleNamespace(PermissionRequested=_DotnetEvent()))
    control.CoreWebView2InitializationCompleted.handlers[0](sender, SimpleNamespace(IsSuccess=True))
    return sender.CoreWebView2.PermissionRequested.handlers[0]


def _permission_args(kind="mic", uri="http://192.168.1.20:8420/?accessor=desktop"):
    return SimpleNamespace(PermissionKind=kind, Uri=uri, State="default", SavesInProfile=True)


def test_connected_server_microphone_is_allowed_for_this_request_only(monkeypatch):
    window, control = _permission_window(monkeypatch)
    origin = ["http://192.168.1.20:8420"]
    handler = _attach_permission_hook(window, control, lambda: origin[0])

    allowed = _permission_args()
    handler(None, allowed)
    assert (allowed.State, allowed.SavesInProfile) == ("allow", False)

    for args in (
        _permission_args(kind="camera"),
        _permission_args(uri="http://192.168.1.21:8420/"),
        _permission_args(uri="about:blank"),
    ):
        handler(None, args)
        assert (args.State, args.SavesInProfile) == ("default", True)

    origin[0] = None
    other = _permission_args()
    handler(None, other)
    assert other.State == "default"


def test_permission_hook_tolerates_older_sdks_and_logs_failures_once(monkeypatch, caplog):
    window, control = _permission_window(monkeypatch)
    handler = _attach_permission_hook(window, control, lambda: "http://192.168.1.20:8420")

    class OldArgs:
        PermissionKind = "mic"
        Uri = "http://192.168.1.20:8420/"
        State = "default"

        def __setattr__(self, name, value):
            if name == "SavesInProfile":
                raise AttributeError(name)
            object.__setattr__(self, name, value)

    old = OldArgs()
    handler(None, old)
    assert old.State == "allow"

    broken = SimpleNamespace(PermissionKind="mic")  # no Uri
    handler(None, broken)
    handler(None, broken)
    assert sum("permission hook failed" in record.getMessage() for record in caplog.records) == 1


def test_permission_hook_attach_failure_never_breaks_startup(monkeypatch, caplog):
    window, _control = _permission_window(monkeypatch)
    _windows.allow_server_microphone(window, lambda: None)
    window.native = SimpleNamespace()  # no browser control

    window.events.before_show.handlers[0]()

    assert any("permission hook failed (attach)" in r.getMessage() for r in caplog.records)
