"""Win32 bindings, theme colors and drawing helpers shared by the tray windows.

Private DLL instances keep these prototypes from colliding with other ctypes
users in the same process.
"""

from __future__ import annotations

import ctypes
import io
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

assert sys.platform == "win32"

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
uxtheme = ctypes.WinDLL("uxtheme")

WM_NULL, WM_CREATE, WM_DESTROY, WM_SIZE = 0x0000, 0x0001, 0x0002, 0x0005
WM_SETFOCUS, WM_PAINT, WM_CLOSE, WM_ERASEBKGND = 0x0007, 0x000F, 0x0010, 0x0014
WM_SETTINGCHANGE, WM_GETMINMAXINFO, WM_SETFONT = 0x001A, 0x0024, 0x0030
WM_DRAWITEM, WM_MEASUREITEM, WM_SETICON, WM_CONTEXTMENU = 0x002B, 0x002C, 0x0080, 0x007B
WM_COMMAND, WM_CTLCOLOREDIT, WM_CTLCOLORBTN, WM_CTLCOLORSTATIC = 0x0111, 0x0133, 0x0135, 0x0138
WM_DPICHANGED, WM_THEMECHANGED, WM_USER, WM_APP = 0x02E0, 0x031A, 0x0400, 0x8000
WS_OVERLAPPEDWINDOW, WS_CLIPCHILDREN, WS_CHILD, WS_VISIBLE = (
    0x00CF0000,
    0x02000000,
    0x40000000,
    (0x10000000),
)
WS_TABSTOP, WS_VSCROLL, WS_EX_CONTROLPARENT = 0x00010000, 0x00200000, 0x00010000
ES_MULTILINE, ES_AUTOVSCROLL, ES_READONLY = 0x0004, 0x0040, 0x0800
BS_PUSHBUTTON, BS_DEFPUSHBUTTON = 0x0000, 0x0001
EM_SETSEL, EM_SCROLLCARET, EM_SETMARGINS = 0x00B1, 0x00B7, 0x00D3
SW_SHOW, SW_RESTORE = 5, 9
IDC_ARROW = 32512
SM_CXSMICON, SM_CXICON = 49, 11
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
DT_WORD_ELLIPSIS, DT_SINGLELINE, DT_VCENTER, DT_NOPREFIX, DT_END_ELLIPSIS = (
    0x40000,
    0x20,
    0x4,
    0x800,
    0x8000,
)
DT_PATH_ELLIPSIS, DT_CALCRECT, DT_WORDBREAK = 0x4000, 0x400, 0x10
MONITOR_DEFAULTTONEAREST = 2


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class MEASUREITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT),
        ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT),
        ("itemWidth", wintypes.UINT),
        ("itemHeight", wintypes.UINT),
        ("itemData", ctypes.c_size_t),
    ]


class DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT),
        ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT),
        ("itemAction", wintypes.UINT),
        ("itemState", wintypes.UINT),
        ("hwndItem", wintypes.HWND),
        ("hDC", wintypes.HDC),
        ("rcItem", wintypes.RECT),
        ("itemData", ctypes.c_size_t),
    ]


class MENUINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.DWORD),
        ("dwStyle", wintypes.DWORD),
        ("cyMax", wintypes.UINT),
        ("hbrBack", wintypes.HBRUSH),
        ("dwContextHelpID", wintypes.DWORD),
        ("dwMenuData", ctypes.c_size_t),
    ]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", wintypes.POINT),
        ("ptMaxSize", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _prototype(library: Any, name: str, restype: Any, *argtypes: Any) -> None:
    function = getattr(library, name)
    function.restype = restype
    function.argtypes = list(argtypes)


_H, _U, _I, _B = wintypes.HANDLE, wintypes.UINT, ctypes.c_int, wintypes.BOOL
_HWND, _WP, _LP = wintypes.HWND, wintypes.WPARAM, wintypes.LPARAM
_PRECT = ctypes.POINTER(wintypes.RECT)
_args: tuple[Any, ...]
for _name, _restype, _args in (
    ("RegisterClassExW", wintypes.ATOM, (ctypes.POINTER(WNDCLASSEXW),)),
    ("UnregisterClassW", _B, (wintypes.LPCWSTR, wintypes.HINSTANCE)),
    (
        "CreateWindowExW",
        _HWND,
        (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            _I,
            _I,
            _I,
            _I,
            _HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ),
    ),
    ("DestroyWindow", _B, (_HWND,)),
    ("DefWindowProcW", LRESULT, (_HWND, _U, _WP, _LP)),
    ("GetMessageW", _I, (ctypes.POINTER(wintypes.MSG), _HWND, _U, _U)),
    ("TranslateMessage", _B, (ctypes.POINTER(wintypes.MSG),)),
    ("DispatchMessageW", LRESULT, (ctypes.POINTER(wintypes.MSG),)),
    ("IsDialogMessageW", _B, (_HWND, ctypes.POINTER(wintypes.MSG))),
    ("PostMessageW", _B, (_HWND, _U, _WP, _LP)),
    ("SendMessageW", LRESULT, (_HWND, _U, _WP, _LP)),
    ("PostQuitMessage", None, (_I,)),
    ("RegisterWindowMessageW", _U, (wintypes.LPCWSTR,)),
    ("LoadCursorW", _H, (wintypes.HINSTANCE, wintypes.LPVOID)),
    ("SetCursor", _H, (_H,)),
    ("SetForegroundWindow", _B, (_HWND,)),
    ("AllowSetForegroundWindow", _B, (wintypes.DWORD,)),
    ("GetForegroundWindow", _HWND, ()),
    ("CreatePopupMenu", wintypes.HMENU, ()),
    ("AppendMenuW", _B, (wintypes.HMENU, _U, ctypes.c_size_t, ctypes.c_size_t)),
    ("DestroyMenu", _B, (wintypes.HMENU,)),
    ("SetMenuInfo", _B, (wintypes.HMENU, ctypes.POINTER(MENUINFO))),
    ("SetMenuDefaultItem", _B, (wintypes.HMENU, _U, _U)),
    ("EndMenu", _B, ()),
    ("TrackPopupMenuEx", _B, (wintypes.HMENU, _U, _I, _I, _HWND, wintypes.LPVOID)),
    ("GetCursorPos", _B, (ctypes.POINTER(wintypes.POINT),)),
    ("SetWindowPos", _B, (_HWND, _HWND, _I, _I, _I, _I, _U)),
    ("GetDpiForWindow", _U, (_HWND,)),
    ("GetSystemMetricsForDpi", _I, (_I, _U)),
    (
        "CreateIconFromResourceEx",
        wintypes.HICON,
        (ctypes.c_char_p, wintypes.DWORD, _B, wintypes.DWORD, _I, _I, _U),
    ),
    ("DestroyIcon", _B, (wintypes.HICON,)),
    ("GetDC", wintypes.HDC, (_HWND,)),
    ("ReleaseDC", _I, (_HWND, wintypes.HDC)),
    ("FillRect", _I, (wintypes.HDC, _PRECT, wintypes.HBRUSH)),
    ("DrawTextW", _I, (wintypes.HDC, wintypes.LPCWSTR, _I, _PRECT, _U)),
    ("BeginPaint", wintypes.HDC, (_HWND, ctypes.POINTER(PAINTSTRUCT))),
    ("EndPaint", _B, (_HWND, ctypes.POINTER(PAINTSTRUCT))),
    ("InvalidateRect", _B, (_HWND, _PRECT, _B)),
    ("GetClientRect", _B, (_HWND, _PRECT)),
    ("MoveWindow", _B, (_HWND, _I, _I, _I, _I, _B)),
    ("ShowWindow", _B, (_HWND, _I)),
    ("IsIconic", _B, (_HWND,)),
    ("IsWindow", _B, (_HWND,)),
    ("SetWindowTextW", _B, (_HWND, wintypes.LPCWSTR)),
    ("EnableWindow", _B, (_HWND, _B)),
    ("SetFocus", _HWND, (_HWND,)),
    ("GetWindowThreadProcessId", wintypes.DWORD, (_HWND, ctypes.POINTER(wintypes.DWORD))),
    (
        "SetWinEventHook",
        _H,
        (
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HMODULE,
            WINEVENTPROC,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        ),
    ),
    ("UnhookWinEvent", _B, (_H,)),
    ("AdjustWindowRectExForDpi", _B, (_PRECT, wintypes.DWORD, _B, wintypes.DWORD, _U)),
    ("MonitorFromPoint", _H, (wintypes.POINT, wintypes.DWORD)),
    ("GetMonitorInfoW", _B, (_H, ctypes.POINTER(MONITORINFO))),
):
    _prototype(user32, _name, _restype, *_args)
_prototype(shell32, "Shell_NotifyIconW", _B, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
_prototype(shell32, "SetCurrentProcessExplicitAppUserModelID", ctypes.c_long, wintypes.LPCWSTR)
for _name, _restype, _args in (
    ("CreateSolidBrush", wintypes.HBRUSH, (wintypes.COLORREF,)),
    (
        "CreateFontW",
        wintypes.HFONT,
        (_I, _I, _I, _I, _I, *(wintypes.DWORD,) * 8, wintypes.LPCWSTR),
    ),
    ("SelectObject", wintypes.HGDIOBJ, (wintypes.HDC, wintypes.HGDIOBJ)),
    ("DeleteObject", _B, (wintypes.HGDIOBJ,)),
    ("SetBkMode", _I, (wintypes.HDC, _I)),
    ("SetTextColor", wintypes.COLORREF, (wintypes.HDC, wintypes.COLORREF)),
    ("SetBkColor", wintypes.COLORREF, (wintypes.HDC, wintypes.COLORREF)),
    ("RoundRect", _B, (wintypes.HDC, _I, _I, _I, _I, _I, _I)),
    ("GetStockObject", wintypes.HGDIOBJ, (_I,)),
    (
        "GetTextExtentPoint32W",
        _B,
        (wintypes.HDC, wintypes.LPCWSTR, _I, ctypes.POINTER(wintypes.SIZE)),
    ),
    ("CreateCompatibleDC", wintypes.HDC, (wintypes.HDC,)),
    ("CreateCompatibleBitmap", wintypes.HBITMAP, (wintypes.HDC, _I, _I)),
    ("DeleteDC", _B, (wintypes.HDC,)),
    ("GetPixel", wintypes.COLORREF, (wintypes.HDC, _I, _I)),
):
    _prototype(gdi32, _name, _restype, *_args)
for _name, _restype, _args in (
    ("GetModuleHandleW", wintypes.HMODULE, (wintypes.LPCWSTR,)),
    ("OpenProcess", _H, (wintypes.DWORD, _B, wintypes.DWORD)),
    ("CloseHandle", _B, (_H,)),
    (
        "QueryFullProcessImageNameW",
        _B,
        (_H, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)),
    ),
):
    _prototype(kernel32, _name, _restype, *_args)
_prototype(
    dwmapi,
    "DwmSetWindowAttribute",
    ctypes.c_long,
    _HWND,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
)
_prototype(uxtheme, "SetWindowTheme", ctypes.c_long, _HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)


@dataclass(frozen=True)
class Palette:
    """Explicit colors (COLORREF, 0x00BBGGRR) for one app theme."""

    dark: bool
    background: int
    foreground: int
    secondary: int
    hover: int
    separator: int
    error: int
    field: int


DARK = Palette(
    True, 0x00202020, 0x00EEEEEE, 0x00B5B5B5, 0x00383838, 0x003A3A3A, 0x006B6BFF, 0x002B2B2B
)
LIGHT = Palette(
    False, 0x00FAFAFA, 0x00202020, 0x00606060, 0x00EAEAEA, 0x00DDDDDD, 0x001C2BC4, 0x00FFFFFF
)


def palette() -> Palette:
    return DARK if apps_use_dark_theme() else LIGHT


def apps_use_dark_theme() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return bool(value == 0)
    except OSError:
        return False


def scale(value: int, dpi: int) -> int:
    return max(1, (value * dpi + 48) // 96)


def create_font(dpi: int, points: float = 10, *, weight: int = 400, face: str = "Segoe UI") -> int:
    return int(
        gdi32.CreateFontW(-round(points * dpi / 72), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face)
    )


def draw_text(dc: int, rect: wintypes.RECT, text: str, font: int, color: int, flags: int) -> None:
    gdi32.SetTextColor(dc, color)
    previous = gdi32.SelectObject(dc, font)
    try:
        user32.DrawTextW(dc, text, len(text), ctypes.byref(rect), flags | DT_NOPREFIX)
    finally:
        gdi32.SelectObject(dc, previous)


def text_width(dc: int, text: str, font: int) -> int:
    previous = gdi32.SelectObject(dc, font)
    try:
        size = wintypes.SIZE()
        gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size))
        return int(size.cx)
    finally:
        gdi32.SelectObject(dc, previous)


def fill(dc: int, rect: wintypes.RECT, color: int) -> None:
    brush = gdi32.CreateSolidBrush(color)
    user32.FillRect(dc, ctypes.byref(rect), brush)
    gdi32.DeleteObject(brush)


def round_fill(dc: int, rect: wintypes.RECT, color: int, radius: int) -> None:
    brush = gdi32.CreateSolidBrush(color)
    previous_brush = gdi32.SelectObject(dc, brush)
    previous_pen = gdi32.SelectObject(dc, gdi32.GetStockObject(8))  # NULL_PEN
    gdi32.RoundRect(dc, rect.left, rect.top, rect.right, rect.bottom, radius, radius)
    gdi32.SelectObject(dc, previous_pen)
    gdi32.SelectObject(dc, previous_brush)
    gdi32.DeleteObject(brush)


def icon_from_image(image: Any) -> int:
    """Create an HICON from a square RGBA Pillow image."""

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = buffer.getvalue()
    handle = user32.CreateIconFromResourceEx(
        data, len(data), True, 0x00030000, image.width, image.height, 0
    )
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def use_dark_title_bar(hwnd: int, dark: bool) -> None:
    value = ctypes.c_int(1 if dark else 0)
    dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))


def loword(value: int) -> int:
    return value & 0xFFFF


def hiword(value: int) -> int:
    return (value >> 16) & 0xFFFF


def signed_word(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value
