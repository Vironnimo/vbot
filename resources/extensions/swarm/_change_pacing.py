"""Change pacing: each published Swarm change reaches clients at most once a second.

Every Board post, delivery acknowledgment and Run step changes what the open
Swarm page shows; in a busy Swarm that is several changes a second, and each
publication is an event to every connected client. The first change of a
resource and id is published at once and opens a window of ``INTERVAL_SECONDS``;
further changes of it within the window are published once when the window
closes, with the highest revision among them, which opens the next window. No
change is dropped, and none is published more than one interval late. The page
refreshes at most once a second anyway (``ui/pageRefresh.js``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

# Zero publishes every change at once.
INTERVAL_SECONDS = 1.0


@dataclass
class _Window:
    timer: asyncio.TimerHandle | None = None
    # The highest revision changed within the window; ``None`` while nothing did.
    revision: int | None = None


class ChangePacing:
    """Publish ``(resource, id, revision)`` changes, at most one per interval and key."""

    def __init__(self, publish: Callable[[str, str, int], None]) -> None:
        self._publish = publish
        self._windows: dict[tuple[str, str], _Window] = {}

    def changed(self, resource: str, record_id: str, revision: int = 0) -> None:
        if INTERVAL_SECONDS <= 0:
            self._publish(resource, record_id, revision)
            return
        window = self._windows.get((resource, record_id))
        if window is None:
            self._open((resource, record_id), revision)
        elif window.revision is None or revision > window.revision:
            window.revision = revision

    def close(self) -> None:
        """Drop unpublished changes: a retiring registration refreshes every page itself."""
        for window in self._windows.values():
            if window.timer is not None:
                window.timer.cancel()
        self._windows.clear()

    def _open(self, key: tuple[str, str], revision: int) -> None:
        window = _Window()
        self._windows[key] = window
        window.timer = asyncio.get_running_loop().call_later(
            INTERVAL_SECONDS, self._close_window, key, window
        )
        self._publish(key[0], key[1], revision)

    def _close_window(self, key: tuple[str, str], window: _Window) -> None:
        if self._windows.get(key) is not window:
            return
        del self._windows[key]
        if window.revision is not None:
            self._open(key, window.revision)


__all__ = ["INTERVAL_SECONDS", "ChangePacing"]
