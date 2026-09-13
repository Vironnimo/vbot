"""Desktop DPI initialization and logical geometry regressions without a GUI."""

from __future__ import annotations

import ctypes
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
