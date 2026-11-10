"""The desktop a Computer Use call acts on, independent of its platform.

The Tools, access checks and imaging use only this protocol. A target owns the
operating-system boundary: displays, screenshots, windows, applications and
input. Every coordinate here is a physical virtual-desktop pixel; origins may
be negative. All methods block and run on the service's single desktop worker
thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image

BUTTONS = ("left", "right", "middle")
SCROLL_DIRECTIONS = ("up", "down", "left", "right")
CATEGORIES = ("browser", "terminal", "ide", "shell", "other")


class TargetError(Exception):
    """An expected desktop failure; ``code`` and message are Model-facing."""

    def __init__(self, message: str, code: str = "computer_use_failed") -> None:
        super().__init__(message)
        self.code = code


class InputInterrupted(TargetError):  # noqa: N818 - a stop, not an error
    """A stop request ended input; held keys and buttons were released."""

    def __init__(self) -> None:
        super().__init__("Computer Use was stopped.", "computer_use_interrupted")


@dataclass(frozen=True)
class Display:
    id: str
    name: str
    left: int
    top: int
    width: int
    height: int
    primary: bool
    scale_percent: int

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.left + self.width and self.top <= y < self.top + self.height


@dataclass(frozen=True)
class AppInfo:
    """An application as the user knows it; ``keys`` match its windows."""

    name: str
    keys: frozenset[str]
    running: bool
    launchable: bool

    def matches(self, other: AppInfo) -> bool:
        return bool(self.keys & other.keys)


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    app: AppInfo
    left: int
    top: int
    right: int
    bottom: int
    elevated: bool
    owner: int = 0
    shell: bool = False
    process_id: int = 0

    def same_window(self, other: WindowInfo) -> bool:
        return (self.handle, self.process_id, self.app.keys) == (
            other.handle,
            other.process_id,
            other.app.keys,
        )

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass(frozen=True)
class DesktopSnapshot:
    displays: tuple[Display, ...]
    windows: tuple[WindowInfo, ...]
    foreground: WindowInfo | None
    receivers: tuple[WindowInfo | None, ...] = ()

    def same_layout(self, other: DesktopSnapshot) -> bool:
        def identity(window: WindowInfo | None) -> object:
            if window is None:
                return None
            return (
                window.handle,
                window.process_id,
                window.app.keys,
                window.left,
                window.top,
                window.right,
                window.bottom,
                window.owner,
                window.elevated,
                window.shell,
            )

        return (
            self.displays == other.displays
            and identity(self.foreground) == identity(other.foreground)
            and tuple(map(identity, self.windows)) == tuple(map(identity, other.windows))
        )


class CaptureChangedError(TargetError):
    def __init__(self, message: str = "The screen changed during capture.") -> None:
        super().__init__(message, "screen_changed")


class DesktopTarget(Protocol):
    def activate(self, window: WindowInfo) -> None:
        """Activate this exact window; refuse a closed/replaced window or blocking modal."""
        ...

    def inspect(self, window: WindowInfo) -> dict[str, Any]:
        """Read bounded accessibility state for this window, without a screenshot."""
        ...

    def element(
        self, window: WindowInfo, reference: dict[str, Any], action: str, text: str
    ) -> dict[str, Any]:
        """Resolve an observed element or perform one of its advertised actions."""
        ...

    def snapshot(self, points: list[tuple[int, int]]) -> DesktopSnapshot:
        """Read display geometry, z-order, focus and all input receivers together."""
        ...

    def capture_region(
        self,
        area: tuple[int, int, int, int],
        snapshot: DesktopSnapshot,
        visible: list[int],
        size: tuple[int, int],
    ) -> Image.Image:
        """Verify the snapshot around capture; mask before resizing on the native host."""
        ...

    def readiness(self) -> str | None:
        """``None`` when usable, else an English reason for the readiness hint."""
        ...

    def displays(self) -> list[Display]: ...

    def capture(self, display: Display) -> Image.Image:
        """Physical RGB pixels of *display* in the colours applications drew.

        A colour filter that desktop composition applied (for example f.lux) is
        removed only when the target proves it; otherwise pixels are as captured.
        """
        ...

    def windows(self) -> list[WindowInfo]:
        """Visible, non-minimized, non-cloaked top-level windows, topmost first.

        Includes the shell's taskbar and desktop windows as app "File Explorer".
        """
        ...

    def foreground(self) -> WindowInfo | None: ...

    def window_at(self, x: int, y: int) -> WindowInfo | None: ...

    def apps(self) -> list[AppInfo]:
        """Installed and running windowed applications; cached briefly."""
        ...

    def open(self, app: AppInfo) -> bool:
        """Bring a window of *app* to the front, launching the app if it has none.

        ``True`` when the app was launched, ``False`` when an open window came to the front.
        """
        ...

    def cursor(self) -> tuple[int, int]: ...

    def move(self, x: int, y: int) -> None: ...

    def button(self, button: str, down: bool) -> None: ...

    def click(self, x: int, y: int, button: str, count: int, modifiers: list[str]) -> None: ...

    def drag(self, path: list[tuple[int, int]], seconds: float, modifiers: list[str]) -> None:
        """Press the left button at the first point and move through the rest in *seconds*."""
        ...

    def scroll(self, x: int, y: int, direction: str, amount: int, modifiers: list[str]) -> None: ...

    def keys(self, chord: list[str], repeat: int) -> None:
        """Press *chord* (canonical names from ``_keys``) *repeat* times."""
        ...

    def start_hold(self, chord: list[str]) -> None: ...

    def end_hold(self) -> None: ...

    def hold(self, chord: list[str], seconds: float) -> None: ...

    def type_text(self, text: str) -> None:
        """Type Unicode text; newline presses Enter and tab presses Tab."""
        ...

    def release_all(self) -> None:
        """Release every key and button this target pressed."""
        ...

    def set_stop_event(self, event: threading.Event) -> None:
        """Input checks *event* between events and raises ``InputInterrupted``."""
        ...

    def set_activity(self, active: bool) -> None:
        """Show or hide the on-screen sign that an Agent controls the computer.

        Quick and safe from any thread; captures never contain the sign.
        """
        ...

    def close(self) -> None:
        """Remove the activity sign and release what the target holds besides input."""
        ...
