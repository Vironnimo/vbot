"""Native Windows notification-area icon, menu and toasts for the tray host.

One hidden window on the host's main thread owns the notification icon
(``NOTIFYICON_VERSION_4``), the owner-drawn popup menu, legacy-balloon toasts
(which Windows 10/11 shows as toast notifications) and the status window.
Other threads hand work to it through a queue and one posted message.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import sys
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol

assert sys.platform == "win32"

from cli.application import windows_native as native  # noqa: E402
from cli.application.integration import (  # noqa: E402
    NOTIFICATION_APP_ID,
    register_notification_identity,
)
from cli.application.notifications import Toast  # noqa: E402
from cli.application.tray import TrayMenuItem, TrayPresentation  # noqa: E402

_LOGGER = logging.getLogger("vbot.application.tray")

_CLASS_NAME = "vBotTrayHost"
_ICON_ID = 1
_WM_TRAY = native.WM_APP + 1
_WM_WAKE = native.WM_APP + 2
_NIM_ADD, _NIM_MODIFY, _NIM_DELETE, _NIM_SETVERSION = 0, 1, 2, 4
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO, _NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x10, 0x80
_NIIF_USER, _NIIF_LARGE_ICON, _NIIF_RESPECT_QUIET_TIME = 0x4, 0x20, 0x80
_NIN_SELECT, _NIN_KEYSELECT = 0x400, 0x401
_NIN_BALLOONSHOW, _NIN_BALLOONHIDE, _NIN_BALLOONTIMEOUT, _NIN_BALLOONUSERCLICK = (
    0x402,
    0x403,
    0x404,
    0x405,
)
_MF_GRAYED, _MF_OWNERDRAW, _MF_SEPARATOR = 0x1, 0x100, 0x800
_TPM_RIGHTBUTTON, _TPM_RIGHTALIGN, _TPM_BOTTOMALIGN, _TPM_RETURNCMD = 0x2, 0x8, 0x20, 0x100
_ODS_SELECTED, _ODS_DISABLED = 0x1, 0x4
_EVENT_SYSTEM_FOREGROUND = 0x0003
_WINEVENT_SKIPOWNPROCESS = 0x0002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ASFW_ANY = 0xFFFFFFFF
_DESKTOP_EXECUTABLE = "vbot.desktop.exe"
_BADGES = {
    "stopped": (138, 138, 138, 255),
    "updating": (47, 124, 246, 255),
    "error": (229, 72, 77, 255),
}


class TrayCommands(Protocol):
    """Controller entry points the native view calls on its UI thread."""

    def invoke(self, action: str) -> None: ...

    def invoke_default(self) -> None: ...

    def activate_toast(self, toast: Toast) -> None: ...


class WindowsTray:
    """Native tray view; public methods are safe from any thread."""

    def __init__(self, commands: TrayCommands, icon_path: Path) -> None:
        self._commands = commands
        self._icon_path = icon_path
        self._calls: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._hwnd = 0
        self._instance = 0
        self._taskbar_created = 0
        self._presentation: TrayPresentation | None = None
        self._icon_state = ""
        self._icon_dpi = 0
        self._icons: dict[tuple[str, int], int] = {}
        self._menu_labels: dict[int, TrayMenuItem] = {}
        self._menu_open = False
        self._stopping = False
        self._toast: Toast | None = None
        self._foreground_hook = 0
        self._status: Any | None = None
        self._base_image: Any | None = None
        # Callbacks stay referenced for the lifetime of their native registrations.
        self._window_procedure = native.WNDPROC(self._window_proc)
        self._foreground_procedure = native.WINEVENTPROC(self._on_foreground)

    # Thread-safe view interface.

    def present(self, presentation: TrayPresentation) -> None:
        self._post(lambda: self._apply(presentation))

    def show_toast(self, toast: Toast) -> None:
        self._post(lambda: self._show_toast(toast))

    def dismiss_toast(self, key: str) -> None:
        self._post(lambda: self._dismiss_toast(key))

    def show_status(self) -> None:
        self._post(self._open_status)

    def stop(self) -> None:
        self._post(self._begin_stop)

    def run(self) -> None:
        """Create the icon and pump the host thread's message loop until stopped."""

        try:
            register_notification_identity(self._icon_path)
        except OSError:
            _LOGGER.warning("Could not register the vBot notification identity", exc_info=True)
        native.shell32.SetCurrentProcessExplicitAppUserModelID(NOTIFICATION_APP_ID)
        self._create_window()
        self._add_icon()
        self._drain()
        message = wintypes.MSG()
        while True:
            result = native.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result in {0, -1}:
                break
            status = self._status.hwnd if self._status is not None else 0
            if status and native.user32.IsDialogMessageW(status, ctypes.byref(message)):
                continue
            native.user32.TranslateMessage(ctypes.byref(message))
            native.user32.DispatchMessageW(ctypes.byref(message))
        self._release()

    # UI thread.

    def _post(self, call: Callable[[], None]) -> None:
        self._calls.put(call)
        if self._hwnd:
            native.user32.PostMessageW(self._hwnd, _WM_WAKE, 0, 0)

    def _drain(self) -> None:
        while True:
            try:
                call = self._calls.get_nowait()
            except queue.Empty:
                return
            try:
                call()
            except Exception:
                _LOGGER.exception("Tray view update failed")

    def _create_window(self) -> None:
        self._instance = int(native.kernel32.GetModuleHandleW(None) or 0)
        window_class = native.WNDCLASSEXW(
            cbSize=ctypes.sizeof(native.WNDCLASSEXW),
            lpfnWndProc=self._window_procedure,
            hInstance=self._instance,
            hCursor=native.user32.LoadCursorW(None, native.IDC_ARROW),
            lpszClassName=_CLASS_NAME,
        )
        if not native.user32.RegisterClassExW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        # A hidden top-level window, unlike a message-only one, receives the
        # TaskbarCreated broadcast after Explorer restarts.
        self._hwnd = int(
            native.user32.CreateWindowExW(
                0, _CLASS_NAME, "vBot", 0, 0, 0, 0, 0, None, None, self._instance, None
            )
            or 0
        )
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._taskbar_created = native.user32.RegisterWindowMessageW("TaskbarCreated")

    def _release(self) -> None:
        self._unhook_foreground()
        for handle in self._icons.values():
            native.user32.DestroyIcon(handle)
        self._icons.clear()
        native.user32.UnregisterClassW(_CLASS_NAME, self._instance)

    def _begin_stop(self) -> None:
        self._stopping = True
        if self._menu_open:
            native.user32.EndMenu()
            return
        self._destroy()

    def _destroy(self) -> None:
        if self._status is not None:
            self._status.destroy()
        self._notify(_NIM_DELETE, 0)
        native.user32.DestroyWindow(self._hwnd)

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            result = self._handle(hwnd, message, wparam, lparam)
        except Exception:
            _LOGGER.exception("Tray window message %#x failed", message)
            result = None
        if result is None:
            return int(native.user32.DefWindowProcW(hwnd, message, wparam, lparam))
        return result

    def _handle(self, hwnd: int, message: int, wparam: int, lparam: int) -> int | None:
        if message == _WM_WAKE:
            self._drain()
            return 0
        if message == _WM_TRAY:
            self._on_tray(native.loword(lparam), wparam)
            return 0
        if message == native.WM_MEASUREITEM:
            return self._measure_item(lparam)
        if message == native.WM_DRAWITEM:
            return self._draw_item(lparam)
        if message == self._taskbar_created and self._taskbar_created:
            self._add_icon()
            return 0
        if message in {native.WM_DPICHANGED, native.WM_SETTINGCHANGE}:
            self._refresh_icon()
            return None
        if message == native.WM_DESTROY:
            native.user32.PostQuitMessage(0)
            return 0
        return None

    def _on_tray(self, event: int, anchor: int) -> None:
        if event == native.WM_CONTEXTMENU:
            self._track_menu(native.signed_word(anchor), native.signed_word(anchor >> 16))
        elif event in {_NIN_SELECT, _NIN_KEYSELECT}:
            native.user32.AllowSetForegroundWindow(_ASFW_ANY)
            self._commands.invoke_default()
        elif event == _NIN_BALLOONUSERCLICK:
            toast = self._toast
            self._toast_closed()
            if toast is not None:
                native.user32.AllowSetForegroundWindow(_ASFW_ANY)
                self._commands.activate_toast(toast)
        elif event in {_NIN_BALLOONHIDE, _NIN_BALLOONTIMEOUT}:
            self._toast_closed()

    # Icon and tooltip.

    def _add_icon(self) -> None:
        self._icon_dpi = 0
        data = self._icon_data(_NIF_MESSAGE | _NIF_ICON | _NIF_TIP | _NIF_SHOWTIP)
        if not native.shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)):
            # After an Explorer restart the old registration may still exist.
            native.shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))
        data.uVersion = 4
        native.shell32.Shell_NotifyIconW(_NIM_SETVERSION, ctypes.byref(data))

    def _icon_data(self, flags: int) -> native.NOTIFYICONDATAW:
        presentation = self._presentation
        data = native.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(native.NOTIFYICONDATAW),
            hWnd=self._hwnd,
            uID=_ICON_ID,
            uFlags=flags,
            uCallbackMessage=_WM_TRAY,
        )
        if flags & _NIF_ICON:
            self._icon_state = presentation.icon if presentation else "normal"
            self._icon_dpi = self._dpi()
            data.hIcon = self._icon(self._icon_state, native.SM_CXSMICON)
        if flags & _NIF_TIP:
            data.szTip = presentation.tooltip if presentation else "vBot"
        return data

    def _notify(self, message: int, flags: int) -> None:
        if self._hwnd:
            data = self._icon_data(flags)
            native.shell32.Shell_NotifyIconW(message, ctypes.byref(data))

    def _refresh_icon(self) -> None:
        self._notify(_NIM_MODIFY, _NIF_ICON)

    def _icon(self, state: str, metric: int) -> int:
        dpi = self._dpi()
        size = native.user32.GetSystemMetricsForDpi(metric, dpi) or (16 if metric == 49 else 32)
        key = (state, size)
        handle = self._icons.get(key)
        if handle is None:
            if self._base_image is None:
                from PIL import Image

                self._base_image = Image.open(self._icon_path)
            handle = native.icon_from_image(render_icon(self._base_image, state, size))
            self._icons[key] = handle
        return handle

    def _dpi(self) -> int:
        return int(native.user32.GetDpiForWindow(self._hwnd) or 96) if self._hwnd else 96

    def _apply(self, presentation: TrayPresentation) -> None:
        previous, self._presentation = self._presentation, presentation
        flags = 0
        if presentation.icon != self._icon_state or self._icon_dpi != self._dpi():
            flags |= _NIF_ICON
        if previous is None or previous.tooltip != presentation.tooltip:
            flags |= _NIF_TIP | _NIF_SHOWTIP
        if flags:
            self._notify(_NIM_MODIFY, flags)
        if self._status is not None and (
            previous is None or previous.status != presentation.status
        ):
            self._status.update(presentation.status)

    # Menu.

    def _track_menu(self, x: int, y: int) -> None:
        presentation = self._presentation
        if presentation is None or self._menu_open:
            return
        menu = native.user32.CreatePopupMenu()
        brush = native.gdi32.CreateSolidBrush(native.palette().background)
        self._menu_labels = {}
        try:
            for index, item in enumerate(presentation.menu, start=1):
                self._menu_labels[index] = item
                flags = _MF_OWNERDRAW
                if item.separator:
                    flags |= _MF_SEPARATOR
                elif not item.enabled or item.action is None:
                    flags |= _MF_GRAYED
                native.user32.AppendMenuW(menu, flags, index, index)
                if item.default and item.enabled:
                    native.user32.SetMenuDefaultItem(menu, index, False)
            info = native.MENUINFO(
                cbSize=ctypes.sizeof(native.MENUINFO),
                fMask=0x12,  # MIM_BACKGROUND | MIM_STYLE
                dwStyle=0x80000000,  # MNS_NOCHECK
                hbrBack=brush,
            )
            native.user32.SetMenuInfo(menu, ctypes.byref(info))
            # The hidden owner follows the tray's monitor so the popup measures
            # itself with that monitor's DPI.
            native.user32.SetWindowPos(
                self._hwnd,
                None,
                x,
                y,
                0,
                0,
                native.SWP_NOSIZE | native.SWP_NOZORDER | native.SWP_NOACTIVATE,
            )
            native.user32.SetForegroundWindow(self._hwnd)
            native.user32.SetCursor(native.user32.LoadCursorW(None, native.IDC_ARROW))
            self._menu_open = True
            command = native.user32.TrackPopupMenuEx(
                menu,
                _TPM_RIGHTBUTTON | _TPM_RIGHTALIGN | _TPM_BOTTOMALIGN | _TPM_RETURNCMD,
                x,
                y,
                self._hwnd,
                None,
            )
            # Lets the popup close when the user clicks elsewhere next time.
            native.user32.PostMessageW(self._hwnd, native.WM_NULL, 0, 0)
        finally:
            self._menu_open = False
            native.user32.DestroyMenu(menu)
            native.gdi32.DeleteObject(brush)
        if self._stopping:
            self._destroy()
            return
        chosen = self._menu_labels.get(int(command))
        if chosen is not None and chosen.action:
            native.user32.AllowSetForegroundWindow(_ASFW_ANY)
            self._commands.invoke(chosen.action)

    def _measure_item(self, lparam: int) -> int | None:
        measure = ctypes.cast(lparam, ctypes.POINTER(native.MEASUREITEMSTRUCT)).contents
        item = self._menu_labels.get(int(measure.itemData))
        if item is None:
            return None
        dpi = self._dpi()
        measure.itemWidth = native.scale(238, dpi)
        height = 9 if item.separator else 60 if "\n" in item.label else 32
        measure.itemHeight = native.scale(height, dpi)
        return 1

    def _draw_item(self, lparam: int) -> int | None:
        draw = ctypes.cast(lparam, ctypes.POINTER(native.DRAWITEMSTRUCT)).contents
        item = self._menu_labels.get(int(draw.itemData))
        if item is None:
            return None
        paint_menu_item(draw, item, self._dpi(), native.palette())
        return 1

    # Toasts.

    def _show_toast(self, toast: Toast) -> None:
        if not self._hwnd:
            return
        data = self._icon_data(_NIF_INFO)
        data.szInfoTitle = toast.title[:63]
        data.szInfo = toast.body[:255] or " "
        data.dwInfoFlags = _NIIF_USER | _NIIF_LARGE_ICON | _NIIF_RESPECT_QUIET_TIME
        data.hBalloonIcon = self._icon("error" if toast.failure else "normal", native.SM_CXICON)
        if native.shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data)):
            self._toast = toast
            self._hook_foreground()

    def _dismiss_toast(self, key: str) -> None:
        if self._toast is None or self._toast.key != key:
            return
        self._hide_toast()

    def _hide_toast(self) -> None:
        # An empty text removes the current balloon (documented NIF_INFO use).
        self._notify(_NIM_MODIFY, _NIF_INFO)
        self._toast_closed()

    def _toast_closed(self) -> None:
        self._toast = None
        self._unhook_foreground()

    def _hook_foreground(self) -> None:
        if not self._foreground_hook:
            self._foreground_hook = int(
                native.user32.SetWinEventHook(
                    _EVENT_SYSTEM_FOREGROUND,
                    _EVENT_SYSTEM_FOREGROUND,
                    None,
                    self._foreground_procedure,
                    0,
                    0,
                    _WINEVENT_SKIPOWNPROCESS,
                )
                or 0
            )

    def _unhook_foreground(self) -> None:
        if self._foreground_hook:
            native.user32.UnhookWinEvent(self._foreground_hook)
            self._foreground_hook = 0

    def _on_foreground(self, _hook, _event, hwnd, _object, _child, _thread, _time) -> None:
        # Switching to the vBot Desktop window shows the news already.
        try:
            if self._toast is not None and _executable_name(hwnd) == _DESKTOP_EXECUTABLE:
                self._hide_toast()
        except Exception:
            _LOGGER.exception("Foreground change handling failed")

    # Status window.

    def _open_status(self) -> None:
        from cli.application.windows_status import StatusWindow

        if self._status is None:
            self._status = StatusWindow(
                self._commands.invoke,
                lambda: self._icon("normal", native.SM_CXICON),
                lambda: self._icon("normal", native.SM_CXSMICON),
            )
        presentation = self._presentation
        if presentation is not None:
            self._status.show(presentation.status)


def paint_menu_item(
    draw: native.DRAWITEMSTRUCT, item: TrayMenuItem, dpi: int, colors: native.Palette
) -> None:
    """Paint one owner-drawn menu row with explicit theme colors."""

    selected = bool(draw.itemState & _ODS_SELECTED) and not draw.itemState & _ODS_DISABLED
    disabled = bool(draw.itemState & _ODS_DISABLED)
    rect = wintypes.RECT.from_buffer_copy(draw.rcItem)
    native.fill(draw.hDC, rect, colors.background)
    if item.separator:
        line = wintypes.RECT(
            rect.left + native.scale(12, dpi),
            (rect.top + rect.bottom) // 2,
            rect.right - native.scale(12, dpi),
            (rect.top + rect.bottom) // 2 + 1,
        )
        native.fill(draw.hDC, line, colors.separator)
        return
    if selected:
        inset = native.scale(4, dpi)
        native.round_fill(
            draw.hDC,
            wintypes.RECT(rect.left + inset, rect.top + 1, rect.right - inset, rect.bottom - 1),
            colors.hover,
            native.scale(8, dpi),
        )
    rect.left += native.scale(14, dpi)
    rect.right -= native.scale(14, dpi)
    native.gdi32.SetBkMode(draw.hDC, 1)  # TRANSPARENT
    flags = native.DT_SINGLELINE | native.DT_VCENTER | native.DT_END_ELLIPSIS
    if "\n" in item.label:
        title, subtitle = item.label.split("\n", 1)
        first = wintypes.RECT(
            rect.left, rect.top + native.scale(6, dpi), rect.right, rect.top + native.scale(31, dpi)
        )
        second = wintypes.RECT(
            rect.left, first.bottom, rect.right, rect.bottom - native.scale(6, dpi)
        )
        _draw_with_font(draw.hDC, first, title, dpi, 10, 600, colors.foreground, flags)
        _draw_with_font(draw.hDC, second, subtitle, dpi, 9, 400, colors.secondary, flags)
        return
    color = colors.secondary if disabled else colors.foreground
    weight = 600 if item.default and not disabled else 400
    _draw_with_font(draw.hDC, rect, item.label, dpi, 10, weight, color, flags)


def _draw_with_font(
    dc: int,
    rect: wintypes.RECT,
    text: str,
    dpi: int,
    points: float,
    weight: int,
    color: int,
    flags: int,
) -> None:
    font = native.create_font(dpi, points, weight=weight)
    try:
        native.draw_text(dc, rect, text, font, color, flags)
    finally:
        native.gdi32.DeleteObject(font)


def render_icon(base: Any, state: str, size: int) -> Any:
    """Return the application icon at ``size`` with the badge for ``state``.

    ``normal`` has no badge; ``stopped``, ``updating`` and ``error`` add a
    colored dot, separated from the logo by a transparent ring.
    """

    from PIL import Image, ImageChops, ImageDraw

    image = _icon_frame(base, size)
    color = _BADGES.get(state)
    if color is None:
        return image
    supersample = 4
    canvas = size * supersample
    diameter = round(canvas * 0.52)
    ring = max(supersample, round(canvas * 0.08))
    start = canvas - diameter
    cutout = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(cutout).ellipse(
        (start - ring, start - ring, canvas + ring, canvas + ring), fill=255
    )
    alpha = ImageChops.subtract(
        image.getchannel("A"), cutout.resize((size, size), Image.Resampling.LANCZOS)
    )
    image.putalpha(alpha)
    badge = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    ImageDraw.Draw(badge).ellipse((start, start, canvas - 1, canvas - 1), fill=color)
    image.alpha_composite(badge.resize((size, size), Image.Resampling.LANCZOS))
    return image


def _icon_frame(base: Any, size: int) -> Any:
    from PIL import Image

    sizes = sorted(base.info.get("sizes", ()) or ())
    source = base
    candidates = [candidate for candidate in sizes if candidate[0] >= size]
    if candidates and hasattr(base, "ico"):
        source = base.ico.getimage(candidates[0])
    image = source.convert("RGBA")
    if image.size != (size, size):
        image = image.resize((size, size), Image.Resampling.LANCZOS)
    return image


def _executable_name(hwnd: int) -> str:
    process_id = wintypes.DWORD()
    native.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return ""
    handle = native.kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value
    )
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not native.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return ""
        return os.path.basename(buffer.value).lower()
    finally:
        native.kernel32.CloseHandle(handle)
