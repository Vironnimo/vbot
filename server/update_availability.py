"""The update check of the packaged installation whose server this process is.

The installation owns the check (``cli.application.availability``); its
server follows it so that the WebUI can say when a newer vBot version is
published, also for a Linux installation without a tray. Every
``CHECK_TICK_S`` the watch reads the stored answer and asks the update channel
when that answer is due; a changed answer publishes
``resource_changed(kind="application")``, and the WebUI reads
``application.update_status`` again. A development checkout, or a server the
installation does not record as its own, has no installation to follow.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli.application import availability
from cli.application.state import ApplicationError, Installation, discover
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("server.update_availability")
_WORKERS = BoundedWorkerPool(name="update-availability", max_workers=1)

#: How often the server looks at the stored answer; the channel itself is asked
#: only when that answer is due (``availability.CHECK_INTERVAL``).
CHECK_TICK_S = 15 * 60.0

JsonObject = dict[str, Any]


def owning_installation(data_dir: Path) -> Installation | None:
    """The packaged installation that records this data directory's server, if any."""

    try:
        install = discover()
    except ApplicationError, OSError:
        return None
    if (
        install is None
        or not install.owns_server
        or not install.server_data_directory
        or Path(install.server_data_directory).resolve() != data_dir.resolve()
    ):
        return None
    return install


class UpdateAvailabilityWatch:
    """Follow the installation's update check and announce a changed answer."""

    def __init__(
        self,
        install: Installation | None,
        publish: Callable[[], None],
        *,
        tick_s: float = CHECK_TICK_S,
    ) -> None:
        self._install = install
        self._publish = publish
        self._tick_s = tick_s
        self._last: JsonObject | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._install is not None and self._task is None:
            self._task = asyncio.create_task(self._follow(), name="update-availability")

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def status(self) -> JsonObject:
        """The stored answer, read fresh: the tray may have checked meanwhile.

        ``installed`` is false when no installation records this server; the
        other fields then stay empty.
        """

        install = self._install
        if install is None:
            return {"installed": False, "available": None, "checked_at": None, "channel": None}
        stored = await _WORKERS.run(availability.read, install)
        if stored is None:
            return {"installed": True, "available": None, "checked_at": None, "channel": None}
        projection = stored.projection()
        projection.pop("error")
        return {"installed": True, **projection}

    async def _follow(self) -> None:
        install = self._install
        assert install is not None
        while True:
            try:
                await _WORKERS.run(availability.refresh, install)
                current = await self.status()
                if current != self._last:
                    self._last = current
                    self._publish()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Could not follow the update check")
            await asyncio.sleep(self._tick_s)
