"""Win32 bindings of the Windows desktop target: DLL prototypes, displays, capture, windows.

Importable on every platform: structures are plain ctypes, and the DLLs are
bound only by :func:`api` on Windows. Wrappers return plain Python values, so
callers never handle ctypes objects. Coordinates are physical only on a thread
that called :func:`enter_thread` (per-monitor v2 DPI awareness).
"""

from __future__ import annotations

import ctypes as ct
import functools
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any

from .target import TargetError


class Rect(ct.Structure):
    _fields_ = [(name, ct.c_int32) for name in ("left", "top", "right", "bottom")]


class Point(ct.Structure):
    _fields_ = [("x", ct.c_int32), ("y", ct.c_int32)]


class _MonitorInfo(ct.Structure):
    _fields_ = [
        ("size", ct.c_uint32),
        ("monitor", Rect),
        ("work", Rect),
        ("flags", ct.c_uint32),
        ("device", ct.c_wchar * 32),
    ]


class _BitmapInfoHeader(ct.Structure):
    _fields_ = [
        ("size", ct.c_uint32),
        ("width", ct.c_int32),
        ("height", ct.c_int32),
        ("planes", ct.c_uint16),
        ("bit_count", ct.c_uint16),
        ("compression", ct.c_uint32),
        ("size_image", ct.c_uint32),
        ("x_ppm", ct.c_int32),
        ("y_ppm", ct.c_int32),
        ("used", ct.c_uint32),
        ("important", ct.c_uint32),
    ]


class _BlendFunction(ct.Structure):
    _fields_ = [(name, ct.c_ubyte) for name in ("op", "flags", "alpha", "format")]


class _Luid(ct.Structure):
    _fields_ = [("low", ct.c_uint32), ("high", ct.c_int32)]


class _PathEnd(ct.Structure):
    """The adapter and id that start a DISPLAYCONFIG source or target info."""

    _fields_ = [("adapter", _Luid), ("id", ct.c_uint32)]


class _PathInfo(ct.Structure):
    # DISPLAYCONFIG_PATH_INFO: 20-byte source info, 48-byte target info, flags.
    _fields_ = [
        ("source", _PathEnd),
        ("_source_rest", ct.c_uint32 * 2),
        ("target", _PathEnd),
        ("_target_rest", ct.c_uint32 * 9),
        ("flags", ct.c_uint32),
    ]


class _ModeInfo(ct.Structure):
    # DISPLAYCONFIG_MODE_INFO: its 48-byte union holds 64-bit fields.
    _fields_ = [
        ("type", ct.c_uint32),
        ("id", ct.c_uint32),
        ("adapter", _Luid),
        ("data", ct.c_uint64 * 6),
    ]


class _DeviceInfoHeader(ct.Structure):
    _fields_ = [
        ("type", ct.c_int32),
        ("size", ct.c_uint32),
        ("adapter", _Luid),
        ("id", ct.c_uint32),
    ]


class _SourceName(ct.Structure):
    _fields_ = [("header", _DeviceInfoHeader), ("gdi_name", ct.c_wchar * 32)]


class _TargetName(ct.Structure):
    _fields_ = [
        ("header", _DeviceInfoHeader),
        ("flags", ct.c_uint32),
        ("technology", ct.c_uint32),
        ("manufacturer", ct.c_uint16),
        ("product", ct.c_uint16),
        ("connector", ct.c_uint32),
        ("friendly_name", ct.c_wchar * 64),
        ("device_path", ct.c_wchar * 128),
    ]


SW_RESTORE = 9
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x20, 0x80, 0x40000
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
_PER_MONITOR_V2 = -4
_DWMWA_EXTENDED_FRAME_BOUNDS, _DWMWA_CLOAKED = 9, 14
_GWL_STYLE, _GWL_EXSTYLE = -16, -20
_GW_OWNER, _GA_ROOT = 4, 2
_LWA_ALPHA = 0x2
_ULW_ALPHA, _AC_SRC_ALPHA = 0x2, 0x1
_SRCCOPY_CAPTUREBLT = 0x00CC0020 | 0x40000000
_QDC_ONLY_ACTIVE_PATHS = 2
_DPI = threading.local()


@dataclass(frozen=True)
class Monitor:
    device: str
    left: int
    top: int
    right: int
    bottom: int
    primary: bool
    dpi: int


class _Api:
    """Bound DLLs and callback prototypes; created once by :func:`api`."""

    user32: Any
    gdi32: Any
    kernel32: Any
    advapi32: Any
    dwmapi: Any
    shcore: Any
    version: Any
    enum_proc: Any
    monitor_proc: Any
    window_proc: Any


_HANDLE, _INT, _UINT32 = ct.c_void_p, ct.c_int, ct.c_uint32
_P_UINT32 = ct.POINTER(ct.c_uint32)
_SIGNATURES: dict[str, dict[str, tuple[list[Any], Any]]] = {
    "user32": {
        "SetThreadDpiAwarenessContext": ([_HANDLE], _HANDLE),
        "EnumDisplayMonitors": ([_HANDLE, _HANDLE, _HANDLE, ct.c_ssize_t], _INT),
        "GetMonitorInfoW": ([_HANDLE, ct.POINTER(_MonitorInfo)], _INT),
        "GetSystemMetrics": ([_INT], _INT),
        "GetDC": ([_HANDLE], _HANDLE),
        "ReleaseDC": ([_HANDLE, _HANDLE], _INT),
        "EnumWindows": ([_HANDLE, ct.c_ssize_t], _INT),
        "EnumChildWindows": ([_HANDLE, _HANDLE, ct.c_ssize_t], _INT),
        "GetClassNameW": ([_HANDLE, ct.c_wchar_p, _INT], _INT),
        "GetWindowTextLengthW": ([_HANDLE], _INT),
        "GetWindowTextW": ([_HANDLE, ct.c_wchar_p, _INT], _INT),
        "GetWindowThreadProcessId": ([_HANDLE, _P_UINT32], _UINT32),
        "IsWindowVisible": ([_HANDLE], _INT),
        "IsWindowEnabled": ([_HANDLE], _INT),
        "IsIconic": ([_HANDLE], _INT),
        "IsHungAppWindow": ([_HANDLE], _INT),
        "GetWindowRect": ([_HANDLE, ct.POINTER(Rect)], _INT),
        "GetWindowLongPtrW": ([_HANDLE, _INT], ct.c_ssize_t),
        "GetLayeredWindowAttributes": (
            [_HANDLE, _HANDLE, ct.POINTER(ct.c_ubyte), _P_UINT32],
            _INT,
        ),
        "GetWindow": ([_HANDLE, ct.c_uint], _HANDLE),
        "GetAncestor": ([_HANDLE, ct.c_uint], _HANDLE),
        "GetLastActivePopup": ([_HANDLE], _HANDLE),
        "GetForegroundWindow": ([], _HANDLE),
        "WindowFromPoint": ([Point], _HANDLE),
        "SendInput": ([ct.c_uint, _HANDLE, _INT], ct.c_uint),
        "GetKeyboardLayout": ([_UINT32], _HANDLE),
        "VkKeyScanExW": ([ct.c_wchar, _HANDLE], ct.c_short),
        "MapVirtualKeyExW": ([ct.c_uint, ct.c_uint, _HANDLE], ct.c_uint),
        "ToUnicodeEx": (
            [ct.c_uint, ct.c_uint, _HANDLE, ct.c_wchar_p, _INT, ct.c_uint, _HANDLE],
            _INT,
        ),
        "GetCursorPos": ([ct.POINTER(Point)], _INT),
        "SetCursorPos": ([_INT, _INT], _INT),
        "GetDoubleClickTime": ([], ct.c_uint),
        "ShowWindow": ([_HANDLE, _INT], _INT),
        "SetForegroundWindow": ([_HANDLE], _INT),
        "BringWindowToTop": ([_HANDLE], _INT),
        "AttachThreadInput": ([_UINT32, _UINT32, _INT], _INT),
        "AllowSetForegroundWindow": ([_UINT32], _INT),
        "GetProcessWindowStation": ([], _HANDLE),
        "GetUserObjectInformationW": ([_HANDLE, _INT, _HANDLE, _UINT32, _P_UINT32], _INT),
        "OpenInputDesktop": ([_UINT32, _INT, _UINT32], _HANDLE),
        "CloseDesktop": ([_HANDLE], _INT),
        "GetDisplayConfigBufferSizes": ([_UINT32, _P_UINT32, _P_UINT32], ct.c_long),
        "QueryDisplayConfig": (
            [_UINT32, _P_UINT32, _HANDLE, _P_UINT32, _HANDLE, _HANDLE],
            ct.c_long,
        ),
        "DisplayConfigGetDeviceInfo": ([_HANDLE], ct.c_long),
        "RegisterClassExW": ([_HANDLE], ct.c_uint16),
        "CreateWindowExW": (
            [_UINT32, ct.c_wchar_p, ct.c_wchar_p, _UINT32, *[_INT] * 4, *[_HANDLE] * 4],
            _HANDLE,
        ),
        "DestroyWindow": ([_HANDLE], _INT),
        "DefWindowProcW": ([_HANDLE, ct.c_uint, ct.c_size_t, ct.c_ssize_t], ct.c_ssize_t),
        "GetMessageW": ([_HANDLE, _HANDLE, ct.c_uint, ct.c_uint], _INT),
        "PeekMessageW": ([_HANDLE, _HANDLE, ct.c_uint, ct.c_uint, ct.c_uint], _INT),
        "DispatchMessageW": ([_HANDLE], ct.c_ssize_t),
        "PostThreadMessageW": ([_UINT32, ct.c_uint, ct.c_size_t, ct.c_ssize_t], _INT),
        "UpdateLayeredWindow": (
            [_HANDLE, _HANDLE, _HANDLE, _HANDLE, _HANDLE, _HANDLE, _UINT32, _HANDLE, _UINT32],
            _INT,
        ),
        "SetWindowDisplayAffinity": ([_HANDLE, _UINT32], _INT),
        "SetWindowPos": ([_HANDLE, _HANDLE, _INT, _INT, _INT, _INT, ct.c_uint], _INT),
    },
    "gdi32": {
        "CreateCompatibleDC": ([_HANDLE], _HANDLE),
        "CreateCompatibleBitmap": ([_HANDLE, _INT, _INT], _HANDLE),
        "SelectObject": ([_HANDLE, _HANDLE], _HANDLE),
        "BitBlt": ([_HANDLE, _INT, _INT, _INT, _INT, _HANDLE, _INT, _INT, _UINT32], _INT),
        "GetDIBits": ([_HANDLE, _HANDLE, ct.c_uint, ct.c_uint, _HANDLE, _HANDLE, ct.c_uint], _INT),
        "CreateDIBSection": (
            [_HANDLE, _HANDLE, ct.c_uint, ct.POINTER(ct.c_void_p), _HANDLE, _UINT32],
            _HANDLE,
        ),
        "DeleteObject": ([_HANDLE], _INT),
        "DeleteDC": ([_HANDLE], _INT),
    },
    "kernel32": {
        "OpenProcess": ([_UINT32, _INT, _UINT32], _HANDLE),
        "CloseHandle": ([_HANDLE], _INT),
        "QueryFullProcessImageNameW": ([_HANDLE, _UINT32, ct.c_wchar_p, _P_UINT32], _INT),
        "GetPackageFamilyName": ([_HANDLE, _P_UINT32, ct.c_wchar_p], ct.c_long),
        "GetPackagesByPackageFamily": (
            [ct.c_wchar_p, _P_UINT32, _HANDLE, _P_UINT32, _HANDLE],
            ct.c_long,
        ),
        "GetPackagePathByFullName": ([ct.c_wchar_p, _P_UINT32, ct.c_wchar_p], ct.c_long),
        "GetCurrentProcess": ([], _HANDLE),
        "GetCurrentThreadId": ([], _UINT32),
        "ProcessIdToSessionId": ([_UINT32, _P_UINT32], _INT),
        "GetModuleHandleW": ([ct.c_wchar_p], _HANDLE),
    },
    "advapi32": {
        "OpenProcessToken": ([_HANDLE, _UINT32, ct.POINTER(ct.c_void_p)], _INT),
        "GetTokenInformation": ([_HANDLE, _INT, _HANDLE, _UINT32, _P_UINT32], _INT),
        "GetSidSubAuthorityCount": ([_HANDLE], ct.POINTER(ct.c_ubyte)),
        "GetSidSubAuthority": ([_HANDLE, _UINT32], _P_UINT32),
    },
    "dwmapi": {"DwmGetWindowAttribute": ([_HANDLE, _UINT32, _HANDLE, _UINT32], ct.c_long)},
    "shcore": {
        "GetDpiForMonitor": (
            [_HANDLE, _INT, ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint)],
            ct.c_long,
        ),
    },
    "version": {
        "GetFileVersionInfoSizeW": ([ct.c_wchar_p, _HANDLE], _UINT32),
        "GetFileVersionInfoW": ([ct.c_wchar_p, _UINT32, _UINT32, _HANDLE], _INT),
        "VerQueryValueW": (
            [_HANDLE, ct.c_wchar_p, ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_uint)],
            _INT,
        ),
    },
}


@functools.cache
def api() -> _Api:
    """Bind the Win32 functions once; refuses on other platforms."""
    if sys.platform != "win32":
        raise TargetError("Computer Use needs a Windows desktop.", "computer_use_unavailable")
    bound = _Api()
    for library, functions in _SIGNATURES.items():
        # Private instances keep these prototypes apart from other ctypes users.
        dll = ct.WinDLL(library, use_last_error=True)
        for name, (arguments, result) in functions.items():
            function = getattr(dll, name)
            function.argtypes, function.restype = arguments, result
        setattr(bound, library, dll)
    bound.enum_proc = ct.WINFUNCTYPE(ct.c_int, ct.c_void_p, ct.c_ssize_t)
    bound.monitor_proc = ct.WINFUNCTYPE(
        ct.c_int, ct.c_void_p, ct.c_void_p, ct.POINTER(Rect), ct.c_ssize_t
    )
    bound.window_proc = ct.WINFUNCTYPE(
        ct.c_ssize_t, ct.c_void_p, ct.c_uint, ct.c_size_t, ct.c_ssize_t
    )
    return bound


def last_error() -> int:
    """The Win32 error of this thread's latest call through :func:`api`."""
    if sys.platform != "win32":
        return 0
    return ct.get_last_error()


def windows_directory() -> str:
    return os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or "C:\\Windows"


# Desktop, displays and capture


def enter_thread() -> None:
    """Make the calling thread per-monitor (v2) DPI aware, once, so pixels are physical."""
    if getattr(_DPI, "entered", False):
        return
    if not api().user32.SetThreadDpiAwarenessContext(ct.c_void_p(_PER_MONITOR_V2)):
        raise TargetError(
            "Windows could not provide physical screen coordinates (Windows 10 version 1703 "
            "or later is required).",
            "computer_use_unavailable",
        )
    _DPI.entered = True


def interactive_window_station() -> bool:
    """Whether this process's window station shows on screen (not a service session)."""
    bound = api()
    session = ct.c_uint32()
    if bound.kernel32.ProcessIdToSessionId(os.getpid(), ct.byref(session)) and not session.value:
        return False
    flags = (ct.c_uint32 * 3)()  # USEROBJECTFLAGS: fInherit, fReserved, dwFlags
    station = bound.user32.GetProcessWindowStation()
    if not station or not bound.user32.GetUserObjectInformationW(
        station, 1, flags, ct.sizeof(flags), None
    ):
        return False
    return bool(flags[2] & 1)  # WSF_VISIBLE


def input_desktop_available() -> bool:
    """False while the lock screen or a secure prompt (UAC) owns the input desktop."""
    user32 = api().user32
    desktop = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not desktop:
        return False
    try:
        name = ct.create_unicode_buffer(64)
        if not user32.GetUserObjectInformationW(desktop, 2, name, ct.sizeof(name), None):
            return False
        return name.value.lower() == "default"
    finally:
        user32.CloseDesktop(desktop)


def monitors() -> list[Monitor]:
    bound = api()
    found: list[Monitor] = []

    def visit(handle: int, _dc: int, _rect: Any, _data: int) -> int:
        info = _MonitorInfo(size=ct.sizeof(_MonitorInfo))
        if bound.user32.GetMonitorInfoW(handle, ct.byref(info)):
            dpi, unused = ct.c_uint(), ct.c_uint()
            if bound.shcore.GetDpiForMonitor(handle, 0, ct.byref(dpi), ct.byref(unused)) != 0:
                dpi.value = 96
            box = info.monitor
            found.append(
                Monitor(
                    device=str(info.device),
                    left=int(box.left),
                    top=int(box.top),
                    right=int(box.right),
                    bottom=int(box.bottom),
                    primary=bool(info.flags & 1),
                    dpi=int(dpi.value),
                )
            )
        return 1

    bound.user32.EnumDisplayMonitors(None, None, bound.monitor_proc(visit), 0)
    return found


def monitor_names() -> dict[str, str]:
    """Monitor model names, as Windows Settings shows them, by GDI device name."""
    user32 = api().user32
    path_count, mode_count = ct.c_uint32(), ct.c_uint32()
    if user32.GetDisplayConfigBufferSizes(
        _QDC_ONLY_ACTIVE_PATHS, ct.byref(path_count), ct.byref(mode_count)
    ):
        return {}
    paths = (_PathInfo * path_count.value)()
    modes = (_ModeInfo * mode_count.value)()
    if user32.QueryDisplayConfig(
        _QDC_ONLY_ACTIVE_PATHS, ct.byref(path_count), paths, ct.byref(mode_count), modes, None
    ):
        return {}
    names: dict[str, str] = {}
    for path in paths[: path_count.value]:
        source = _SourceName()
        source.header = _DeviceInfoHeader(1, ct.sizeof(_SourceName), path.source.adapter)
        source.header.id = path.source.id
        target = _TargetName()
        target.header = _DeviceInfoHeader(2, ct.sizeof(_TargetName), path.target.adapter)
        target.header.id = path.target.id
        if user32.DisplayConfigGetDeviceInfo(ct.byref(source)) or (
            user32.DisplayConfigGetDeviceInfo(ct.byref(target))
        ):
            continue
        name = str(target.friendly_name).strip()
        if name:
            names.setdefault(str(source.gdi_name), name)
    return names


def virtual_screen() -> tuple[int, int, int, int]:
    """Left, top, width and height of the whole virtual desktop."""
    metrics = api().user32.GetSystemMetrics
    return int(metrics(76)), int(metrics(77)), int(metrics(78)), int(metrics(79))


def buttons_swapped() -> bool:
    return bool(api().user32.GetSystemMetrics(23))  # SM_SWAPBUTTON


def capture_bgrx(left: int, top: int, width: int, height: int) -> bytes:
    """Screen pixels of a rectangle as top-down 32-bit BGRX rows."""
    bound = api()
    user32, gdi32 = bound.user32, bound.gdi32
    screen = user32.GetDC(None)
    if not screen:
        raise TargetError("Windows refused access to the screen.")
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    try:
        if not memory or not bitmap:
            raise TargetError("Windows could not allocate the screenshot.")
        previous = gdi32.SelectObject(memory, bitmap)
        # CAPTUREBLT includes layered windows such as menus and tooltips.
        copied = gdi32.BitBlt(memory, 0, 0, width, height, screen, left, top, _SRCCOPY_CAPTUREBLT)
        gdi32.SelectObject(memory, previous)  # GetDIBits needs the bitmap deselected
        if not copied:
            raise TargetError(
                "Windows refused the screenshot; the desktop may be locked.",
                "computer_use_unavailable",
            )
        header = _BitmapInfoHeader(ct.sizeof(_BitmapInfoHeader), width, -height, 1, 32)
        info = ct.create_string_buffer(ct.sizeof(_BitmapInfoHeader) + 16)
        ct.memmove(info, ct.byref(header), ct.sizeof(header))
        pixels = ct.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(memory, bitmap, 0, height, pixels, info, 0) != height:
            raise TargetError("Windows could not read the screenshot pixels.")
        return pixels.raw
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)


def paint_layered(
    handle: int, left: int, top: int, width: int, height: int, bgra: bytes, alpha: int
) -> bool:
    """Place a layered window and give it top-down premultiplied BGRA pixels and an opacity."""
    bound = api()
    user32, gdi32 = bound.user32, bound.gdi32
    screen = user32.GetDC(None)
    if not screen:
        return False
    memory = gdi32.CreateCompatibleDC(screen)
    header = _BitmapInfoHeader(ct.sizeof(_BitmapInfoHeader), width, -height, 1, 32)
    bits = ct.c_void_p()
    bitmap = gdi32.CreateDIBSection(memory, ct.byref(header), 0, ct.byref(bits), None, 0)
    try:
        if not memory or not bitmap or not bits.value:
            return False
        ct.memmove(bits.value, bgra, width * height * 4)
        previous = gdi32.SelectObject(memory, bitmap)
        try:
            return bool(
                user32.UpdateLayeredWindow(
                    handle,
                    screen,
                    ct.byref(Point(left, top)),
                    ct.byref(Point(width, height)),  # SIZE shares POINT's layout
                    memory,
                    ct.byref(Point(0, 0)),
                    0,
                    ct.byref(_BlendFunction(0, 0, alpha, _AC_SRC_ALPHA)),
                    _ULW_ALPHA,
                )
            )
        finally:
            gdi32.SelectObject(memory, previous)
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)


def set_layered_opacity(handle: int, alpha: int) -> bool:
    """Change only the opacity of a layered window that :func:`paint_layered` painted."""
    blend = _BlendFunction(0, 0, alpha, _AC_SRC_ALPHA)
    return bool(
        api().user32.UpdateLayeredWindow(
            handle, None, None, None, None, None, 0, ct.byref(blend), _ULW_ALPHA
        )
    )


# Windows


def top_level_windows() -> list[int]:
    """Top-level windows in z-order, topmost first."""
    bound = api()
    handles: list[int] = []

    def visit(handle: int | None, _data: int) -> int:
        handles.append(int(handle or 0))
        return 1

    bound.user32.EnumWindows(bound.enum_proc(visit), 0)
    return handles


def child_windows(handle: int) -> list[int]:
    bound = api()
    handles: list[int] = []

    def visit(child: int | None, _data: int) -> int:
        handles.append(int(child or 0))
        return 1

    bound.user32.EnumChildWindows(handle, bound.enum_proc(visit), 0)
    return handles


def window_class(handle: int) -> str:
    buffer = ct.create_unicode_buffer(256)
    api().user32.GetClassNameW(handle, buffer, len(buffer))
    return buffer.value


def window_title(handle: int) -> str:
    user32 = api().user32
    length = int(user32.GetWindowTextLengthW(handle))
    if length <= 0:
        return ""
    buffer = ct.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, len(buffer))
    return buffer.value


def window_process(handle: int) -> tuple[int, int]:
    """Process and thread id of a window's creator."""
    pid = ct.c_uint32()
    thread = int(api().user32.GetWindowThreadProcessId(handle, ct.byref(pid)))
    return int(pid.value), thread


def is_visible(handle: int) -> bool:
    return bool(api().user32.IsWindowVisible(handle))


def is_minimized(handle: int) -> bool:
    return bool(api().user32.IsIconic(handle))


def is_enabled(handle: int) -> bool:
    return bool(api().user32.IsWindowEnabled(handle))


def is_cloaked(handle: int) -> bool:
    """Hidden by DWM: another virtual desktop, a suspended app, a hidden shell surface."""
    value = ct.c_uint32()
    result = api().dwmapi.DwmGetWindowAttribute(
        handle, _DWMWA_CLOAKED, ct.byref(value), ct.sizeof(value)
    )
    return result == 0 and value.value != 0


def window_bounds(handle: int) -> tuple[int, int, int, int] | None:
    """Visible frame bounds (without the invisible resize border), physical pixels."""
    bound, rect = api(), Rect()
    if bound.dwmapi.DwmGetWindowAttribute(
        handle, _DWMWA_EXTENDED_FRAME_BOUNDS, ct.byref(rect), ct.sizeof(rect)
    ) and not bound.user32.GetWindowRect(handle, ct.byref(rect)):
        return None
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def extended_style(handle: int) -> int:
    return int(api().user32.GetWindowLongPtrW(handle, _GWL_EXSTYLE)) & 0xFFFFFFFF


def layered_alpha(handle: int) -> int | None:
    """Constant alpha of a layered window, or ``None`` when it sets none."""
    alpha, flags = ct.c_ubyte(), ct.c_uint32()
    if not api().user32.GetLayeredWindowAttributes(handle, None, ct.byref(alpha), ct.byref(flags)):
        return None
    return int(alpha.value) if flags.value & _LWA_ALPHA else None


def window_owner(handle: int) -> int:
    return int(api().user32.GetWindow(handle, _GW_OWNER) or 0)


def last_active_popup(handle: int) -> int:
    return int(api().user32.GetLastActivePopup(handle) or 0)


def foreground_window() -> int:
    return int(api().user32.GetForegroundWindow() or 0)


def window_at(x: int, y: int) -> int:
    """The top-level window that receives a click at a point, or 0."""
    user32 = api().user32
    child = user32.WindowFromPoint(Point(x, y))
    return int(user32.GetAncestor(child, _GA_ROOT) or 0) if child else 0


# Activation


def is_hung(handle: int) -> bool:
    return bool(api().user32.IsHungAppWindow(handle))


def show_window(handle: int, command: int) -> None:
    api().user32.ShowWindow(handle, command)


def set_foreground(handle: int) -> bool:
    api().user32.SetForegroundWindow(handle)
    return foreground_window() == handle


def attach_and_activate(handle: int) -> bool:
    """Activate *handle* while sharing the current foreground thread's input state."""
    user32, kernel32 = api().user32, api().kernel32
    foreground = foreground_window()
    if not foreground or is_hung(foreground):
        return False
    own, other = int(kernel32.GetCurrentThreadId()), window_process(foreground)[1]
    if not other or not user32.AttachThreadInput(own, other, True):
        return False
    try:
        user32.SetForegroundWindow(handle)
        user32.BringWindowToTop(handle)
    finally:
        user32.AttachThreadInput(own, other, False)
    return foreground_window() == handle


def allow_any_foreground() -> None:
    """Pass this process's foreground right on to the next process (ASFW_ANY)."""
    api().user32.AllowSetForegroundWindow(0xFFFFFFFF)
