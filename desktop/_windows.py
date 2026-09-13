"""Windows DPI bootstrap and screen conversion for the Desktop shell."""

from __future__ import annotations

import importlib
import logging
import sys
from copy import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger("vbot.desktop")


def configure_dpi() -> None:
    """Select per-monitor rendering before pywebview queries screens or creates HWNDs.

    pywebview's later SetProcessDPIAware call only selects system DPI awareness.
    Establishing PerMonitorV2 first prevents Windows bitmap-scaling the WebView
    when the window crosses monitors. This applies to every Desktop entrypoint,
    including an ordinary Python host without a Desktop-specific manifest.
    """
    if sys.platform != "win32":
        return

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_awareness = user32.SetProcessDpiAwarenessContext
    set_awareness.argtypes = [ctypes.c_void_p]
    set_awareness.restype = wintypes.BOOL
    get_context = user32.GetThreadDpiAwarenessContext
    get_context.argtypes = []
    get_context.restype = ctypes.c_void_p
    equal_contexts = user32.AreDpiAwarenessContextsEqual
    equal_contexts.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    equal_contexts.restype = wintypes.BOOL
    per_monitor_v2 = ctypes.c_void_p(-4)
    if set_awareness(per_monitor_v2):
        return
    error = ctypes.get_last_error()
    # A manifest or a previous Desktop launch may already have selected V2.
    if not equal_contexts(get_context(), per_monitor_v2):
        logger.warning("Desktop per-monitor DPI awareness could not be enabled (error=%s)", error)


def configure_winforms() -> None:
    """Opt the Python-hosted .NET Framework into dynamic WinForms DPI handling.

    Python hosts have no TargetFrameworkAttribute. Without this opt-in WinForms
    ignores its PerMonitorV2 configuration even when Windows reports that mode.
    Set both values before pywebview loads WinForms through screen discovery.
    Modern .NET already uses the process DPI context and needs no Framework file.
    """
    if sys.platform != "win32":
        return
    importlib.import_module("clr")
    system = importlib.import_module("System")
    if system.Environment.Version.Major == 4:
        domain = system.AppDomain.CurrentDomain
        domain.SetData("APP_CONFIG_FILE", str(Path(__file__).with_name("windows.config")))
        domain.SetData("TargetFrameworkName", ".NETFramework,Version=v4.8")


def primary_scale() -> float:
    """Return the primary display scale after per-monitor awareness is enabled."""
    if sys.platform != "win32":
        return 1.0
    import ctypes
    from ctypes import wintypes

    get_dpi = ctypes.WinDLL("user32", use_last_error=True).GetDpiForSystem
    get_dpi.argtypes = []
    get_dpi.restype = wintypes.UINT
    return int(get_dpi()) / 96.0


def logical_primary_screen(screen: Any) -> Any:
    """Convert WinForms' physical screen/work-area pixels for pywebview's API.

    With PerMonitorV2 enabled, pywebview's Windows screen enumeration returns
    physical bounds and a misleading scale of 1. Its window constructor still
    expects logical dimensions. Only the origin-containing primary is used for
    startup; preserve the original snapshot and normalize a private copy.
    """
    scale = primary_scale()
    if scale == 1.0:
        return screen
    result = copy(screen)
    for name in ("x", "y", "width", "height"):
        setattr(result, name, int(getattr(screen, name) / scale))
    result.scale = scale
    frame = getattr(screen, "frame", None)
    if frame is not None:
        result.frame = SimpleNamespace(
            **{name: int(getattr(frame, name) / scale) for name in ("X", "Y", "Width", "Height")}
        )
    return result


def bind_window_dpi(window: Any, minimum_size: tuple[int, int]) -> None:
    """Keep the resize floor in logical pixels across WinForms DPI changes."""
    if sys.platform != "win32":
        return

    def dpi_changed(sender: Any, event: Any) -> None:
        scale = event.DeviceDpiNew / 96.0
        # .NET Framework otherwise retains the old physical minimum, which can
        # enlarge a small window during the move or prevent shrinking it again.
        minimum = type(sender.MinimumSize)(
            round(minimum_size[0] * scale), round(minimum_size[1] * scale)
        )
        sender.MinimumSize = minimum

        def finish_layout() -> None:
            # Framework autoscaling can rewrite MinimumSize after DpiChanged.
            if not sender.IsDisposed:
                sender.MinimumSize = minimum

        system = importlib.import_module("System")
        sender.BeginInvoke(system.Action(finish_layout))

    def before_show() -> None:
        # pywebview runs before_show synchronously on the native GUI thread.
        window.native.DpiChanged += dpi_changed

    window.events.before_show += before_show
