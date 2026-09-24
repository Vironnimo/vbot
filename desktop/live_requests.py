"""Hands-free Live voice requests pushed from the Desktop into the WebUI page.

A wakeword model with the Live voice action and the global hotkey both ask the
loaded WebUI to start (or toggle) Live voice. The page owns the call, so the
Desktop only announces the request as a ``vbot-desktop-live`` window event::

    {detail: {action: "start" | "toggle", source: "wakeword" | "hotkey"}}

Pushing avoids the bridge poll interval and the timer throttling of a minimized
window. ``Window.evaluate_js`` blocks until the page answers and deadlocks on
the GUI thread, so delivery runs on one dedicated daemon thread behind a small
bounded queue: producers (the wakeword worker, the hotkey thread) never block,
and a request that cannot be delivered promptly is dropped instead of starting
a call long after the user asked. A page without a Live voice handler (the
connection screen) leaves the event unhandled; that is logged at debug level.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("vbot.desktop.live_requests")

LIVE_REQUEST_EVENT = "vbot-desktop-live"
LIVE_REQUEST_ACTIONS = frozenset({"start", "toggle"})
LIVE_REQUEST_SOURCES = frozenset({"wakeword", "hotkey"})
_MAX_PENDING_REQUESTS = 4
_MAX_REQUEST_AGE_SECONDS = 5.0


def live_request_script(action: str, source: str) -> str:
    """Return JavaScript that dispatches one request and reports whether it was handled.

    The event is cancelable: the WebUI handler calls ``preventDefault()`` to
    acknowledge it, so ``dispatchEvent`` returning ``false`` means handled.
    """

    detail = json.dumps({"action": action, "source": source})
    return (
        f"!window.dispatchEvent(new CustomEvent({json.dumps(LIVE_REQUEST_EVENT)}, "
        f"{{cancelable: true, detail: {detail}}}))"
    )


class LiveRequestDispatcher:
    """Deliver Live voice requests to the window's page off the GUI thread."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._window: Any = None
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, str, float] | None] = queue.Queue(
            maxsize=_MAX_PENDING_REQUESTS
        )
        self._thread: threading.Thread | None = None
        self._closed = False

    def attach_window(self, window: Any) -> None:
        """Bind the pywebview window whose page receives requests."""

        with self._lock:
            self._window = window

    def request(self, action: str, source: str) -> None:
        """Queue one request without blocking; invalid or excess requests are dropped."""

        if action not in LIVE_REQUEST_ACTIONS or source not in LIVE_REQUEST_SOURCES:
            logger.warning(
                "Ignoring invalid Live voice request (action=%s, source=%s)", action, source
            )
            return
        with self._lock:
            if self._closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="vbot-live-requests",
                    daemon=True,
                )
                self._thread.start()
            try:
                self._queue.put_nowait((action, source, self._clock()))
            except queue.Full:
                logger.info("Dropping Live voice request; earlier requests are still pending")
                return
        logger.info("Live voice requested (action=%s, source=%s)", action, source)

    def close(self) -> None:
        """Stop delivering; pending requests are discarded."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue.put(None)
        thread.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._deliver(*item)

    def _deliver(self, action: str, source: str, requested_at: float) -> None:
        if self._clock() - requested_at > _MAX_REQUEST_AGE_SECONDS:
            logger.info("Dropping stale Live voice request (action=%s, source=%s)", action, source)
            return
        with self._lock:
            window = self._window
        if window is None:
            logger.debug("Dropping Live voice request; no window is attached")
            return
        try:
            handled = window.evaluate_js(live_request_script(action, source))
        except Exception:
            logger.warning("Live voice request could not reach the page", exc_info=True)
            return
        if handled is not True:
            logger.debug(
                "Live voice request was not handled by the page (action=%s, source=%s)",
                action,
                source,
            )
