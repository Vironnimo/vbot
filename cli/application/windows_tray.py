"""DPI-aware Windows tray menu with UI-thread lifetime and explicit colors."""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from pystray import _win32  # type: ignore[import-untyped]

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_user32.GetDC.restype = wintypes.HDC
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDpiForWindow.restype = wintypes.UINT
_user32.GetDpiForWindow.argtypes = [wintypes.HWND]
_gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
_gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
_gdi32.CreateFontW.restype = wintypes.HFONT
_gdi32.CreateFontW.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPCWSTR,
]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
_gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
_user32.DrawTextW.argtypes = [
    wintypes.HDC,
    wintypes.LPCWSTR,
    ctypes.c_int,
    ctypes.POINTER(wintypes.RECT),
    wintypes.UINT,
]
_gdi32.GetTextExtentPoint32W.argtypes = [
    wintypes.HDC,
    wintypes.LPCWSTR,
    ctypes.c_int,
    ctypes.POINTER(wintypes.SIZE),
]
_gdi32.GetTextExtentPoint32W.restype = wintypes.BOOL

_WM_APPLY_MENU = _win32.win32.WM_USER + 20
_WM_DRAWITEM, _WM_MEASUREITEM, _MFT_OWNERDRAW = 0x2B, 0x2C, 0x100
_ODS_SELECTED, _ODS_DISABLED, _ODS_DEFAULT = 1, 4, 0x20
_DRAW_FLAGS = 0x20 | 0x4 | 0x800  # single line, vertically centered, literal ampersands


class _MeasureItem(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT),
        ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT),
        ("itemWidth", wintypes.UINT),
        ("itemHeight", wintypes.UINT),
        ("itemData", ctypes.c_size_t),
    ]


class _DrawItem(ctypes.Structure):
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


class WindowsTrayIcon(_win32.Icon):
    """Retain native popup input while owning predictable light/dark painting."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._menu_open = False
        self._menu_pending = False
        self._labels: dict[int, str] = {}
        self._message_handlers.update(
            {
                _WM_APPLY_MENU: self._on_apply_menu,
                _WM_DRAWITEM: self._on_draw_item,
                _WM_MEASUREITEM: self._on_measure_item,
            }
        )

    def _update_menu(self):
        if self._menu_open:
            self._menu_pending = True
        elif self._hwnd is None or threading.current_thread() is getattr(self, "_thread", None):
            self._apply_menu()
        else:
            self._menu_pending = True
            _win32.win32.PostMessage(self._hwnd, _WM_APPLY_MENU, 0, 0)

    def _on_apply_menu(self, _wparam, _lparam):
        if not self._menu_open and self._menu_pending:
            self._apply_menu()

    def _apply_menu(self):
        self._menu_pending = False
        self._labels.clear()
        super()._update_menu()

    def _create_menu_item(self, descriptor, callbacks):
        item = super()._create_menu_item(descriptor, callbacks)
        if descriptor is not _win32._base.Menu.SEPARATOR:
            item.fType |= _MFT_OWNERDRAW
            item.dwItemData = item.wID
            self._labels[item.wID] = descriptor.text
        return item

    def _on_measure_item(self, _wparam, lparam):
        item = ctypes.cast(lparam, ctypes.POINTER(_MeasureItem)).contents
        label = self._labels.get(item.itemID)
        if label is None:
            return 0
        dpi = self._dpi()
        dc = _user32.GetDC(self._menu_hwnd)
        font = _menu_font(dpi, False)
        previous = _gdi32.SelectObject(dc, font)
        size = wintypes.SIZE()
        _gdi32.GetTextExtentPoint32W(dc, label, len(label), ctypes.byref(size))
        _gdi32.SelectObject(dc, previous)
        _gdi32.DeleteObject(font)
        _user32.ReleaseDC(self._menu_hwnd, dc)
        item.itemWidth = min(size.cx + _scale(40, dpi), _scale(360, dpi))
        item.itemHeight = max(size.cy + _scale(10, dpi), _scale(28, dpi))
        return 1

    def _on_draw_item(self, _wparam, lparam):
        item = ctypes.cast(lparam, ctypes.POINTER(_DrawItem)).contents
        label = self._labels.get(item.itemID)
        if label is None:
            return 0
        dark = _apps_use_dark_theme()
        selected = bool(item.itemState & _ODS_SELECTED)
        disabled = bool(item.itemState & _ODS_DISABLED)
        background = (
            (0x00433934 if selected else 0x002B2B2B)
            if dark
            else (0x00E5E5E5 if selected else 0x00FFFFFF)
        )
        foreground = 0x00808080 if disabled else (0x00FFFFFF if dark else 0)
        brush = _gdi32.CreateSolidBrush(background)
        _user32.FillRect(item.hDC, ctypes.byref(item.rcItem), brush)
        _gdi32.DeleteObject(brush)
        _gdi32.SetBkMode(item.hDC, 1)
        _gdi32.SetTextColor(item.hDC, foreground)
        font = _menu_font(self._dpi(), bool(item.itemState & _ODS_DEFAULT))
        previous = _gdi32.SelectObject(item.hDC, font)
        rect = item.rcItem
        rect.left += _scale(14, self._dpi())
        _user32.DrawTextW(item.hDC, label, len(label), ctypes.byref(rect), _DRAW_FLAGS)
        _gdi32.SelectObject(item.hDC, previous)
        _gdi32.DeleteObject(font)
        return 1

    def _dpi(self) -> int:
        return int(ctypes.windll.user32.GetDpiForWindow(self._menu_hwnd or self._hwnd) or 96)

    def _on_notify(self, wparam, lparam):
        if self._menu_handle and lparam == _win32.win32.WM_RBUTTONUP:
            self._menu_open = True
            try:
                _win32.win32.SetForegroundWindow(self._hwnd)
                point = wintypes.POINT()
                _win32.win32.GetCursorPos(ctypes.byref(point))
                menu, callbacks = self._menu_handle
                command = _win32.win32.TrackPopupMenuEx(
                    menu,
                    _win32.win32.TPM_RIGHTALIGN
                    | _win32.win32.TPM_BOTTOMALIGN
                    | _win32.win32.TPM_RETURNCMD,
                    point.x,
                    point.y,
                    self._hwnd,
                    None,
                )
                if command > 0:
                    callbacks[command - 1](self)
            finally:
                self._menu_open = False
                if self._menu_pending:
                    self._apply_menu()
            return None
        return super()._on_notify(wparam, lparam)


def _scale(value: int, dpi: int) -> int:
    return max(1, (value * dpi + 48) // 96)


def _menu_font(dpi: int, bold: bool):
    return _gdi32.CreateFontW(
        -((9 * dpi + 36) // 72), 0, 0, 0, 700 if bold else 400, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI"
    )


def _apps_use_dark_theme() -> bool:
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
