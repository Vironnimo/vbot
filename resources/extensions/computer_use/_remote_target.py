"""A DesktopTarget whose native operations belong to a connected Desktop app."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PIL import Image

from ._remote_wire import (
    app_data,
    app_value,
    display_data,
    display_value,
    image_value,
    window_value,
)
from .target import AppInfo, Display, InputInterrupted, TargetError, WindowInfo

if TYPE_CHECKING:
    from core.computers import ComputerInfo, ComputerRegistry

_LOGGER = logging.getLogger("vbot.extensions.computer_use")


class RemoteDesktopTarget:
    """Native input stays on the Desktop; the Extension keeps policy and images.

    Blocking target operations run on the existing Computer Use worker. Activity
    and hotkey controls are also called on the Event Loop, so those are queued.
    One lock preserves their order with the worker's subsequent requests.
    """

    def __init__(self, computers: ComputerRegistry, computer: ComputerInfo) -> None:
        self.computers = computers
        self.computer_id = computer.id
        self.instance_id = computer.instance_id
        self.name = computer.name
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = threading.Event()
        self._closed = False
        self._control_error: TargetError | None = None
        self.hotkey_available = False
        self.on_control_changed: Callable[[], None] | None = None

    async def _send(
        self, operation: str, params: dict[str, Any], *, ordered: bool = True
    ) -> dict[str, Any]:
        # Typing is paced by the native keyboard, and hold/drag can last 100 s.
        timeout = max(30.0, float(params.get("seconds", 0)) + 15)
        if operation == "type":
            timeout = max(timeout, len(str(params.get("text", ""))) * 0.06 + 15)

        async def perform() -> dict[str, Any]:
            try:
                return await self.computers.call(
                    self.computer_id,
                    "computer_use",
                    operation,
                    params,
                    timeout=timeout,
                    instance_id=self.instance_id,
                )
            except Exception as error:
                code = getattr(error, "code", "computer_use_failed")
                if code == "computer_use_interrupted":
                    raise InputInterrupted() from error
                raise TargetError(str(error), str(code)) from error

        if not ordered:
            return await perform()
        async with self._lock:
            if self._control_error is not None and operation not in {
                "readiness",
                "release",
                "control",
                "begin",
                "activity",
            }:
                raise self._control_error
            return await perform()

    def _call(self, operation: str, **params: Any) -> dict[str, Any]:
        if self._closed:
            raise TargetError("This desktop connection has ended.", "computer_offline")
        future = asyncio.run_coroutine_threadsafe(self._send(operation, params), self._loop)
        return future.result()

    def _queue(self, operation: str, params: dict[str, Any], *, ordered: bool = True) -> None:
        async def send() -> None:
            try:
                result = await self._send(operation, params, ordered=ordered)
                if operation == "control":
                    self._control_error = None
                    self.set_hotkey_available(result.get("hotkey_available") is True)
            except TargetError as error:
                if operation in {"control", "begin", "activity"}:
                    self._control_error = error
                # A disconnection already stops input locally and through the router.
                _LOGGER.debug("Remote desktop control was unavailable (operation=%s)", operation)

        def start() -> None:
            if self._closed:
                return
            task = asyncio.create_task(send())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        self._loop.call_soon_threadsafe(start)

    async def drain(self) -> None:
        await asyncio.sleep(0)
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    def readiness(self) -> str | None:
        reason = self._call("readiness").get("reason")
        return str(reason) if reason is not None else None

    def displays(self) -> list[Display]:
        return [display_value(item) for item in self._call("displays")["value"]]

    def capture(self, display: Display) -> Image.Image:
        return image_value(self._call("capture", display=display_data(display)))

    def windows(self) -> list[WindowInfo]:
        windows = [window_value(item) for item in self._call("windows")["value"]]
        return [item for item in windows if item is not None]

    def foreground(self) -> WindowInfo | None:
        return window_value(self._call("foreground")["value"])

    def window_at(self, x: int, y: int) -> WindowInfo | None:
        return window_value(self._call("window_at", x=x, y=y)["value"])

    def apps(self) -> list[AppInfo]:
        return [app_value(item) for item in self._call("apps")["value"]]

    def open(self, app: AppInfo) -> bool:
        return bool(self._call("open", app=app_data(app))["value"])

    def cursor(self) -> tuple[int, int]:
        x, y = self._call("cursor")["value"]
        return int(x), int(y)

    def move(self, x: int, y: int) -> None:
        self._call("move", x=x, y=y)

    def button(self, button: str, down: bool) -> None:
        self._call("button", button=button, down=down)

    def click(self, x: int, y: int, button: str, count: int, modifiers: list[str]) -> None:
        self._call("click", x=x, y=y, button=button, count=count, modifiers=modifiers)

    def drag(self, path: list[tuple[int, int]], seconds: float, modifiers: list[str]) -> None:
        self._call(
            "drag", path=[list(point) for point in path], seconds=seconds, modifiers=modifiers
        )

    def scroll(self, x: int, y: int, direction: str, amount: int, modifiers: list[str]) -> None:
        self._call("scroll", x=x, y=y, direction=direction, amount=amount, modifiers=modifiers)

    def keys(self, chord: list[str], repeat: int) -> None:
        self._call("keys", chord=chord, repeat=repeat)

    def hold(self, chord: list[str], seconds: float) -> None:
        self._call("hold", chord=chord, seconds=seconds)

    def type_text(self, text: str) -> None:
        self._call("type", text=text)

    def release_all(self) -> None:
        self._call("release")

    def set_stop_event(self, event: threading.Event) -> None:
        self._stop = event

    def set_activity(self, active: bool) -> None:
        self._queue("activity", {"active": active})

    def control(self, owner: object | None) -> None:
        self._queue("control", {"control_id": str(owner) if owner is not None else None})

    def begin(self) -> None:
        self._queue("begin", {})

    def interrupt(self) -> None:
        # Stop reaches the native Event Loop while its worker is still typing.
        self._queue("stop", {}, ordered=False)

    def set_hotkey_available(self, available: bool) -> None:
        if available != self.hotkey_available:
            self.hotkey_available = available
            if self.on_control_changed is not None:
                self.on_control_changed()

    def close(self) -> None:
        self._closed = True


class RemoteHotkey:
    """Arming follows the remote control id; native events reach the owning service."""

    def __init__(self, target: RemoteDesktopTarget, callback: Callable[[object], None]) -> None:
        self.target = target
        self.callback = callback

    @property
    def available(self) -> bool:
        return self.target.hotkey_available

    def start(self) -> None:
        pass

    def set_armed(self, owner: object | None) -> None:
        self.target.control(owner)

    def close(self) -> None:
        self.target.control(None)
