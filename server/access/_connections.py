"""Open WebSockets and SSE streams per device, so that revoking a device closes them."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(eq=False)
class LiveConnection:
    """One long-lived request of a device: a WebSocket or an SSE response."""

    device_id: str
    task: asyncio.Task[object] | None
    revoked: bool = False
    _cancels: int = field(default=0, repr=False)

    def settle(self) -> None:
        """Withdraw the cancellations the revocation requested, once it is handled."""
        if self.task is not None:
            while self._cancels and self.task.cancelling():
                self.task.uncancel()
                self._cancels -= 1
        self._cancels = 0


class LiveConnections:
    """The registry the access guard keeps and device revocation consults."""

    def __init__(self) -> None:
        self._by_device: dict[str, set[LiveConnection]] = {}

    def register(self, device_id: str) -> LiveConnection:
        connection = LiveConnection(device_id, asyncio.current_task())
        self._by_device.setdefault(device_id, set()).add(connection)
        return connection

    def unregister(self, connection: LiveConnection) -> None:
        connections = self._by_device.get(connection.device_id)
        if connections is not None:
            connections.discard(connection)
            if not connections:
                del self._by_device[connection.device_id]

    def revoke(self, device_ids: set[str]) -> int:
        """End every open connection of these devices; returns how many there were."""
        count = 0
        for device_id in device_ids:
            for connection in list(self._by_device.pop(device_id, ())):
                count += 1
                connection.revoked = True
                if connection.task is not None and not connection.task.done():
                    connection.task.cancel()
                    connection._cancels += 1
        return count

    def count(self, device_id: str) -> int:
        return len(self._by_device.get(device_id, ()))
