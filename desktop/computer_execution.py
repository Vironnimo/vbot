"""Computer Use supplied by the running Desktop, with no server-runtime imports."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from resources.extensions.computer_use._remote_wire import (
    app_data,
    app_value,
    display_data,
    display_value,
    image_data,
    window_data,
)
from resources.extensions.computer_use.target import DesktopTarget, InputInterrupted, TargetError

_READ_OPERATIONS = frozenset(
    {"readiness", "displays", "windows", "foreground", "window_at", "apps", "cursor", "capture"}
)


class _Hotkey(Protocol):
    available: bool

    def start(self) -> None: ...
    def set_armed(self, owner: object | None) -> None: ...
    def close(self) -> None: ...


def _new_target() -> DesktopTarget:
    if sys.platform != "win32":
        raise TargetError(
            "Computer Use needs a signed-in Windows desktop.", "computer_use_unavailable"
        )
    from resources.extensions.computer_use.windows_target import WindowsTarget

    return WindowsTarget()


def _new_hotkey(callback: Callable[[object], None]) -> _Hotkey:
    from resources.extensions.computer_use.hotkey import EmergencyHotkey

    return EmergencyHotkey(callback)


class ComputerExecution:
    """One native desktop, serialized and fenced against old connection requests.

    The connection owns this object for the Desktop process's lifetime. It calls
    ``disconnected`` before accepting another connection and ``close`` on exit.
    The native stop also works when the server cannot receive its notification.
    """

    def __init__(
        self,
        emit: Callable[[str, dict[str, Any]], None],
        *,
        target_factory: Callable[[], DesktopTarget] = _new_target,
        hotkey_factory: Callable[[Callable[[object], None]], _Hotkey] = _new_hotkey,
    ) -> None:
        self._emit = emit
        self._target_factory = target_factory
        self._hotkey_factory = hotkey_factory
        self._target: DesktopTarget | None = None
        self._hotkey: _Hotkey | None = None
        self._worker: ThreadPoolExecutor | None = None
        self._lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._generation = 0
        self._owner: str | None = None
        self._stopped_owners: dict[str, None] = {}
        self._closed = False

    async def _work[T](self, function: Callable[..., T], *args: Any) -> T:
        if self._worker is None:
            self._worker = ThreadPoolExecutor(1, thread_name_prefix="desktop-computer")
        future = asyncio.get_running_loop().run_in_executor(
            self._worker, functools.partial(function, *args)
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self._stop.set()
            # Do not release serialization while native input is still running.
            with contextlib.suppress(Exception):
                await asyncio.shield(future)
            raise

    def _ensure_target(self) -> DesktopTarget:
        if self._target is None:
            target = self._target_factory()
            target.set_stop_event(self._stop)
            self._target = target
        return self._target

    async def ready(self) -> str | None:
        if self._closed:
            return "The Desktop app has stopped."
        async with self._lock:
            try:
                return await self._work(lambda: self._ensure_target().readiness())
            except TargetError as error:
                return str(error)

    async def execute(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        generation = self._generation
        if operation == "stop":
            self._stop.set()
            async with self._lock:
                if self._target is not None:
                    await self._work(self._target.release_all)
            return {}
        async with self._lock:
            if self._closed or generation != self._generation:
                raise TargetError(
                    "The desktop connection ended before this operation started. Nothing was done.",
                    "computer_offline",
                )
            return await self._work(self._execute, operation, params, generation)

    def _execute(self, operation: str, params: dict[str, Any], generation: int) -> dict[str, Any]:
        with self._state_lock:
            if self._closed or generation != self._generation:
                raise InputInterrupted()
        target = self._ensure_target()
        if operation == "begin":
            with self._state_lock:
                if self._owner is not None and self._owner not in self._stopped_owners:
                    self._stop.clear()
            return {}
        if operation == "control":
            owner = params.get("control_id")
            if owner is not None and (not isinstance(owner, str) or not owner):
                raise TargetError("A desktop control id is required.", "invalid_arguments")
            with self._state_lock:
                if owner in self._stopped_owners:
                    raise InputInterrupted()
                if owner is not None:
                    self._stop.clear()
            if owner is not None and self._hotkey is None:
                hotkey = self._hotkey_factory(self._stop_from_hotkey)
                try:
                    # Startup can wait on the native hook. Keep disconnect and
                    # shutdown free to invalidate it without blocking their loop.
                    hotkey.start()
                    with self._state_lock:
                        if self._closed or generation != self._generation or self._stop.is_set():
                            raise InputInterrupted()
                        self._hotkey = hotkey
                except BaseException:
                    hotkey.close()
                    raise
            with self._state_lock:
                if (
                    self._closed
                    or generation != self._generation
                    or (owner is not None and self._stop.is_set())
                ):
                    raise InputInterrupted()
                self._owner = owner
                if self._hotkey is not None:
                    self._hotkey.set_armed(owner)
                if owner is None:
                    target.release_all()
                    target.set_activity(False)
            return {"hotkey_available": bool(self._hotkey and self._hotkey.available)}
        if operation == "activity":
            with self._state_lock:
                active = (
                    params.get("active") is True
                    and self._owner is not None
                    and not self._stop.is_set()
                )
            target.set_activity(active)
            return {}
        if operation == "release":
            target.release_all()
            return {}
        if operation not in _READ_OPERATIONS:
            with self._state_lock:
                if self._stop.is_set() or self._owner is None:
                    raise InputInterrupted()
        if operation == "readiness":
            return {"reason": target.readiness()}
        if operation == "displays":
            return {"value": [display_data(item) for item in target.displays()]}
        if operation == "windows":
            return {"value": [window_data(item) for item in target.windows()]}
        if operation == "foreground":
            return {"value": window_data(target.foreground())}
        if operation == "window_at":
            return {
                "value": window_data(target.window_at(_integer(params, "x"), _integer(params, "y")))
            }
        if operation == "apps":
            return {"value": [app_data(item) for item in target.apps()]}
        if operation == "cursor":
            return {"value": list(target.cursor())}
        if operation == "capture":
            display = display_value(params["display"])
            if display not in target.displays():
                raise TargetError(
                    "The display layout changed. Take a new screenshot.", "screen_changed"
                )
            return image_data(target.capture(display))
        if operation == "open":
            requested = app_value(params["app"])
            app = next((item for item in target.apps() if item.keys == requested.keys), None)
            if app is None:
                raise TargetError(
                    "The selected app is no longer available. List apps again.", "app_not_found"
                )
            return {"value": target.open(app)}
        if operation == "move":
            target.move(_integer(params, "x"), _integer(params, "y"))
        elif operation == "button":
            button = _button(params)
            down = params.get("down")
            if not isinstance(down, bool):
                raise TargetError("down must be a boolean.", "invalid_arguments")
            target.button(button, down)
        elif operation == "click":
            target.click(
                _integer(params, "x"),
                _integer(params, "y"),
                _button(params),
                _integer(params, "count", 1, 3),
                _keys(params, "modifiers"),
            )
        elif operation == "drag":
            points = params.get("path")
            if not isinstance(points, list) or not 2 <= len(points) <= 200:
                raise TargetError("A drag needs 2-200 points.", "invalid_arguments")
            path = [
                (_integer({"x": point[0]}, "x"), _integer({"y": point[1]}, "y"))
                for point in points
                if isinstance(point, list) and len(point) == 2
            ]
            if len(path) != len(points):
                raise TargetError("Each drag point needs x and y.", "invalid_arguments")
            target.drag(path, _seconds(params), _keys(params, "modifiers"))
        elif operation == "scroll":
            direction = params.get("direction")
            if direction not in {"up", "down", "left", "right"}:
                raise TargetError("Unknown scroll direction.", "invalid_arguments")
            target.scroll(
                _integer(params, "x"),
                _integer(params, "y"),
                direction,
                _integer(params, "amount", 1, 100),
                _keys(params, "modifiers"),
            )
        elif operation == "keys":
            target.keys(_keys(params, "chord"), _integer(params, "repeat", 1, 100))
        elif operation == "hold":
            target.hold(_keys(params, "chord"), _seconds(params))
        elif operation == "type":
            text = params.get("text")
            if not isinstance(text, str):
                raise TargetError("Text to type must be a string.", "invalid_arguments")
            target.type_text(text)
        else:
            raise TargetError(
                "Unknown Computer Use operation. Nothing was done.", "invalid_arguments"
            )
        return {}

    def _stop_from_hotkey(self, owner: object) -> None:
        with self._state_lock:
            if owner != self._owner or self._owner is None:
                return
            control_id = self._owner
            self._stopped_owners[control_id] = None
            while len(self._stopped_owners) > 128:
                del self._stopped_owners[next(iter(self._stopped_owners))]
            self._stop.set()
            self._owner = None
            if self._hotkey is not None:
                self._hotkey.set_armed(None)
            target = self._target
            if target is not None:
                target.set_activity(False)
            if self._worker is not None and target is not None:
                self._worker.submit(target.release_all)
        self._emit("stopped", {"control_id": control_id, "source": "double_escape"})

    async def disconnected(self) -> None:
        with self._state_lock:
            self._generation += 1
            self._stop.set()
            self._owner = None
            if self._hotkey is not None:
                self._hotkey.set_armed(None)
            if self._target is not None:
                self._target.set_activity(False)
        async with self._lock:
            if self._target is not None:
                await self._work(self._target.release_all)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.disconnected()
        if self._hotkey is not None:
            self._hotkey.close()
        async with self._lock:
            if self._target is not None:
                await self._work(self._target.close)
        if self._worker is not None:
            self._worker.shutdown(wait=False, cancel_futures=True)


def _integer(
    params: dict[str, Any], key: str, minimum: int = -1_000_000, maximum: int = 1_000_000
) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise TargetError(f"{key} is outside its supported range.", "invalid_arguments")
    return value


def _seconds(params: dict[str, Any]) -> float:
    value = params.get("seconds")
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
        raise TargetError("seconds must be between 0 and 100.", "invalid_arguments")
    return float(value)


def _keys(params: dict[str, Any], key: str) -> list[str]:
    value = params.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TargetError(f"{key} must be a list of key names.", "invalid_arguments")
    return value


def _button(params: dict[str, Any]) -> str:
    value = params.get("button")
    if value not in {"left", "right", "middle"}:
        raise TargetError("Unknown mouse button.", "invalid_arguments")
    return str(value)
