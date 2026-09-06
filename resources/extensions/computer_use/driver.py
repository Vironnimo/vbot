"""Persistent, bounded MCP transport to the independently installed Cua Driver."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from contextlib import ExitStack, asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .windows import WindowsDesktop

import anyio
from anyio.from_thread import start_blocking_portal
from anyio.streams.text import TextReceiveStream
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import jsonrpc_message_adapter

MINIMUM_VERSION = (0, 23, 2)
TIMEOUT = 45
SAFE_ENVIRONMENT_KEYS = {
    "APPDATA",
    "COMSPEC",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
}


class EmergencyHotkey:
    """Observe physical double-Esc globally, dispatching stop off the hook thread."""

    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.available = False
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._closing = threading.Event()
        self._requested = threading.Event()
        self._worker: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._armed = False
        self._down = False
        self._first_press: float | None = None

    def set_armed(self, armed: bool) -> None:
        with self._state_lock:
            if armed != self._armed:
                self._armed = armed
                self._first_press = None
                self._down = False

    @property
    def pending(self) -> bool:
        return self._requested.is_set() and not self._closing.is_set()

    def _key_event(self, key: int, down: bool, flags: int) -> None:
        # LLKHF_INJECTED / LLKHF_LOWER_IL_INJECTED: Agent input never counts.
        if flags & 0x12:
            return
        with self._state_lock:
            if not self._armed:
                return
            if key != 0x1B:
                if down:
                    self._first_press = None
                return
            if not down:
                self._down = False
                return
            if self._down:  # Holding Esc and its auto-repeat are one press.
                return
            self._down = True
            now = time.monotonic()
            if self._first_press is not None and now - self._first_press <= 0.6:
                self._first_press = None
                self._armed = False
                self._requested.set()
            else:
                self._first_press = now

    def _dispatch(self) -> None:
        while not self._closing.is_set():
            self._requested.wait()
            if self._closing.is_set():
                return
            try:
                self.callback()
            finally:
                self._requested.clear()

    def start(self) -> None:
        if os.name != "nt":
            return
        if self._thread is not None:
            return
        self._worker = threading.Thread(target=self._dispatch, daemon=True)
        self._worker.start()
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        self._ready.wait(2)

    def _listen(self) -> None:
        import ctypes
        from ctypes import wintypes

        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class KeyboardEvent(ctypes.Structure):
            _fields_ = [
                ("key", wintypes.DWORD),
                ("scan", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("extra", ctypes.c_size_t),
            ]

        hook_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        user.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            hook_proc,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        user.SetWindowsHookExW.restype = wintypes.HHOOK
        user.CallNextHookEx.argtypes = [
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user.CallNextHookEx.restype = ctypes.c_ssize_t
        user.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        user.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._thread_id = kernel.GetCurrentThreadId()

        @hook_proc
        def observe(code: int, message: int, pointer: int) -> int:
            if code == 0 and message in {0x100, 0x101, 0x104, 0x105}:
                event = ctypes.cast(pointer, ctypes.POINTER(KeyboardEvent)).contents
                self._key_event(event.key, message in {0x100, 0x104}, event.flags)
            # Never swallow normal Esc or interfere with the foreground app.
            return int(user.CallNextHookEx(None, code, message, pointer))

        hook = user.SetWindowsHookExW(13, observe, kernel.GetModuleHandleW(None), 0)
        message = wintypes.MSG()
        # Ensure PostThreadMessage can wake shutdown even before GetMessage starts.
        user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self.available = bool(hook)
        self._ready.set()
        if not self.available:
            return
        try:
            while (
                not self._closing.is_set()
                and user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0
            ):
                pass
        finally:
            user.UnhookWindowsHookEx(hook)
            self.available = False

    def close(self) -> None:
        self._closing.set()
        self.set_armed(False)
        self._requested.set()
        if self._thread_id and self.available:
            import ctypes

            ctypes.WinDLL("user32").PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._worker is not None:
            self._worker.join(timeout=2)


class ComputerUseError(Exception):
    """A driver/observation failure; mutations must never retry automatically."""

    def __init__(self, message: str, code: str = "computer_use_failed") -> None:
        super().__init__(message)
        self.code = code


def unpack(result: dict[str, Any]) -> dict[str, Any]:
    """Retain structured data and image blocks, rejecting explicit refusals."""
    if result.get("isError") or result.get("is_error"):
        raise ComputerUseError(_error_text(result))
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        payload = None
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    candidate = json.loads(block.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(candidate, dict):
                    payload = candidate
                    break
        if payload is None:
            if "content" in result:
                raise ComputerUseError(_error_text(result))
            payload = result
    for key in ("result", "data"):
        if isinstance(payload.get(key), dict):
            payload = payload[key]
    if (
        payload.get("status") == "refused"
        or payload.get("effect") == "refused"
        or payload.get("isError")
    ):
        raise ComputerUseError(_error_text(payload))
    output = dict(payload)
    for block in result.get("content", []):
        if block.get("type") == "image" and block.get("mimeType") == "image/png":
            output.setdefault("screenshot_png_b64", block.get("data"))
            break
    return output


def _error_text(result: dict[str, Any]) -> str:
    refusal = result.get("refusal") or result.get("error") or result.get("message")
    if refusal:
        return str(refusal)[:4000]
    return (
        " ".join(
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        )[:4000]
        or "The driver connection was lost. Capture the target again before sending input."
    )


class CuaDriver:
    """One SDK connection shared by serialized service calls, with no input replay."""

    def __init__(self, executable: str) -> None:
        self.executable = executable
        self._stack: ExitStack | None = None
        self._portal: Any = None
        self._session: ClientSession | None = None
        self.schemas: dict[str, dict[str, Any]] = {}
        self.version = ""
        self.broken = False
        self._process: Any = None
        self._process_lock = threading.Lock()
        self._interrupted = False
        self.desktop: WindowsDesktop | None = None
        if os.name == "nt":
            from .windows import WindowsDesktop

            self.desktop = WindowsDesktop()

    def interrupt(self) -> None:
        """Stop only this connection's owned input worker, without waiting for RPC."""
        if self.desktop is not None:
            try:
                self.desktop.interrupt()
            except ComputerUseError:
                # The MCP process must still be interrupted if Windows refused key cleanup.
                self.broken = True
        with self._process_lock:
            self._interrupted = True
            self.broken = True
            if self._process is not None:
                with suppress(ProcessLookupError):
                    self._process.kill()

    @asynccontextmanager
    async def _stdio(self, environment: dict[str, str]):
        # Own the real input worker, never a proxy to a shared desktop daemon.
        process = await anyio.open_process(
            [self.executable, "mcp", "--direct"],
            env=environment,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        with self._process_lock:
            self._process = process
            if self._interrupted:
                process.kill()
        incoming, reader = anyio.create_memory_object_stream[SessionMessage | Exception](0)
        writer, outgoing = anyio.create_memory_object_stream[SessionMessage](0)

        async def receive():
            assert process.stdout is not None
            async with incoming:
                buffer = ""
                async for chunk in TextReceiveStream(process.stdout):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        try:
                            message = SessionMessage(jsonrpc_message_adapter.validate_json(line))
                        except ValueError as error:
                            await incoming.send(error)
                        else:
                            await incoming.send(message)

        async def send():
            assert process.stdin is not None
            try:
                async with outgoing:
                    async for message in outgoing:
                        encoded = message.message.model_dump_json(by_alias=True, exclude_none=True)
                        await process.stdin.send((encoded + "\n").encode())
            except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                await incoming.aclose()

        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(receive)
                tasks.start_soon(send)
                try:
                    yield reader, writer
                finally:
                    with self._process_lock:
                        if process.returncode is None:
                            with suppress(ProcessLookupError):
                                process.kill()
                        self._process = None
                    tasks.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await process.aclose()
                await reader.aclose()
                await writer.aclose()
                await incoming.aclose()
                await outgoing.aclose()

    @asynccontextmanager
    async def _connection(self):
        environment = {
            key: value for key, value in os.environ.items() if key.upper() in SAFE_ENVIRONMENT_KEYS
        }
        environment["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
        async with (
            self._stdio(environment) as (reader, writer),
            ClientSession(reader, writer, read_timeout_seconds=TIMEOUT) as session,
        ):
            initialized = await session.initialize()
            self.version = initialized.server_info.version
            version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", self.version)
            if not version or tuple(map(int, version.groups())) < MINIMUM_VERSION:
                raise ComputerUseError(
                    "This driver lacks a required capability. Update cua-driver and "
                    "reload Extensions."
                )
            catalog = await session.list_tools()
            self.schemas = {tool.name: tool.input_schema for tool in catalog.tools}
            for name in ("capture_pixels", "list_monitors"):
                self.schemas[name] = {"properties": {"session": {}}}
            config = await session.call_tool("get_config", {})
            values = unpack(config.model_dump(by_alias=True, exclude_none=True))
            if self.desktop is None and values.get("max_image_dimension") != 0:
                raise ComputerUseError(
                    "Set cua-driver max_image_dimension to 0 and reload Extensions so "
                    "original screenshots remain available."
                )
            yield session

    def connect(self) -> None:
        if self._interrupted:
            raise ComputerUseError(
                "Computer Use was stopped by the user. "
                "Wait for the user to allow computer control again.",
                "computer_use_stopped",
            )
        if self._session is not None:
            return
        stack = ExitStack()
        try:
            portal = stack.enter_context(start_blocking_portal())
            session = stack.enter_context(portal.wrap_async_context_manager(self._connection()))
        except Exception as exc:
            stack.close()
            self.broken = True
            if self._interrupted:
                raise ComputerUseError(
                    "Computer Use was stopped by the user. "
                    "Wait for the user to allow computer control again.",
                    "computer_use_stopped",
                ) from exc
            pending: list[BaseException] = [exc]
            while pending:
                error = pending.pop()
                if isinstance(error, ComputerUseError):
                    raise error from exc
                if isinstance(error, BaseExceptionGroup):
                    pending.extend(error.exceptions)
            raise ComputerUseError(
                "The driver connection was lost. Capture the target again before sending input."
            ) from exc
        with self._process_lock:
            self._stack, self._portal, self._session = stack, portal, session
            self.broken = self._interrupted
        if self._interrupted:
            self.close()
            raise ComputerUseError(
                "Computer Use was stopped by the user. "
                "Wait for the user to allow computer control again.",
                "computer_use_stopped",
            )

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.connect()
        arguments = dict(arguments)
        if "modifiers" in arguments and (self.desktop is None or "element_token" in arguments):
            raise ComputerUseError("Invalid value for modifiers.", "invalid_arguments")
        if self.desktop is not None:
            if name == "list_monitors":
                return {"monitors": self.desktop.monitors()}
            if name in {"capture_pixels", "get_desktop_state"}:
                return self.desktop.capture(arguments)
            if name == "get_window_state" and arguments.get("include_screenshot", True):
                # One native screenshot; request UIA separately only when needed.
                pixels = self.desktop.capture(arguments)
                state = self.call(name, {**arguments, "include_screenshot": False})
                return {**state, **pixels}
            if (
                name
                in {"move_cursor", "click", "type_text", "press_key", "hotkey", "scroll", "drag"}
                and "element_token" not in arguments
            ):
                if arguments.get("delivery_mode") != "foreground":
                    raise ComputerUseError(
                        (
                            "Pixel input needs foreground delivery. Use foreground=true or a window"
                            " element reference."
                        ),
                        "foreground_required",
                    )
                return self.desktop.input(name, arguments)
            if name == "end_session":
                self.desktop.end_session(arguments.get("session", ""))
        elif name == "list_monitors" or "monitor" in arguments or "duration_ms" in arguments:
            raise ComputerUseError(
                "Monitor selection and timed input are currently available on Windows only.",
                "unsupported_capability",
            )
        elif name == "capture_pixels":
            name = "get_window_state"
        # Desktop scope is implicit in this read-only upstream method.
        if name == "get_desktop_state":
            arguments.pop("scope", None)
        if name not in self.schemas:
            raise ComputerUseError(
                "This driver lacks a required capability. Update cua-driver and reload Extensions."
            )
        try:
            assert self._session is not None
            response = self._portal.call(self._session.call_tool, name, arguments)
        except Exception as exc:
            self.broken = True
            with suppress(Exception):  # Preserve the dispatch failure; never replay input.
                self.close()
            if self._interrupted:
                raise ComputerUseError(
                    "Computer Use was stopped by the user. "
                    "Wait for the user to allow computer control again.",
                    "computer_use_stopped",
                ) from exc
            raise ComputerUseError(
                "The driver connection was lost. Capture the target again before sending input."
            ) from exc
        return unpack(response.model_dump(by_alias=True, exclude_none=True))

    def close(self) -> None:
        if self.desktop is not None:
            self.desktop.release()
        stack, self._stack = self._stack, None
        self._session = None
        self._portal = None
        if stack is not None:
            stack.close()
