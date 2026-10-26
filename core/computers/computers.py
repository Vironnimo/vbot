"""Exact computer selection and bounded request routing to live Desktop installations."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.config_validation import JsonConfigValidationError
from core.utils.ids import is_safe_id, new_id
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

from ._store import COMPUTERS_PATH, ComputerStore, validate_description
from .policy import normalize_execution

if TYPE_CHECKING:
    from core.tools.tools import ToolContext

_LOGGER = get_logger("computers")
_STORE_WORKER = BoundedWorkerPool(name="computers", max_workers=1)
MAX_FRAME_BYTES = 16 * 1024 * 1024


class ComputerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ComputerInfo:
    id: str
    name: str
    platform: str
    online: bool
    capabilities: tuple[str, ...]
    instance_id: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "online": self.online,
            "capabilities": list(self.capabilities),
            "instance_id": self.instance_id,
        }


@dataclass(eq=False)
class ComputerConnection:
    info: ComputerInfo
    outgoing: asyncio.Queue[dict[str, Any] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=128)
    )
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    closed: bool = False


class ComputerRegistry:
    """Own identities and links; capability implementations own execution and recovery.

    Mutations run on the serving Event Loop. Snapshots and policy resolution are
    synchronous, including from native capability workers. No native PID, screen
    reference or application state is interpreted by this owner.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self.server_instance = new_id("srv")
        self._store: ComputerStore | None
        try:
            self._store = ComputerStore(data_dir / COMPUTERS_PATH if data_dir else None)
        except OSError, ValueError, JsonConfigValidationError:
            self._store = None
            _LOGGER.warning("Computer registrations unavailable; existing file was preserved")
        local_capabilities = (
            ("terminal", "computer_use") if sys.platform == "win32" else ("terminal",)
        )
        self._infos = {
            "server": ComputerInfo("server", "Server", sys.platform, True, local_capabilities)
        }
        if self._store is not None:
            for entry in self._store.entries.values():
                self._infos[entry["id"]] = ComputerInfo(
                    entry["id"],
                    entry["name"],
                    entry["platform"],
                    False,
                    tuple(entry["capabilities"]),
                )
        self._connections: dict[str, ComputerConnection] = {}
        self._listeners: list[Callable[[str, str, dict[str, Any]], None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._changed = asyncio.Event()
        self._registration_lock = asyncio.Lock()
        self._closed = False

    def list(self) -> list[ComputerInfo]:
        return sorted(
            self._infos.values(),
            key=lambda item: (item.id != "server", item.name.casefold(), item.id),
        )

    def get(self, computer_id: str) -> ComputerInfo:
        info = self._infos.get(computer_id)
        if info is None:
            raise ComputerError(
                "computer_not_found",
                f"Computer {computer_id!r} is not registered. Nothing was started or sent. "
                "Choose a registered computer in this Session's settings.",
            )
        return info

    def resolve(
        self, context: ToolContext, requested_id: str | None, capability: str
    ) -> ComputerInfo:
        policy = normalize_execution(context.execution)
        target = policy["computer_id"] if requested_id is None else requested_id
        if not isinstance(target, str) or not target:
            raise ComputerError("computer_not_found", "No computer was selected; nothing was sent.")
        if target not in self._infos:
            matches = [
                item.id
                for item in self._infos.values()
                if item.name.casefold() == target.casefold()
            ]
            if len(matches) == 1:
                target = matches[0]
        info = self.get(target)
        if info.id not in policy["allowed_computer_ids"]:
            raise ComputerError(
                "computer_not_allowed",
                f"Computer {info.name!r} is not enabled for this Session. Nothing was sent. "
                "The user can enable it in the Session's computer settings.",
            )
        self._available(info, capability)
        return info

    @staticmethod
    def _available(info: ComputerInfo, capability: str) -> None:
        if not info.online:
            raise ComputerError(
                "computer_offline",
                f"Computer {info.name!r} is offline. Nothing was sent. "
                "Reconnect its Desktop app to this server, then try again.",
            )
        if capability not in info.capabilities:
            raise ComputerError(
                "computer_capability_unavailable",
                f"Computer {info.name!r} does not share {capability}. Nothing was sent. "
                "Enable that function in its Desktop app.",
            )

    def subscribe(self, callback: Callable[[str, str, dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def _emit(self, computer_id: str, event: str, data: dict[str, Any]) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
        for callback in tuple(self._listeners):
            try:
                callback(computer_id, event, dict(data))
            except Exception:
                _LOGGER.exception("Computer event listener failed (computer=%s)", computer_id)

    async def connect(self, hello: Mapping[str, Any], *, actor: str) -> ComputerConnection:
        self._loop = asyncio.get_running_loop()
        if self._closed:
            raise ComputerError("computers_closed", "The server is shutting down.")
        if hello.get("type") != "hello" or hello.get("protocol") != 1:
            raise ComputerError("computer_protocol", "Unsupported computer connection protocol.")
        description: dict[str, Any] = {
            "id": hello.get("computer_id"),
            "name": hello.get("name"),
            "platform": hello.get("platform"),
            "capabilities": hello.get("capabilities"),
        }
        try:
            validate_description(description)
            instance_id = hello.get("instance_id")
            if not isinstance(instance_id, str) or not is_safe_id(instance_id):
                raise ValueError("Invalid computer instance")
        except (TypeError, ValueError) as error:
            raise ComputerError("computer_protocol", "Invalid computer registration.") from error
        async with self._registration_lock:
            if self._store is None:
                raise ComputerError(
                    "computer_store_unavailable", "Computer registrations are unavailable."
                )
            try:
                await _STORE_WORKER.run(self._store.claim, description, hello.get("key"), actor)
            except PermissionError as error:
                raise ComputerError(
                    "computer_identity_conflict",
                    "Computer identity belongs to another installation.",
                ) from error
            except (OSError, ValueError) as error:
                raise ComputerError(
                    "computer_registration_failed", "Computer registration could not be saved."
                ) from error
            computer_id = description["id"]
            if self._closed:
                raise ComputerError("computers_closed", "The server is shutting down.")
            previous = self._infos.get(computer_id)
            old = self._connections.get(computer_id)
            if old is not None:
                self.disconnect(old)
            info = ComputerInfo(
                computer_id,
                description["name"],
                description["platform"],
                True,
                tuple(dict.fromkeys(description["capabilities"])),
                instance_id,
            )
            connection = ComputerConnection(info)
            self._connections[computer_id] = connection
            self._infos[computer_id] = info
            self._emit(
                computer_id,
                "connected",
                {
                    "instance_id": instance_id,
                    "previous_instance_id": previous.instance_id if previous else "",
                },
            )
            _LOGGER.info("Computer connected (computer=%s)", computer_id)
            return connection

    def disconnect(self, connection: ComputerConnection) -> None:
        if connection.closed:
            return
        connection.closed = True
        for future in tuple(connection.pending.values()):
            if not future.done():
                future.set_exception(
                    ComputerError(
                        "computer_connection_lost",
                        "The computer connection ended after the request was sent; "
                        "its effects are unknown. "
                        "Reconnect and inspect the current state before repeating it.",
                    )
                )
        while not connection.outgoing.empty():
            connection.outgoing.get_nowait()
        connection.outgoing.put_nowait(None)
        computer_id = connection.info.id
        if self._connections.get(computer_id) is connection:
            del self._connections[computer_id]
            self._infos[computer_id] = replace(connection.info, online=False)
            self._emit(computer_id, "disconnected", {"instance_id": connection.info.instance_id})
            _LOGGER.info("Computer disconnected (computer=%s)", computer_id)

    def receive(self, connection: ComputerConnection, message: Any) -> None:
        if connection.closed or not isinstance(message, dict):
            return
        if message.get("type") == "result":
            request_id = message.get("id")
            future = connection.pending.get(request_id) if isinstance(request_id, str) else None
            if future is None or future.done():
                return
            if message.get("ok") is True and isinstance(message.get("result"), dict):
                future.set_result(message["result"])
            else:
                error = message.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                wording = error.get("message") if isinstance(error, dict) else None
                future.set_exception(
                    ComputerError(
                        code if isinstance(code, str) else "computer_execution_error",
                        wording
                        if isinstance(wording, str)
                        else "Computer operation failed; inspect its state before retrying.",
                    )
                )
        elif message.get("type") == "event":
            event, capability, data = (
                message.get("event"),
                message.get("capability"),
                message.get("data"),
            )
            if (
                isinstance(event, str)
                and event not in {"connected", "disconnected"}
                and capability in connection.info.capabilities
                and isinstance(data, dict)
            ):
                self._emit(
                    connection.info.id,
                    event,
                    {**data, "capability": capability, "instance_id": connection.info.instance_id},
                )

    async def call(
        self,
        computer_id: str,
        capability: str,
        operation: str,
        params: dict[str, Any],
        *,
        timeout: float = 30,
        instance_id: str | None = None,
    ) -> dict[str, Any]:
        info = self.get(computer_id)
        self._available(info, capability)
        if instance_id is not None and info.instance_id != instance_id:
            raise ComputerError(
                "computer_instance_changed",
                "The Desktop app restarted. Nothing was sent to the replacement instance.",
            )
        connection = self._connections.get(computer_id)
        if connection is None or connection.closed:
            raise ComputerError(
                "computer_offline", "The computer is not connected. Nothing was sent."
            )
        if len(connection.pending) >= 128:
            raise ComputerError(
                "computer_busy",
                "The computer has too many pending operations. Nothing was sent; "
                "try again after they finish.",
            )
        request_id = new_id("req")
        request = {
            "type": "call",
            "id": request_id,
            "capability": capability,
            "operation": operation,
            "params": params,
        }
        if len(json.dumps(request, ensure_ascii=True).encode()) > MAX_FRAME_BYTES:
            raise ComputerError(
                "computer_request_too_large",
                "The computer request is too large. Nothing was sent; split it into smaller calls.",
            )
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        connection.pending[request_id] = future
        try:
            connection.outgoing.put_nowait(request)
            async with asyncio.timeout(timeout):
                return await future
        except asyncio.QueueFull as error:
            raise ComputerError(
                "computer_busy",
                "The computer is busy. Nothing was sent; try again after pending work finishes.",
            ) from error
        except TimeoutError as error:
            raise ComputerError(
                "computer_timeout",
                "The computer did not answer in time; the request's effects are unknown. "
                "Inspect its state before repeating it.",
            ) from error
        finally:
            connection.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    def call_sync(
        self,
        computer_id: str,
        capability: str,
        operation: str,
        params: dict[str, Any],
        *,
        timeout: float = 30,
        instance_id: str | None = None,
    ) -> dict[str, Any]:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise ComputerError(
                "computer_offline", "No Desktop computer is connected. Nothing was sent."
            )
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            raise RuntimeError("call_sync cannot block the computer connection's Event Loop")
        future = asyncio.run_coroutine_threadsafe(
            self.call(
                computer_id, capability, operation, params, timeout=timeout, instance_id=instance_id
            ),
            loop,
        )
        try:
            return future.result(timeout + 5)
        except BaseException:
            future.cancel()
            raise

    async def wait_online(self, computer_id: str, *, instance_id: str | None = None) -> None:
        while not self._closed:
            changed = self._changed
            info = self.get(computer_id)
            if instance_id is not None and info.instance_id != instance_id:
                raise ComputerError(
                    "computer_instance_changed",
                    "The Desktop app restarted; the earlier process is unavailable.",
                )
            if info.online:
                return
            await changed.wait()
        raise ComputerError("computers_closed", "The server's computer connections have closed.")

    def close(self) -> None:
        self._closed = True
        for connection in tuple(self._connections.values()):
            self.disconnect(connection)
        self._changed.set()
        self._listeners.clear()

    async def aclose(self) -> None:
        self.close()
