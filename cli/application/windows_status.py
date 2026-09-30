"""Native vBot status window: version, server, update source and update progress.

The window lives on the tray's UI thread. Its activity pane shows update
progress like the console of a waiting ``vbot update``; its buttons invoke the
same controller actions as the tray menu.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from ctypes import wintypes

assert sys.platform == "win32"

from cli.application import windows_native as native  # noqa: E402
from cli.application.tray import TrayMenuItem, TrayStatus  # noqa: E402

_LOGGER = logging.getLogger("vbot.application.tray")

_CLASS_NAME = "vBotTrayStatus"
_BUTTON_SLOTS = 3
_BUTTON_ID = 100
_IDCANCEL = 2
_SIZE_MINIMIZED = 1
_EMPTY_ACTIVITY = "No update activity since the tray started."
_PATH_ROWS = frozenset({"Data", "Installed in"})

_shcore = ctypes.WinDLL("shcore")
_shcore.GetDpiForMonitor.restype = ctypes.c_long
_shcore.GetDpiForMonitor.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT),
]


class StatusWindow:
    """One reusable status window; its native window exists only while shown."""

    def __init__(
        self,
        invoke: Callable[[str], None],
        big_icon: Callable[[], int],
        small_icon: Callable[[], int],
    ) -> None:
        self.hwnd = 0
        self._invoke = invoke
        self._big_icon = big_icon
        self._small_icon = small_icon
        self._status: TrayStatus | None = None
        self._colors = native.palette()
        self._dpi = 96
        self._fonts: dict[str, int] = {}
        self._background_brush = 0
        self._field_brush = 0
        self._edit = 0
        self._activity = ""
        self._buttons: list[int] = []
        self._button_items: list[TrayMenuItem | None] = [None] * _BUTTON_SLOTS
        self._close = 0
        self._minimized = False
        self._registered = False
        self._procedure = native.WNDPROC(self._window_proc)

    @property
    def visible(self) -> bool:
        """Whether the window is open and not minimized; readable from any thread."""

        return bool(self.hwnd) and not self._minimized

    def show(self, status: TrayStatus) -> None:
        self._status = status
        if not self.hwnd:
            self._create()
        self._apply()
        if native.user32.IsIconic(self.hwnd):
            native.user32.ShowWindow(self.hwnd, native.SW_RESTORE)
        native.user32.ShowWindow(self.hwnd, native.SW_SHOW)
        native.user32.SetForegroundWindow(self.hwnd)

    def update(self, status: TrayStatus) -> None:
        self._status = status
        if self.hwnd:
            self._apply()

    def destroy(self) -> None:
        if self.hwnd:
            native.user32.DestroyWindow(self.hwnd)

    # Creation and layout.

    def _create(self) -> None:
        instance = native.kernel32.GetModuleHandleW(None)
        if not self._registered:
            window_class = native.WNDCLASSEXW(
                cbSize=ctypes.sizeof(native.WNDCLASSEXW),
                style=0x3,  # CS_HREDRAW | CS_VREDRAW
                lpfnWndProc=self._procedure,
                hInstance=instance,
                hCursor=native.user32.LoadCursorW(None, native.IDC_ARROW),
                lpszClassName=_CLASS_NAME,
            )
            if not native.user32.RegisterClassExW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._registered = True
        cursor = wintypes.POINT()
        native.user32.GetCursorPos(ctypes.byref(cursor))
        monitor = native.user32.MonitorFromPoint(cursor, native.MONITOR_DEFAULTTONEAREST)
        info = native.MONITORINFO(cbSize=ctypes.sizeof(native.MONITORINFO))
        native.user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
        _shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        self._dpi = int(dpi_x.value) or 96
        style = native.WS_OVERLAPPEDWINDOW | native.WS_CLIPCHILDREN
        frame = wintypes.RECT(0, 0, native.scale(620, self._dpi), native.scale(480, self._dpi))
        native.user32.AdjustWindowRectExForDpi(
            ctypes.byref(frame), style, False, native.WS_EX_CONTROLPARENT, self._dpi
        )
        width, height = frame.right - frame.left, frame.bottom - frame.top
        work = info.rcWork
        x = work.left + max(0, (work.right - work.left - width) // 2)
        y = work.top + max(0, (work.bottom - work.top - height) // 2)
        self.hwnd = int(
            native.user32.CreateWindowExW(
                native.WS_EX_CONTROLPARENT,
                _CLASS_NAME,
                "vBot status",
                style,
                x,
                y,
                width,
                height,
                None,
                None,
                instance,
                None,
            )
            or 0
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._dpi = int(native.user32.GetDpiForWindow(self.hwnd) or self._dpi)
        native.user32.SendMessageW(self.hwnd, native.WM_SETICON, 1, self._big_icon())
        native.user32.SendMessageW(self.hwnd, native.WM_SETICON, 0, self._small_icon())
        control = native.WS_CHILD | native.WS_VISIBLE | native.WS_TABSTOP
        self._edit = self._child(
            "EDIT",
            "",
            control
            | native.WS_VSCROLL
            | native.ES_MULTILINE
            | native.ES_READONLY
            | native.ES_AUTOVSCROLL,
            0,
        )
        self._buttons = [
            self._child("BUTTON", "", control | native.BS_PUSHBUTTON, _BUTTON_ID + slot)
            for slot in range(_BUTTON_SLOTS)
        ]
        self._close = self._child("BUTTON", "Close", control | native.BS_DEFPUSHBUTTON, _IDCANCEL)
        self._activity = "\0"
        self._theme()
        self._layout()

    def _child(self, kind: str, text: str, style: int, identifier: int) -> int:
        handle = native.user32.CreateWindowExW(
            0,
            kind,
            text,
            style,
            0,
            0,
            0,
            0,
            self.hwnd,
            wintypes.HMENU(identifier),
            native.kernel32.GetModuleHandleW(None),
            None,
        )
        return int(handle or 0)

    def _theme(self) -> None:
        self._colors = native.palette()
        for brush in (self._background_brush, self._field_brush):
            if brush:
                native.gdi32.DeleteObject(brush)
        self._background_brush = int(native.gdi32.CreateSolidBrush(self._colors.background))
        self._field_brush = int(native.gdi32.CreateSolidBrush(self._colors.field))
        native.use_dark_title_bar(self.hwnd, self._colors.dark)
        theme = "DarkMode_Explorer" if self._colors.dark else "Explorer"
        for control in (self._edit, *self._buttons, self._close):
            native.uxtheme.SetWindowTheme(control, theme, None)
        self._create_fonts()
        native.user32.InvalidateRect(self.hwnd, None, True)

    def _create_fonts(self) -> None:
        for font in self._fonts.values():
            native.gdi32.DeleteObject(font)
        dpi = self._dpi
        self._fonts = {
            "title": native.create_font(dpi, 15, weight=600),
            "text": native.create_font(dpi, 10),
            "caption": native.create_font(dpi, 10, weight=600),
            "mono": native.create_font(dpi, 9.5, face="Consolas"),
        }
        native.user32.SendMessageW(self._edit, native.WM_SETFONT, self._fonts["mono"], 1)
        for button in (*self._buttons, self._close):
            native.user32.SendMessageW(button, native.WM_SETFONT, self._fonts["text"], 1)
        margin = native.scale(10, dpi)
        native.user32.SendMessageW(self._edit, native.EM_SETMARGINS, 3, margin | (margin << 16))

    def _metrics(self) -> dict[str, int]:
        s = self._dpi
        rows = len(self._status.rows) if self._status is not None else 0
        margin = native.scale(24, s)
        top = margin + native.scale(30, s) + native.scale(22, s) + native.scale(16, s)
        caption = top + rows * native.scale(24, s) + native.scale(14, s)
        return {
            "margin": margin,
            "rows": top,
            "caption": caption,
            "activity": caption + native.scale(28, s),
            "button": native.scale(32, s),
        }

    def _layout(self) -> None:
        if not self.hwnd:
            return
        client = wintypes.RECT()
        native.user32.GetClientRect(self.hwnd, ctypes.byref(client))
        metrics = self._metrics()
        margin, button = metrics["margin"], metrics["button"]
        bottom = client.bottom - margin - button
        inset = native.scale(1, self._dpi)
        native.user32.MoveWindow(
            self._edit,
            margin + inset,
            metrics["activity"] + inset,
            max(0, client.right - 2 * margin - 2 * inset),
            max(0, bottom - native.scale(16, self._dpi) - metrics["activity"] - 2 * inset),
            True,
        )
        dc = native.user32.GetDC(self.hwnd)
        try:
            x = margin
            for slot, handle in enumerate(self._buttons):
                item = self._button_items[slot]
                if item is None:
                    native.user32.ShowWindow(handle, 0)
                    continue
                width = self._button_width(dc, item.label)
                native.user32.MoveWindow(handle, x, bottom, width, button, True)
                native.user32.ShowWindow(handle, native.SW_SHOW)
                x += width + native.scale(8, self._dpi)
            width = self._button_width(dc, "Close")
            native.user32.MoveWindow(
                self._close, client.right - margin - width, bottom, width, button, True
            )
        finally:
            native.user32.ReleaseDC(self.hwnd, dc)
        native.user32.InvalidateRect(self.hwnd, None, True)

    def _button_width(self, dc: int, label: str) -> int:
        padding = native.scale(28, self._dpi)
        return max(
            native.scale(96, self._dpi), native.text_width(dc, label, self._fonts["text"]) + padding
        )

    def _apply(self) -> None:
        status = self._status
        if status is None or not self.hwnd:
            return
        activity = "\r\n".join(status.activity) or _EMPTY_ACTIVITY
        if activity != self._activity:
            self._activity = activity
            native.user32.SetWindowTextW(self._edit, activity)
            end = len(activity)
            native.user32.SendMessageW(self._edit, native.EM_SETSEL, end, end)
            native.user32.SendMessageW(self._edit, native.EM_SCROLLCARET, 0, 0)
        items: list[TrayMenuItem | None] = list(status.buttons[:_BUTTON_SLOTS])
        items.extend([None] * (_BUTTON_SLOTS - len(items)))
        relabel = items != self._button_items
        self._button_items = items
        for handle, item in zip(self._buttons, items, strict=True):
            if item is not None:
                native.user32.SetWindowTextW(handle, item.label)
                native.user32.EnableWindow(handle, item.enabled)
        if relabel:
            self._layout()
        else:
            native.user32.InvalidateRect(self.hwnd, None, True)

    # Messages.

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            result = self._handle(hwnd, message, wparam, lparam)
        except Exception:
            _LOGGER.exception("Status window message %#x failed", message)
            result = None
        if result is None:
            return int(native.user32.DefWindowProcW(hwnd, message, wparam, lparam))
        return result

    def _handle(self, hwnd: int, message: int, wparam: int, lparam: int) -> int | None:
        if message == native.WM_PAINT:
            self._paint(hwnd)
            return 0
        if message == native.WM_ERASEBKGND:
            return 1
        if message == native.WM_SIZE:
            self._minimized = wparam == _SIZE_MINIMIZED
            self._layout()
            return 0
        if message == native.WM_GETMINMAXINFO:
            info = ctypes.cast(lparam, ctypes.POINTER(native.MINMAXINFO)).contents
            info.ptMinTrackSize.x = native.scale(480, self._dpi)
            info.ptMinTrackSize.y = native.scale(380, self._dpi)
            return 0
        if message == native.WM_DPICHANGED:
            self._dpi = native.hiword(wparam) or 96
            suggested = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            self._create_fonts()
            native.user32.SetWindowPos(
                hwnd,
                None,
                suggested.left,
                suggested.top,
                suggested.right - suggested.left,
                suggested.bottom - suggested.top,
                native.SWP_NOZORDER | native.SWP_NOACTIVATE,
            )
            self._layout()
            return 0
        if message in {native.WM_SETTINGCHANGE, native.WM_THEMECHANGED}:
            if native.palette() != self._colors:
                self._theme()
            return None
        if message in {native.WM_CTLCOLORSTATIC, native.WM_CTLCOLOREDIT}:
            native.gdi32.SetTextColor(wparam, self._colors.foreground)
            native.gdi32.SetBkColor(wparam, self._colors.field)
            return self._field_brush
        if message == native.WM_CTLCOLORBTN:
            return self._background_brush
        if message == native.WM_COMMAND:
            identifier = native.loword(wparam)
            if identifier == _IDCANCEL:
                native.user32.DestroyWindow(hwnd)
            elif _BUTTON_ID <= identifier < _BUTTON_ID + _BUTTON_SLOTS:
                item = self._button_items[identifier - _BUTTON_ID]
                if item is not None and item.action:
                    self._invoke(item.action)
            return 0
        if message == native.WM_DESTROY:
            self._release()
            return 0
        return None

    def _release(self) -> None:
        for font in self._fonts.values():
            native.gdi32.DeleteObject(font)
        self._fonts = {}
        for brush in (self._background_brush, self._field_brush):
            if brush:
                native.gdi32.DeleteObject(brush)
        self._background_brush = self._field_brush = 0
        self.hwnd = self._edit = self._close = 0
        self._minimized = False
        self._buttons = []
        self._button_items = [None] * _BUTTON_SLOTS

    def _paint(self, hwnd: int) -> None:
        paint = native.PAINTSTRUCT()
        dc = native.user32.BeginPaint(hwnd, ctypes.byref(paint))
        try:
            self._paint_content(hwnd, dc)
        finally:
            native.user32.EndPaint(hwnd, ctypes.byref(paint))

    def _paint_content(self, hwnd: int, dc: int) -> None:
        colors, s = self._colors, self._dpi
        client = wintypes.RECT()
        native.user32.GetClientRect(hwnd, ctypes.byref(client))
        native.fill(dc, client, colors.background)
        status = self._status
        if status is None:
            return
        native.gdi32.SetBkMode(dc, 1)
        metrics = self._metrics()
        margin = metrics["margin"]
        right = client.right - margin
        line = native.DT_SINGLELINE | native.DT_VCENTER
        top = margin
        native.draw_text(
            dc,
            wintypes.RECT(margin, top, right, top + native.scale(30, s)),
            status.title,
            self._fonts["title"],
            colors.foreground,
            line | native.DT_END_ELLIPSIS,
        )
        top += native.scale(30, s)
        native.draw_text(
            dc,
            wintypes.RECT(margin, top, right, top + native.scale(22, s)),
            status.headline,
            self._fonts["text"],
            colors.error if status.failed else colors.secondary,
            line | native.DT_END_ELLIPSIS,
        )
        top = metrics["rows"]
        label_width = native.scale(112, s)
        for label, value in status.rows:
            row = native.scale(24, s)
            native.draw_text(
                dc,
                wintypes.RECT(margin, top, margin + label_width, top + row),
                label,
                self._fonts["text"],
                colors.secondary,
                line | native.DT_END_ELLIPSIS,
            )
            ellipsis = native.DT_PATH_ELLIPSIS if label in _PATH_ROWS else native.DT_END_ELLIPSIS
            native.draw_text(
                dc,
                wintypes.RECT(margin + label_width, top, right, top + row),
                value,
                self._fonts["text"],
                colors.foreground,
                line | ellipsis,
            )
            top += row
        caption = metrics["caption"]
        native.draw_text(
            dc,
            wintypes.RECT(margin, caption, right, caption + native.scale(24, s)),
            "Update activity",
            self._fonts["caption"],
            colors.foreground,
            line,
        )
        bottom = client.bottom - margin - metrics["button"] - native.scale(16, s)
        native.round_fill(
            dc,
            wintypes.RECT(margin, metrics["activity"], right, bottom),
            colors.field,
            native.scale(8, s),
        )
