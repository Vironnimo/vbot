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
from typing import Protocol

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

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


class DesktopTarget(Protocol):
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

    def open(self, app: AppInfo) -> None:
        """Bring a window of *app* to the front, launching the app if needed."""
        ...

    def cursor(self) -> tuple[int, int]: ...

    def move(self, x: int, y: int) -> None: ...

    def button(self, button: str, down: bool) -> None: ...

    def click(self, x: int, y: int, button: str, count: int, modifiers: list[str]) -> None: ...

    def drag(self, start: tuple[int, int], end: tuple[int, int], modifiers: list[str]) -> None: ...

    def scroll(self, x: int, y: int, direction: str, amount: int, modifiers: list[str]) -> None: ...

    def keys(self, chord: list[str], repeat: int) -> None:
        """Press *chord* (canonical names from ``_keys``) *repeat* times."""
        ...

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
