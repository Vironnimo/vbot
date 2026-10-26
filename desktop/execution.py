"""Computer capabilities supplied by the running Desktop to its selected server.

The native connection belongs to the window's lifetime, independently of the
WebUI document. A server scope owns its capability handlers across network
reconnects; changing server or ending the Desktop closes those handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import re
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from core.utils.ids import new_id
from desktop._http import shared_ssl_context
from desktop.credentials import DeviceTokenStore, StoredComputerIdentity
from desktop.server_address import parse_server_address
from desktop.settings import read_section, update_section

logger = logging.getLogger("vbot.desktop.execution")
SETTINGS_KEY = "computer_sharing"
CAPABILITIES = ("terminal", "computer_use")
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_PENDING_CALLS = 64
MAX_REMEMBERED_CALLS = 4096
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")


class SharingError(Exception):
    """A Desktop sharing setting could not be applied."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = code


class ExecutionHandler(Protocol):
    async def execute(self, operation: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def disconnected(self) -> None: ...

    async def close(self) -> None: ...


class Authorization(Protocol):
    def header(self, server_url: str) -> str | None: ...

    def rejected(self, server_url: str, header: str | None) -> bool: ...


class Socket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


type EventSink = Callable[[str, dict[str, Any]], None]
type HandlerFactory = Callable[[EventSink], ExecutionHandler]


@asynccontextmanager
async def connect_socket(url: str, header: str | None) -> AsyncIterator[Socket]:
    """Open one native socket with the same origin/TLS policy as Desktop HTTP."""
    from websockets.asyncio.client import connect

    target = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    options: dict[str, Any] = {
        "additional_headers": {"Authorization": header} if header else {},
        "max_size": MAX_MESSAGE_BYTES,
        "proxy": None,
        "open_timeout": 10,
        "close_timeout": 2,
        "ping_interval": 15,
        "ping_timeout": 15,
    }
    if target.startswith("wss://"):
        options["ssl"] = shared_ssl_context()
    async with connect(f"{target.rstrip('/')}/ws/computers", **options) as socket:
        yield socket


@dataclass
class _Scope:
    origin: str
    identity: StoredComputerIdentity
    instance_id: str = field(default_factory=lambda: new_id("pci"))
    server_instance: str | None = None
    handlers: dict[str, ExecutionHandler] = field(default_factory=dict)
    calls: set[asyncio.Task[None]] = field(default_factory=set)
    seen: set[str] = field(default_factory=set)
    seen_order: deque[str] = field(default_factory=deque)
    connection: int = 0


class ComputerSharing:
    """Thread-safe Desktop settings/lifecycle facade around one native async loop."""

    def __init__(
        self,
        *,
        settings_path: Path,
        identities: DeviceTokenStore,
        authorization: Authorization,
        factories: Mapping[str, HandlerFactory],
        publish: Callable[[Mapping[str, Any]], None] = lambda _status: None,
        connector: Callable[..., Any] = connect_socket,
        available: Mapping[str, bool] | None = None,
    ) -> None:
        self._settings_path = settings_path
        self._settings = read_section(SETTINGS_KEY, settings_path)
        self._identities = identities
        self._authorization = authorization
        self._factories = dict(factories)
        self._publish = publish
        self._connector = connector
        self._available = {
            name: name in factories and (name != "computer_use" or sys.platform == "win32")
            for name in CAPABILITIES
        }
        if available is not None:
            self._available.update(available)
        self._lock = threading.RLock()
        self._origin = ""
        self._server_url = ""
        self._closed = False
        self._quiescing = False
        self._switching_server = False
        self._starting_terminals = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._changed: asyncio.Event | None = None
        self._scope: _Scope | None = None
        self._socket: Socket | None = None
        self._connected = False
        self._serving_header: str | None = None
        self._welcomes = 0
        self._error_code: str | None = None
        self._last_status: dict[str, Any] | None = None
        self._capability_errors: dict[str, str] = {}

    def start(self) -> None:
        with self._lock:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._thread_main, name="vbot-desktop-computer", daemon=True
            )
            self._thread.start()

    def set_server_url(self, server_url: str) -> None:
        address = parse_server_address(server_url.rstrip("/"), None) if server_url else None
        with self._lock:
            origin = address.origin if address else ""
            if origin == self._origin:
                # A completed pairing can replace this origin's credential; every
                # other reconnect of the window keeps the healthy native link.
                if (
                    not self._connected
                    or self._authorization.header(self._server_url) != self._serving_header
                ):
                    self._wake()
                return
            self._origin = origin
            self._server_url = address.url if address else ""
            self._connected = False
            self._error_code = None
            self._wake()
        self._report()

    def status(self) -> dict[str, Any]:
        with self._lock:
            settings = self._settings
            identity = self._identities.computer_identity(self._origin) if self._origin else None
            return {
                "computer_id": identity.computer_id if identity else None,
                "name": _computer_name(settings.get("name")),
                "server_url": self._server_url,
                "connected": self._connected,
                "capabilities": _grants(settings, self._origin),
                "available": {
                    name: allowed and name not in self._capability_errors
                    for name, allowed in self._available.items()
                },
                "active_terminals": self.active_terminals,
                "error_code": self._error_code,
            }

    @property
    def active_terminals(self) -> int:
        with self._lock:
            handler = self._scope.handlers.get("terminal") if self._scope else None
            count = getattr(handler, "active_count", 0)
            return max(
                count if isinstance(count, int) and count >= 0 else 0, self._starting_terminals
            )

    def allow_close(self, confirm: Callable[[str, str], bool]) -> bool:
        """Fence new work before checking whether quitting needs the user's choice."""
        with self._lock:
            self._quiescing = True
            count = self.active_terminals
        if not count:
            return True
        try:
            allowed = confirm(
                "Close vBot Desktop?",
                f"{count} local terminal(s) are still running. Closing vBot Desktop stops "
                "their programs and disconnects this computer. Close it?",
            )
        except Exception:
            logger.warning("The Desktop could not confirm stopping local terminals", exc_info=True)
            allowed = False
        if not allowed:
            self.resume()
        return bool(allowed)

    def prepare_restart(self) -> bool:
        """Fence new work for an update only while no local terminal needs preserving."""
        with self._lock:
            if self.active_terminals or self._switching_server:
                return False
            self._quiescing = True
            return True

    @contextmanager
    def changing_server(
        self, server_url: str, confirm: Callable[[str, str], bool]
    ) -> Iterator[bool]:
        """Fence admission while a ready target replaces the selected server."""
        target = parse_server_address(server_url.rstrip("/"), None)
        with self._lock:
            origin = self._origin
            changing = bool(origin and origin != target.origin)
            busy = self._closed or self._quiescing or self._switching_server
            if changing and not busy:
                self._switching_server = True
            count = self.active_terminals
        if busy or not changing:
            yield not busy
            return
        try:
            allowed = True
            if count:
                try:
                    allowed = confirm(
                        "Switch vBot server?",
                        f"{count} local terminal(s) are still running. Switching to "
                        f"{target.origin} stops their programs and disconnects this computer "
                        "from its current server. Switch server?",
                    )
                except Exception:
                    logger.warning("The Desktop could not confirm switching servers", exc_info=True)
                    allowed = False
            with self._lock:
                allowed = bool(
                    allowed and self._origin == origin and not self._closed and not self._quiescing
                )
            yield allowed
        finally:
            with self._lock:
                self._switching_server = False

    def resume(self) -> None:
        """Release a closing/restart fence when the window remains open."""
        with self._lock:
            self._quiescing = False

    def update(self, changes: Any, *, expected_server: str) -> dict[str, Any]:
        """Apply the native-approved settings only to the unchanged selected server."""
        if not isinstance(changes, dict) or set(changes) - {"name", *CAPABILITIES}:
            raise SharingError("computer_sharing_invalid", "Invalid computer sharing settings")
        normalized: dict[str, Any] = {}
        for name in CAPABILITIES:
            if name in changes:
                if not isinstance(changes[name], bool):
                    raise SharingError(
                        "computer_sharing_invalid", "A sharing choice must be boolean"
                    )
                if changes[name] and not self._available.get(name, False):
                    raise SharingError(
                        "computer_capability_unavailable", "This capability is unavailable"
                    )
                normalized[name] = changes[name]
        if "name" in changes:
            name = changes["name"]
            if (
                not isinstance(name, str)
                or not name.strip()
                or len(name.strip()) > 80
                or not name.isprintable()
            ):
                raise SharingError(
                    "computer_name_invalid", "Use a computer name of 1 to 80 printable characters"
                )
            normalized["name"] = name.strip()
        with self._lock:
            if not self._origin or expected_server != self._server_url or self._closed:
                raise SharingError(
                    "computer_server_changed",
                    "The selected server changed; open the settings again",
                )
            origin = self._origin

            def apply(settings: dict[str, Any]) -> dict[str, Any]:
                if "name" in normalized:
                    settings["name"] = normalized["name"]
                servers = settings.setdefault("servers", {})
                if not isinstance(servers, dict):
                    raise SharingError(
                        "computer_sharing_invalid", "Stored computer sharing settings are invalid"
                    )
                grants = servers.setdefault(origin, {})
                if not isinstance(grants, dict):
                    raise SharingError(
                        "computer_sharing_invalid", "Stored computer sharing settings are invalid"
                    )
                grants.update(
                    {name: normalized[name] for name in CAPABILITIES if name in normalized}
                )
                return settings

            updated = update_section(SETTINGS_KEY, apply, self._settings_path)
            self._settings = updated
            if any(_grants(updated, origin).values()):
                self._identities.computer_identity(origin, create=True)
            self._wake()
        logger.info("Computer sharing settings changed (fields=%s)", ",".join(sorted(normalized)))
        self._report()
        return self.status()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._wake()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=30)
            if thread.is_alive():
                logger.error("Desktop computer execution did not close within 30 seconds")

    def _wake(self) -> None:
        if self._loop is not None and self._changed is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._changed.set)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self.run())
        except Exception:
            logger.exception("Desktop computer execution stopped unexpectedly")
            self._set_connection(False, "computer_execution_failed")

    async def run(self) -> None:
        """Serve until close; also usable on an owned Event Loop in protocol tests."""
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._changed = asyncio.Event()
        retry_seconds = 1.0
        try:
            while not self._closed:
                self._changed.clear()
                with self._lock:
                    origin, server_url = self._origin, self._server_url
                    settings = self._settings
                    grants = _grants(settings, origin)
                if self._scope is not None and self._scope.origin != origin:
                    await self._close_scope()
                enabled = {
                    name for name, allowed in grants.items() if allowed and self._available[name]
                }
                if not origin or not enabled:
                    await self._close_scope()
                    self._set_connection(False, None)
                    await self._changed.wait()
                    continue
                try:
                    identity = self._identities.computer_identity(origin, create=True)
                    assert identity is not None
                    if self._scope is not None and self._scope.identity != identity:
                        await self._close_scope()
                    if self._scope is None:
                        self._scope = _Scope(origin, identity)
                    scope = self._scope
                    welcomes = self._welcomes
                    await self._configure(scope, enabled)
                    serving = asyncio.create_task(
                        self._serve(scope, server_url, _computer_name(settings.get("name")))
                    )
                    changed = asyncio.create_task(self._changed.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (serving, changed), return_when=asyncio.FIRST_COMPLETED
                        )
                        if changed in done:
                            serving.cancel()
                        await serving
                    except asyncio.CancelledError:
                        if not changed.done():
                            raise
                    finally:
                        changed.cancel()
                        await asyncio.gather(changed, return_exceptions=True)
                    retry_seconds = 1.0
                except _ServerRestartedError:
                    await self._close_scope()
                    continue
                except Exception as error:
                    self._set_connection(False, _connection_error(error))
                    if self._welcomes != welcomes:
                        retry_seconds = 1.0  # a working link lost later starts a fresh backoff
                if self._closed or self._changed.is_set():
                    continue
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=retry_seconds)
                except TimeoutError:
                    retry_seconds = min(30.0, retry_seconds * 2)
        finally:
            await self._close_scope()
            self._set_connection(False, None)
            with self._lock:
                self._loop = None
                self._changed = None

    async def _configure(self, scope: _Scope, enabled: set[str]) -> None:
        for capability in set(scope.handlers) - enabled:
            await scope.handlers[capability].close()
            del scope.handlers[capability]
        self._capability_errors.clear()
        for capability in sorted(enabled):
            handler = scope.handlers.get(capability)
            if handler is None:

                def emit(event: str, data: dict[str, Any], cap: str = capability) -> None:
                    self._emit(scope, cap, event, data)

                handler = self._factories[capability](emit)
                scope.handlers[capability] = handler
            ready = getattr(handler, "ready", None)
            if ready is not None:
                reason = await ready()
                if reason is not None:
                    self._capability_errors[capability] = str(reason)
                    await scope.handlers.pop(capability).close()
        if not scope.handlers:
            raise SharingError(
                "computer_capability_unavailable", "The shared capabilities are unavailable"
            )

    async def _serve(self, scope: _Scope, server_url: str, name: str) -> None:
        header = self._authorization.header(server_url)
        self._serving_header = header
        try:
            async with self._connector(server_url, header) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "protocol": 1,
                            "computer_id": scope.identity.computer_id,
                            "key": scope.identity.key,
                            "instance_id": scope.instance_id,
                            "name": name,
                            "platform": sys.platform,
                            "capabilities": sorted(scope.handlers),
                        }
                    )
                )
                welcome = _message(await asyncio.wait_for(socket.recv(), timeout=10))
                if welcome.get("type") == "error":
                    raise _refusal(welcome)
                if (
                    welcome.get("type") != "welcome"
                    or welcome.get("protocol") != 1
                    or welcome.get("computer_id") != scope.identity.computer_id
                    or welcome.get("instance_id") != scope.instance_id
                    or not isinstance(welcome.get("server_instance"), str)
                ):
                    raise SharingError(
                        "computer_protocol_error", "The server returned an invalid welcome"
                    )
                server_instance = welcome["server_instance"]
                if scope.server_instance is not None and scope.server_instance != server_instance:
                    raise _ServerRestartedError()
                scope.server_instance = server_instance
                scope.connection += 1
                self._welcomes += 1
                self._socket = socket
                self._set_connection(True, None)
                while True:
                    message = _message(await socket.recv())
                    if message.get("type") == "error":
                        raise _refusal(message)
                    if message.get("type") != "call":
                        raise SharingError(
                            "computer_protocol_error", "The server sent an invalid request"
                        )
                    call_id = message.get("id")
                    if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
                        raise SharingError(
                            "computer_protocol_error", "The request has no valid identity"
                        )
                    if call_id in scope.seen:
                        await self._failure(
                            socket,
                            call_id,
                            "computer_duplicate_call",
                            "This request was already received; no action was repeated",
                        )
                        continue
                    if len(scope.calls) >= MAX_PENDING_CALLS:
                        await self._failure(
                            socket,
                            call_id,
                            "computer_busy",
                            "The computer has too many pending operations; nothing was started",
                        )
                        continue
                    scope.seen.add(call_id)
                    scope.seen_order.append(call_id)
                    if len(scope.seen_order) > MAX_REMEMBERED_CALLS:
                        scope.seen.discard(scope.seen_order.popleft())
                    task = asyncio.create_task(self._call(scope, socket, scope.connection, message))
                    scope.calls.add(task)
                    task.add_done_callback(scope.calls.discard)
        except Exception as error:
            if _unauthorized(error):
                self._authorization.rejected(server_url, header)
            raise
        finally:
            self._socket = None
            scope.connection += 1
            self._set_connection(False, self._error_code)
            await asyncio.gather(*(handler.disconnected() for handler in scope.handlers.values()))

    async def _call(
        self, scope: _Scope, socket: Socket, connection: int, request: dict[str, Any]
    ) -> None:
        call_id = request["id"]
        starting_terminal = False
        try:
            if scope.connection != connection or scope is not self._scope:
                return
            capability, operation, params = (
                request.get("capability"),
                request.get("operation"),
                request.get("params"),
            )
            handler = scope.handlers.get(capability) if isinstance(capability, str) else None
            read_only = isinstance(operation, str) and operation in getattr(
                handler, "read_only_operations", ()
            )
            with self._lock:
                grants = _grants(self._settings, self._origin)
                if (
                    self._origin != scope.origin
                    or not isinstance(capability, str)
                    or not grants.get(capability, False)
                ):
                    raise SharingError(
                        "computer_capability_not_shared",
                        "This capability is not shared; nothing was done",
                    )
                if (self._quiescing or self._switching_server) and not read_only:
                    raise SharingError(
                        "computer_closing",
                        "The Desktop is closing, restarting or switching servers; nothing was done",
                    )
                if capability == "terminal" and operation == "start":
                    self._starting_terminals += 1
                    starting_terminal = True
            if handler is None:
                raise SharingError(
                    "computer_capability_not_shared",
                    "This capability is not shared; nothing was done",
                )
            if not isinstance(operation, str) or not operation or not isinstance(params, dict):
                raise SharingError(
                    "invalid_arguments",
                    "The operation and its parameters are required; nothing was done",
                )
            result = await handler.execute(operation, params)
            if not isinstance(result, dict):
                raise TypeError("A computer capability returned an invalid result")
            await self._send(
                socket, {"type": "result", "id": call_id, "ok": True, "result": result}
            )
        except Exception as error:
            code = getattr(error, "code", getattr(error, "error_code", None))
            if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
                code = "computer_execution_error"
                message = (
                    "The computer operation failed; its effects are unknown. "
                    "Check the current state before repeating it."
                )
                logger.error("Computer operation failed (error_type=%s)", type(error).__name__)
            else:
                message = str(error)
            await self._failure(socket, call_id, code, message)
        finally:
            if starting_terminal:
                with self._lock:
                    self._starting_terminals -= 1
            self._report()

    async def _send(self, socket: Socket, message: dict[str, Any]) -> None:
        if self._socket is not socket:
            return
        encoded = json.dumps(message)
        if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("Computer response exceeds transport limit")
        # The receive loop reports a lost connection; a reply is never replayed.
        with contextlib.suppress(Exception):
            await socket.send(encoded)

    async def _failure(self, socket: Socket, call_id: str, code: str, message: str) -> None:
        await self._send(
            socket,
            {
                "type": "result",
                "id": call_id,
                "ok": False,
                "error": {"code": code, "message": message[:4000]},
            },
        )

    def _emit(self, scope: _Scope, capability: str, event: str, data: dict[str, Any]) -> None:
        def deliver() -> None:
            socket = self._socket
            if socket is None or self._scope is not scope:
                return
            task = asyncio.create_task(
                self._send(
                    socket,
                    {"type": "event", "capability": capability, "event": event, "data": data},
                )
            )
            scope.calls.add(task)
            task.add_done_callback(scope.calls.discard)

        with self._lock:
            if self._loop is not None:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(deliver)

    async def _close_scope(self) -> None:
        scope = self._scope
        if scope is None:
            return
        self._socket = None
        scope.connection += 1
        disconnects = await asyncio.gather(
            *(handler.disconnected() for handler in scope.handlers.values()), return_exceptions=True
        )
        closed = await asyncio.gather(
            *(handler.close() for handler in scope.handlers.values()), return_exceptions=True
        )
        if scope.calls:
            await asyncio.gather(*tuple(scope.calls), return_exceptions=True)
        for result in (*disconnects, *closed):
            if isinstance(result, BaseException):
                raise result
        self._scope = None

    def _set_connection(self, connected: bool, error_code: str | None) -> None:
        with self._lock:
            previous = self._connected, self._error_code
            self._connected, self._error_code = connected, error_code
        if previous == (connected, error_code):
            return
        if connected:
            logger.info("Desktop computer connected")
        elif error_code and previous[1] != error_code:
            logger.warning("Desktop computer unavailable (code=%s)", error_code)
        self._report()

    def _report(self) -> None:
        status = self.status()
        with self._lock:
            if status == self._last_status:
                return
            self._last_status = status
        self._publish(status)


class _ServerRestartedError(Exception):
    pass


def _message(raw: str | bytes) -> dict[str, Any]:
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise SharingError("computer_protocol_error", "The server sent an invalid message")
    return message


def _refusal(message: dict[str, Any]) -> SharingError:
    """The server's own reason, such as an identity conflict, instead of a protocol error."""
    error = message.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    wording = error.get("message") if isinstance(error, dict) else None
    if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
        code = "computer_protocol_error"
    return SharingError(
        code,
        wording if isinstance(wording, str) else "The server refused this computer",
    )


def _grants(settings: dict[str, Any], origin: str) -> dict[str, bool]:
    servers = settings.get("servers")
    grants = servers.get(origin) if isinstance(servers, dict) else None
    return {name: isinstance(grants, dict) and grants.get(name) is True for name in CAPABILITIES}


def _computer_name(value: Any) -> str:
    return value if isinstance(value, str) and value else platform.node() or "This computer"


def _unauthorized(error: Exception) -> bool:
    response = getattr(error, "response", None)
    received = getattr(error, "rcvd", None)
    return getattr(response, "status_code", None) == 401 or getattr(received, "code", None) == 4401


def _connection_error(error: Exception) -> str:
    if _unauthorized(error):
        return "auth_required"
    code = getattr(error, "error_code", None)
    return (
        code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else "computer_disconnected"
    )
