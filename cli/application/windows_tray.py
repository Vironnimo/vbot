"""DPI-aware Windows tray menu with UI-thread lifetime and explicit colors."""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

assert sys.platform == "win32"

from pystray import _win32  # type: ignore[import-untyped]  # noqa: E402

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_user32.GetDC.restype = wintypes.HDC
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDpiForWindow.restype = wintypes.UINT
_user32.GetDpiForWindow.argtypes = [wintypes.HWND]
_user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
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


class _MenuInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.DWORD),
        ("dwStyle", wintypes.DWORD),
        ("cyMax", wintypes.UINT),
        ("hbrBack", wintypes.HBRUSH),
        ("dwContextHelpID", wintypes.DWORD),
        ("dwMenuData", ctypes.c_size_t),
    ]


_user32.SetMenuInfo.argtypes = [wintypes.HMENU, ctypes.POINTER(_MenuInfo)]
_user32.SetMenuInfo.restype = wintypes.BOOL
_gdi32.RoundRect.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
]
_gdi32.GetStockObject.argtypes = [ctypes.c_int]
_gdi32.GetStockObject.restype = wintypes.HGDIOBJ


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
        self._background_brush = None
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
        old_brush = self._background_brush
        self._background_brush = _gdi32.CreateSolidBrush(
            0x00202020 if _apps_use_dark_theme() else 0x00FAFAFA
        )
        super()._update_menu()
        if old_brush:
            _gdi32.DeleteObject(old_brush)

    def _create_menu(self, descriptors, callbacks):
        menu = super()._create_menu(descriptors, callbacks)
        if menu:
            info = _MenuInfo(
                cbSize=ctypes.sizeof(_MenuInfo),
                fMask=0x12,
                dwStyle=0x80000000,
                hbrBack=self._background_brush,
            )
            _user32.SetMenuInfo(menu, ctypes.byref(info))
        return menu

    def _mainloop(self):
        try:
            super()._mainloop()
        finally:
            if self._background_brush:
                _gdi32.DeleteObject(self._background_brush)
                self._background_brush = None

    def _on_stop(self, wparam, lparam):
        if self._menu_open:
            _user32.EndMenu()
        return super()._on_stop(wparam, lparam)

    def _create_menu_item(self, descriptor, callbacks):
        item = super()._create_menu_item(descriptor, callbacks)
        item.fType |= _MFT_OWNERDRAW
        item.wID = len(callbacks)
        item.fMask |= _win32.win32.MIIM_ID | 0x20  # MIIM_DATA
        item.dwItemData = item.wID
        self._labels[item.wID] = (
            "" if descriptor is _win32._base.Menu.SEPARATOR else descriptor.text
        )
        return item

    def _on_measure_item(self, _wparam, lparam):
        item = ctypes.cast(lparam, ctypes.POINTER(_MeasureItem)).contents
        label = self._labels.get(item.itemID)
        if label is None:
            return 0
        dpi = self._dpi()
        item.itemWidth = _scale(238, dpi)
        item.itemHeight = _scale(60 if "\n" in label else 9 if not label else 32, dpi)
        return 1

    def _on_draw_item(self, _wparam, lparam):
        item = ctypes.cast(lparam, ctypes.POINTER(_DrawItem)).contents
        label = self._labels.get(item.itemID)
        if label is None:
            return 0
        dpi = self._dpi()
        dark = _apps_use_dark_theme()
        selected = bool(item.itemState & _ODS_SELECTED) and not item.itemState & _ODS_DISABLED
        disabled = bool(item.itemState & _ODS_DISABLED)
        background = 0x00202020 if dark else 0x00FAFAFA
        foreground = 0x00EEEEEE if dark else 0x00202020
        secondary = 0x00B5B5B5 if dark else 0x00606060
        rect = wintypes.RECT.from_buffer_copy(item.rcItem)
        brush = _gdi32.CreateSolidBrush(background)
        _user32.FillRect(item.hDC, ctypes.byref(rect), brush)
        _gdi32.DeleteObject(brush)
        if not label:
            rect.left += _scale(12, dpi)
            rect.right -= _scale(12, dpi)
            rect.top = (rect.top + rect.bottom) // 2
            rect.bottom = rect.top + 1
            brush = _gdi32.CreateSolidBrush(0x003A3A3A if dark else 0x00DDDDDD)
            _user32.FillRect(item.hDC, ctypes.byref(rect), brush)
            _gdi32.DeleteObject(brush)
            return 1
        if selected:
            brush = _gdi32.CreateSolidBrush(0x00383838 if dark else 0x00EAEAEA)
            previous_brush = _gdi32.SelectObject(item.hDC, brush)
            previous_pen = _gdi32.SelectObject(item.hDC, _gdi32.GetStockObject(8))  # NULL_PEN
            inset = _scale(4, dpi)
            _gdi32.RoundRect(
                item.hDC,
                rect.left + inset,
                rect.top + 1,
                rect.right - inset,
                rect.bottom - 1,
                _scale(8, dpi),
                _scale(8, dpi),
            )
            _gdi32.SelectObject(item.hDC, previous_pen)
            _gdi32.SelectObject(item.hDC, previous_brush)
            _gdi32.DeleteObject(brush)
        rect.left += _scale(14, dpi)
        rect.right -= _scale(14, dpi)
        _gdi32.SetBkMode(item.hDC, 1)
        if "\n" in label:
            title, subtitle = label.split("\n", 1)
            first = wintypes.RECT(
                rect.left, rect.top + _scale(6, dpi), rect.right, rect.top + _scale(31, dpi)
            )
            second = wintypes.RECT(
                rect.left, first.bottom, rect.right, rect.bottom - _scale(6, dpi)
            )
            _draw_text(item.hDC, first, title, dpi, foreground, bold=True)
            _draw_text(item.hDC, second, subtitle, dpi, secondary, points=9)
        else:
            _draw_text(item.hDC, rect, label, dpi, secondary if disabled else foreground)
        return 1

    def _dpi(self) -> int:
        return int(_user32.GetDpiForWindow(self._hwnd) or 96)

    def _on_notify(self, wparam, lparam):
        if self._menu_handle and lparam == _win32.win32.WM_RBUTTONUP:
            self._menu_open = True
            try:
                _win32.win32.SetForegroundWindow(self._hwnd)
                point = wintypes.POINT()
                _win32.win32.GetCursorPos(ctypes.byref(point))
                # The hidden owner follows the tray's monitor before Windows
                # measures its popup, including after a monitor/DPI change.
                _user32.SetWindowPos(self._hwnd, None, point.x, point.y, 0, 0, 0x15)
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


def _menu_font(dpi: int, bold: bool, points: int = 10):
    return _gdi32.CreateFontW(
        -((points * dpi + 36) // 72),
        0,
        0,
        0,
        600 if bold else 400,
        0,
        0,
        0,
        1,
        0,
        0,
        5,
        0,
        "Segoe UI",
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


def _draw_text(dc, rect, text: str, dpi: int, color: int, *, bold: bool = False, points: int = 10):
    _gdi32.SetTextColor(dc, color)
    font = _menu_font(dpi, bold, points)
    previous = _gdi32.SelectObject(dc, font)
    try:
        _user32.DrawTextW(dc, text, len(text), ctypes.byref(rect), _DRAW_FLAGS | 0x8000)  # ellipsis
    finally:
        _gdi32.SelectObject(dc, previous)
        _gdi32.DeleteObject(font)
