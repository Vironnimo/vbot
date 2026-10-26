"""The activity overlay: a glowing frame on every display while an Agent controls the computer.

Four thin layered windows per monitor draw an accent glow along its edges, and a
pill at the top of the primary monitor names vBot and the emergency stop. Every
window passes clicks through, never activates, stays out of Alt+Tab and of the
target's window listing (layered and click-through, see ``_invisible_overlay``),
and is excluded from screen capture, so the Agent's screenshots never show it.
One daemon thread owns the windows and their message loop: ``show`` and ``hide``
only record the wanted state and wake it, and it fades towards that state.
"""

from __future__ import annotations

import ctypes as ct
import logging
import sys
import threading
import time
from collections.abc import Sequence
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import _win32
from .target import TargetError

_LOGGER = logging.getLogger("vbot.extensions.computer_use")

ACCENT = (232, 135, 10)  # the WebUI's --accent, #e8870a
LABEL, HINT = "vBot is using this computer", "Esc Esc to stop"
_PILL_FILL = (21, 19, 15, 224)  # the WebUI's --bg, #15130f, mostly opaque
_TEXT, _TEXT_DIM = (241, 237, 230), (179, 170, 158)  # --text-hi, --text-med
_LINE_ALPHA, _GLOW_ALPHA = 230, 120  # the edge line, and where the glow starts
_PILL_TOP = 10  # pixels between the display's top edge and the pill, at 100 % scale
_FADE_SECONDS, _FADE_STEP = 0.15, 1 / 60
_CLASS, _TITLE = "vBotActivityOverlay", "vBot activity"
_WS_POPUP, _WS_EX_TOPMOST = 0x80000000, 0x8
_EX_STYLE = (
    _win32.WS_EX_LAYERED
    | _win32.WS_EX_TRANSPARENT
    | _win32.WS_EX_TOOLWINDOW
    | _win32.WS_EX_NOACTIVATE
    | _WS_EX_TOPMOST
)
_SW_SHOWNOACTIVATE = 4
_HWND_TOPMOST = -1
_SWP_RAISE = 0x1 | 0x2 | 0x10 | 0x200  # NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER
_WDA_EXCLUDEFROMCAPTURE = 0x11
_WM_QUIT, _WM_MOUSEACTIVATE, _WM_DISPLAYCHANGE, _WM_DPICHANGED = 0x12, 0x21, 0x7E, 0x2E0
_MA_NOACTIVATE = 3
_WM_SYNC, _WM_RESTACK = 0x8001, 0x8002
# Window activation, show/reorder and move/resize (including maximize/fullscreen).
_EVENT_RANGES = ((0x3, 0x3), (0x8002, 0x8004), (0x800B, 0x800B))
_WINEVENT_SKIPOWNTHREAD = 0x1  # out of context; never react to our own restacking


class _Message(ct.Structure):
    _fields_ = [
        ("hwnd", ct.c_void_p),
        ("message", ct.c_uint),
        ("wparam", ct.c_size_t),
        ("lparam", ct.c_ssize_t),
        ("time", ct.c_uint32),
        ("point", _win32.Point),
        ("private", ct.c_uint32),
    ]


class _WindowClass(ct.Structure):
    _fields_ = [
        ("size", ct.c_uint),
        ("style", ct.c_uint),
        ("procedure", ct.c_void_p),
        ("class_extra", ct.c_int),
        ("window_extra", ct.c_int),
        ("instance", ct.c_void_p),
        ("icon", ct.c_void_p),
        ("cursor", ct.c_void_p),
        ("background", ct.c_void_p),
        ("menu_name", ct.c_wchar_p),
        ("class_name", ct.c_wchar_p),
        ("small_icon", ct.c_void_p),
    ]


class ActivityOverlay:
    """The frame that shows the user an Agent is controlling the computer (Windows only).

    ``show`` and ``hide`` are idempotent and thread-safe, and return at once: they
    record the wanted state and wake the overlay thread, which starts with the first
    ``show`` and fades in or out. While visible, native window events keep the frame
    above newly shown, activated or maximized windows without repainting it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wanted = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._exclude_from_capture = True  # off only to look at the frame while tuning it
        # Owned by the overlay thread.
        self._class_name = f"{_CLASS}_{id(self):x}"
        self._procedure: Any = None
        self._event_procedure: Any = None
        self._hooks: list[int] = []
        self._restack_pending = False
        self._windows: list[int] = []
        self._layout: tuple[_win32.Monitor, ...] = ()
        self._opacity = 0.0  # fade progress, 0 hidden to 1 shown
        self._warned: set[str] = set()

    def show(self) -> None:
        self._request(True)

    def hide(self) -> None:
        self._request(False)

    def close(self) -> None:
        """Destroy the windows and end the overlay thread; later calls do nothing."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._wanted = False
            thread = self._thread
        if thread is None:
            return
        self._post(_WM_QUIT)
        thread.join(timeout=2)
        if thread.is_alive():
            _LOGGER.warning("Computer Use activity overlay is still closing")

    def _request(self, wanted: bool) -> None:
        if sys.platform != "win32":
            return
        with self._lock:
            if self._closed:
                return
            self._wanted = wanted
            if self._thread is None or not self._thread.is_alive():
                if not wanted:
                    return
                self._thread = threading.Thread(
                    target=self._run, name="computer-use-overlay", daemon=True
                )
                self._thread.start()
        self._post(_WM_SYNC)

    def _post(self, message: int) -> None:
        if self._thread_id:
            _win32.api().user32.PostThreadMessageW(self._thread_id, message, 0, 0)

    def _showing(self) -> bool:
        with self._lock:
            return self._wanted and not self._closed

    # The overlay thread

    def _run(self) -> None:
        message = _Message()
        try:
            bound = _win32.api()
            _win32.enter_thread()
            # The first peek creates the thread's queue, so posted wake-ups arrive.
            bound.user32.PeekMessageW(ct.byref(message), None, 0, 0, 0)
            self._thread_id = int(bound.kernel32.GetCurrentThreadId())
            self._sync()  # a wake-up posted before the queue existed is lost
            while not self._closed:
                result = bound.user32.GetMessageW(ct.byref(message), None, 0, 0)
                if result == -1:
                    raise TargetError("Windows refused the activity overlay's message loop.")
                if result == 0:
                    break
                if not message.hwnd and message.message == _WM_SYNC:
                    self._sync()
                elif not message.hwnd and message.message == _WM_RESTACK:
                    self._restack_pending = False
                    if self._showing():
                        self._raise_if_covered()
                else:
                    bound.user32.DispatchMessageW(ct.byref(message))
        except (TargetError, OSError) as error:
            _LOGGER.warning("Computer Use activity overlay unavailable: %s", error)
        except Exception:
            _LOGGER.exception("Computer Use activity overlay failed")
        finally:
            self._destroy()
            self._thread_id = 0
            self._opacity = 0.0

    def _sync(self) -> None:
        """Follow the latest request: lay out and raise the frame when shown, then fade."""
        if self._showing():
            layout = tuple(sorted(_win32.monitors(), key=lambda item: (item.left, item.top)))
            if layout != self._layout or not self._windows:
                self._destroy()
                self._build(layout)
            self._watch()
            self._raise_if_covered()
        else:
            self._unwatch()
        self._fade()

    def _build(self, layout: tuple[_win32.Monitor, ...]) -> None:
        self._register_class()
        self._layout = layout
        for monitor in layout:
            width, height = monitor.right - monitor.left, monitor.bottom - monitor.top
            for left, top, image in frame_strips(width, height, glow_profile(monitor.dpi / 96)):
                self._add(monitor.left + left, monitor.top + top, image)
        primary = next((monitor for monitor in layout if monitor.primary), None)
        if primary is not None:
            scale = primary.dpi / 96
            pill = _pill(scale)
            left = primary.left + (primary.right - primary.left - pill.width) // 2
            self._add(left, primary.top + round(_PILL_TOP * scale), pill)

    def _add(self, left: int, top: int, image: Image.Image) -> None:
        bound = _win32.api()
        handle = int(
            bound.user32.CreateWindowExW(
                _EX_STYLE,
                self._class_name,
                _TITLE,
                _WS_POPUP,
                left,
                top,
                image.width,
                image.height,
                None,
                None,
                bound.kernel32.GetModuleHandleW(None),
                None,
            )
            or 0
        )
        if not handle:
            self._warn("create", "Windows refused an activity overlay window (error %d)")
            return
        self._windows.append(handle)
        # Excluded before it first shows, so no capture ever contains it.
        if self._exclude_from_capture and not bound.user32.SetWindowDisplayAffinity(
            handle, _WDA_EXCLUDEFROMCAPTURE
        ):
            self._warn("affinity", "The activity overlay may appear in screenshots (error %d)")
        alpha = self._alpha()
        if _win32.paint_layered(handle, left, top, image.width, image.height, _bgra(image), alpha):
            _win32.show_window(handle, _SW_SHOWNOACTIVATE)
        else:
            self._warn("paint", "Windows refused to draw the activity overlay (error %d)")

    def _fade(self) -> None:
        """Fade towards the latest request, turning around when it changes on the way."""
        step = _FADE_STEP / _FADE_SECONDS
        while self._windows:
            shown = self._showing()
            goal = 1.0 if shown else 0.0
            if self._opacity == goal:
                break
            self._opacity = (
                min(goal, self._opacity + step) if shown else max(0.0, self._opacity - step)
            )
            alpha = self._alpha()
            for handle in self._windows:
                _win32.set_layered_opacity(handle, alpha)
            time.sleep(_FADE_STEP)
        if not self._showing():
            self._destroy()

    def _alpha(self) -> int:
        progress = self._opacity
        return round(255 * progress * progress * (3 - 2 * progress))  # smoothstep

    def _destroy(self) -> None:
        self._unwatch()
        for handle in self._windows:
            if not _win32.api().user32.DestroyWindow(handle):
                self._warn("destroy", "Windows refused to remove an activity overlay (error %d)")
        self._windows.clear()
        self._layout = ()
        if self._procedure is not None:
            bound = _win32.api()
            if bound.user32.UnregisterClassW(
                self._class_name, bound.kernel32.GetModuleHandleW(None)
            ):
                self._procedure = None
            else:
                self._warn("class", "Windows refused to release the overlay class (error %d)")

    def _register_class(self) -> None:
        """Own the class and its callback together, including across Extension Reloads."""
        if self._procedure is not None:
            return
        bound = _win32.api()
        procedure = bound.window_proc(_window_procedure)
        info = _WindowClass(
            size=ct.sizeof(_WindowClass),
            procedure=ct.cast(procedure, ct.c_void_p).value,
            instance=bound.kernel32.GetModuleHandleW(None),
            class_name=self._class_name,
        )
        if not bound.user32.RegisterClassExW(ct.byref(info)):
            raise TargetError("Windows refused the activity overlay's window class.")
        self._procedure = procedure

    def _watch(self) -> None:
        if self._hooks:
            return
        bound = _win32.api()
        self._event_procedure = bound.event_proc(self._window_event)
        for first, last in _EVENT_RANGES:
            hook = bound.user32.SetWinEventHook(
                first, last, None, self._event_procedure, 0, 0, _WINEVENT_SKIPOWNTHREAD
            )
            if hook:
                self._hooks.append(int(hook))
            else:
                raise TargetError("Windows refused the activity overlay's window events.")

    def _unwatch(self) -> None:
        for hook in self._hooks:
            if not _win32.api().user32.UnhookWinEvent(hook):
                self._warn("unhook", "Windows refused to release an overlay event hook (error %d)")
        self._hooks.clear()
        self._event_procedure = None
        self._restack_pending = False

    def _window_event(
        self,
        _hook: int,
        event: int,
        handle: int,
        object_id: int,
        child_id: int,
        _thread: int,
        _time: int,
    ) -> None:
        # Z-order changes arrive on the desktop's client object, not the window
        # being raised. Otherwise only whole windows, never carets or controls.
        desktop_reorder = (
            event == 0x8004 and object_id == -4 and handle == _win32.api().user32.GetDesktopWindow()
        )
        if (
            not handle
            or (object_id != 0 and not desktop_reorder)
            or child_id != 0
            or handle in self._windows
            or not self._showing()
            or self._restack_pending
        ):
            return
        # Coalesce a burst and leave native callbacks before touching the z-order.
        self._restack_pending = bool(
            _win32.api().user32.PostThreadMessageW(self._thread_id, _WM_RESTACK, 0, 0)
        )

    def _raise_if_covered(self) -> None:
        remaining = set(self._windows)
        if not remaining:
            return
        for handle in _win32.top_level_windows():
            remaining.discard(handle)
            if not remaining:
                return  # already on top: no native mutation or repaint
            if handle in self._windows or not _win32.is_visible(handle):
                continue
            if _win32.is_cloaked(handle) or _win32.window_class(handle).startswith(_CLASS):
                continue  # another vBot instance's frame must not cause a raising loop
            for own in self._windows:  # the pill comes last and ends on top
                if not _win32.api().user32.SetWindowPos(own, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_RAISE):
                    self._warn("raise", "Windows refused to raise the activity overlay (error %d)")
            return

    def _warn(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            _LOGGER.warning(message, _win32.last_error())


# Drawing


def glow_profile(scale: float) -> list[int]:
    """Frame opacity (0-255) by pixel distance from the display edge, outermost first.

    A crisp line of two or more pixels at nearly full opacity, then a glow that fades
    to nothing over about 14 pixels; both grow with the display scale (1.0 = 100 %).
    """
    line, glow = max(2, round(2 * scale)), max(8, round(14 * scale))
    fade = [round(_GLOW_ALPHA * (1 - step / glow) ** 2) for step in range(glow)]
    return [_LINE_ALPHA] * line + fade


def frame_strips(
    width: int, height: int, profile: Sequence[int]
) -> list[tuple[int, int, Image.Image]]:
    """The top, bottom, left and right strips of a display's frame, at their offsets.

    The top and bottom strips span the full width and own the corners; a pixel takes
    the opacity of its distance to the nearest edge.
    """
    depth = min(len(profile), width // 2, height // 2)
    boxes = [
        (0, 0, width, depth),
        (0, height - depth, width, height),
        (0, depth, depth, height - depth),
        (width - depth, depth, width, height - depth),
    ]
    strips = []
    for x0, y0, x1, y1 in boxes:
        if x1 <= x0 or y1 <= y0:
            continue
        size = (x1 - x0, y1 - y0)
        columns = Image.new("L", (size[0], 1))
        columns.putdata(_edge_alphas(width, x0, x1, profile))
        rows = Image.new("L", (1, size[1]))
        rows.putdata(_edge_alphas(height, y0, y1, profile))
        nearest = Image.Resampling.NEAREST
        image = Image.new("RGBA", size, ACCENT)
        image.putalpha(
            ImageChops.lighter(columns.resize(size, nearest), rows.resize(size, nearest))
        )
        strips.append((x0, y0, image))
    return strips


def _edge_alphas(length: int, start: int, stop: int, profile: Sequence[int]) -> list[int]:
    """Opacity at positions *start* to *stop* of a span, by distance to its nearer end."""

    def at(distance: int) -> int:
        return profile[distance] if distance < len(profile) else 0

    return [max(at(position), at(length - 1 - position)) for position in range(start, stop)]


def _pill(scale: float) -> Image.Image:
    """The label pill: an accent dot and border, the name and the stop hint on dark."""
    size = round(13 * scale)
    label_font, hint_font = _font("seguisb.ttf", size), _font("segoeui.ttf", size)
    hint = f"  ·  {HINT}"
    pad, dot, gap = round(14 * scale), round(8 * scale), round(9 * scale)
    label_width = round(label_font.getlength(LABEL))
    width = pad + dot + gap + label_width + round(hint_font.getlength(hint)) + pad
    height = round(30 * scale)
    # The shape is drawn four times larger and reduced, which smooths its edges.
    big, border = 4, 4 * max(1, round(scale))
    shape = Image.new("RGBA", (width * big, height * big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shape)
    right, bottom = width * big - 1, height * big - 1
    draw.rounded_rectangle((0, 0, right, bottom), height * big // 2, fill=(*ACCENT, 170))
    draw.rounded_rectangle(
        (border, border, right - border, bottom - border),
        height * big // 2 - border,
        fill=_PILL_FILL,
    )
    middle = height * big // 2
    draw.ellipse(
        (pad * big, middle - dot * big // 2, (pad + dot) * big, middle + dot * big // 2),
        fill=(*ACCENT, 255),
    )
    pill = shape.resize((width, height), Image.Resampling.LANCZOS)
    text_left = pad + dot + gap
    for left, text, font, color in (
        (text_left, LABEL, label_font, _TEXT),
        (text_left + label_width, hint, hint_font, _TEXT_DIM),
    ):
        mask = Image.new("L", pill.size, 0)
        ImageDraw.Draw(mask).text((left, height / 2), text, fill=255, font=font, anchor="lm")
        ink = Image.new("RGBA", pill.size, color)
        ink.putalpha(mask)
        pill = Image.alpha_composite(pill, ink)
    return pill


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default(size)


def _bgra(image: Image.Image) -> bytes:
    """Premultiplied BGRA rows of an RGBA image, as layered windows take them."""
    red, green, blue, alpha = image.convert("RGBa").split()
    return Image.merge("RGBA", (blue, green, red, alpha)).tobytes()


def _window_procedure(handle: int | None, message: int, wparam: int, lparam: int) -> int:
    bound = _win32.api()
    if message == _WM_MOUSEACTIVATE:
        return _MA_NOACTIVATE
    if message in (_WM_DISPLAYCHANGE, _WM_DPICHANGED):
        # Displays changed: the owning thread lays the frame out again from its loop.
        thread = bound.kernel32.GetCurrentThreadId()
        bound.user32.PostThreadMessageW(thread, _WM_SYNC, 0, 0)
        return 0
    return int(bound.user32.DefWindowProcW(handle, message, wparam, lparam))
