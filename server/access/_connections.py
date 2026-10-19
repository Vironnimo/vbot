"""Open requests per device, so that revoking a device ends or refuses them."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(eq=False)
class LiveConnection:
    """One request of a device, registered while the access guard runs it.

    Revocation cancels every request, including one waiting for its body or for
    mutation admission. Owners settle already-admitted durable work before
    propagating cancellation; the guard then refuses any pending answer.
    """

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
        """End or refuse every open request of these devices; returns how many there were.

        The request that revokes runs in the current task and still answers: a
        device that logs itself out gets its confirmation.
        """
        current = _current_task()
        count = 0
        for device_id in device_ids:
            for connection in list(self._by_device.pop(device_id, ())):
                if current is not None and connection.task is current:
                    continue
                count += 1
                connection.revoked = True
                task = connection.task
                if task is not None and not task.done():
                    task.cancel()
                    connection._cancels += 1
        return count

    def count(self, device_id: str) -> int:
        return len(self._by_device.get(device_id, ()))


def _current_task() -> asyncio.Task[object] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None
