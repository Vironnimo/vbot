"""Select a computer and keep each native desktop's Computer Use state separate."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.computers import ComputerError
from core.tools import tool_failure

from ._remote_target import RemoteDesktopTarget, RemoteHotkey

if TYPE_CHECKING:
    from core.computers import ComputerInfo, ComputerRegistry
    from core.extensions.operations import ExtensionHost
    from core.tools import ToolContext

    from .extension import ComputerUseService


@dataclass
class _Remote:
    instance_id: str
    service: ComputerUseService
    target: RemoteDesktopTarget


class ComputerRouting:
    def __init__(self, local: ComputerUseService, host: ExtensionHost) -> None:
        self.local = local
        self.host = host
        self.computers: ComputerRegistry | None = getattr(host, "computers", None)
        self._remotes: dict[str, _Remote] = {}
        self._closing: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._unsubscribe = self.computers.subscribe(self._event) if self.computers else None

    def ready(self) -> bool:
        return self.computers is not None and any(
            item.id != "server" and item.online and "computer_use" in item.capabilities
            for item in self.computers.list()
        )

    async def call(
        self, tool: str, context: ToolContext, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """None means the local service handles this exact server target."""
        try:
            requested = arguments.get("computer_id")
            if self.computers is None:
                default = context.execution.get("computer_id", "server")
                if (requested if requested is not None else default) != "server":
                    raise ComputerError(
                        "computer_not_found",
                        "This server has no connected computer registry. Nothing was done.",
                    )
                return None
            computer = self.computers.resolve(context, requested, "computer_use")
            if computer.id == "server":
                return None
            service = await self._service(computer)
            handlers = {
                "computer": service.computer,
                "computer_batch": service.computer_batch,
                "computer_apps": service.computer_apps,
            }
            result = await handlers[tool](context, arguments)
            if result.get("ok"):
                result["data"]["content"] = (
                    f"Computer: {computer.name}.\n" + result["data"]["content"]
                )
            elif isinstance(result.get("error"), dict):
                result["error"]["message"] = (
                    f"Computer: {computer.name}. " + result["error"]["message"]
                )
            return result
        except ComputerError as error:
            return tool_failure(error.code, error.message)

    async def _service(self, computer: ComputerInfo) -> ComputerUseService:
        from .extension import ComputerUseService

        assert self.computers is not None
        async with self._lock:
            existing = self._remotes.get(computer.id)
            if existing is not None and existing.instance_id == computer.instance_id:
                return existing.service
            if existing is not None:
                await existing.service.close()
                await existing.target.drain()
            target = RemoteDesktopTarget(self.computers, computer)
            service = ComputerUseService(
                self.local.api,
                target_factory=lambda: target,
                hotkey_factory=lambda callback: RemoteHotkey(target, callback),
                computer_name=computer.name,
                on_call_start=target.begin,
                on_interrupt=target.interrupt,
            )

            def changed() -> None:
                with service._control_lock:
                    service._control_changed()

            target.on_control_changed = changed
            await service.start(self.host)
            try:
                # Registration can be removed while native readiness waits.
                self.computers.get(computer.id)
            except ComputerError:
                target.close()
                await service.close()
                await target.drain()
                raise
            self._remotes[computer.id] = _Remote(computer.instance_id, service, target)
            return service

    def _event(self, computer_id: str, event: str, data: dict[str, Any]) -> None:
        remote = self._remotes.get(computer_id)
        if remote is None:
            return
        service = remote.service
        if event == "removed":
            del self._remotes[computer_id]
            # Fence old queued controls before a later registration can appear.
            remote.target.close()
            task = asyncio.create_task(self._retire(remote), name=f"computer-close:{computer_id}")
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        elif event == "disconnected":
            remote.target.set_hotkey_available(False)
            service.stop(source="connection_lost")
            service.sessions.clear()
            service.access.cancel_all()
        elif event == "connected":
            if data.get("instance_id") != remote.instance_id:
                service.stop(source="connection_lost")
                service.sessions.clear()
                service.access.cancel_all()
            else:
                # Recheck the native desktop on the next call after reconnecting.
                service._unready = None
        elif event == "stopped" and data.get("capability") == "computer_use":
            service.stop(data.get("control_id"), source="double_escape")

    async def _retire(self, remote: _Remote) -> None:
        try:
            await remote.service.close()
            await remote.target.drain()
        except Exception:
            self.local.api.logger.exception("Computer Use removed-desktop cleanup failed")

    def pending_inputs(self) -> list[dict[str, Any]]:
        return [
            request for remote in self._remotes.values() for request in remote.service.access.list()
        ]

    async def respond(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        identifier = arguments.get("request_id")
        for remote in self._remotes.values():
            if any(item["id"] == identifier for item in remote.service.access.list()):
                return await remote.service.respond(arguments)
        return None

    async def control(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        control_id = arguments.get("control_id")
        if arguments.get("action") == "stop" and control_id is not None:
            for remote in self._remotes.values():
                if remote.service._control_id == control_id:
                    return await remote.service.control(arguments)
            return None
        for computer_id, remote in self._remotes.items():
            result = await remote.service.control({"action": "status"})
            if result["active"]:
                return {**result, "computer_id": computer_id, "computer_name": remote.target.name}
        return None

    async def run_end(self, context: Any, **kwargs: Any) -> None:
        for remote in tuple(self._remotes.values()):
            await remote.service.run_end(context, **kwargs)

    async def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        for remote in tuple(self._remotes.values()):
            await remote.service.close()
            await remote.target.drain()
        self._remotes.clear()
        if self._closing:
            await asyncio.gather(*tuple(self._closing))
