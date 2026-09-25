"""Pushes from the Desktop into the loaded page: Live voice requests and Voice updates.

Two window events carry them:

- ``vbot-desktop-live`` (cancelable), ``detail``
  ``{action: "start" | "toggle", source: "wakeword" | "hotkey"}``: a wake phrase
  with the Live voice action or the global hotkey asks the page to start or
  toggle a Live voice call. The page acknowledges with ``preventDefault()``.
- ``vbot-desktop-voice``, ``detail`` ``{type: "status", status}`` or
  ``{type: "event", event}``: Voice status snapshots and events (see
  :mod:`desktop.wakeword.controller`).

``Window.evaluate_js`` blocks until the page answers and deadlocks on the GUI
thread, so one daemon thread (``vbot-desktop-page-events``) delivers every push
in order from one queue. Producers never block:

- Live requests: at most :data:`MAX_PENDING_LIVE_REQUESTS` wait; more are
  dropped, and a request older than :data:`MAX_LIVE_REQUEST_AGE_SECONDS` at
  delivery is dropped, so a call never starts long after the user asked;
- Voice events: at most :data:`MAX_PENDING_VOICE_EVENTS` wait; the oldest is
  dropped (the page notices the sequence gap and reads the status again);
- Voice status: at most one snapshot waits; a newer one replaces its content
  where it waits.

A page without handlers (the connection screen) ignores the events.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("vbot.desktop.page_events")

LIVE_REQUEST_EVENT = "vbot-desktop-live"
VOICE_PUSH_EVENT = "vbot-desktop-voice"
LIVE_REQUEST_ACTIONS = frozenset({"start", "toggle"})
LIVE_REQUEST_SOURCES = frozenset({"wakeword", "hotkey"})
MAX_PENDING_LIVE_REQUESTS = 4
MAX_LIVE_REQUEST_AGE_SECONDS = 5.0
MAX_PENDING_VOICE_EVENTS = 64
_CLOSE_TIMEOUT_SECONDS = 1.0


def live_request_script(action: str, source: str) -> str:
    """Return JavaScript that dispatches one Live request and reports whether it was handled.

    The event is cancelable: the page calls ``preventDefault()`` to acknowledge
    it, so ``dispatchEvent`` returning ``false`` means handled.
    """
    detail = json.dumps({"action": action, "source": source})
    return (
        f"!window.dispatchEvent(new CustomEvent({json.dumps(LIVE_REQUEST_EVENT)}, "
        f"{{cancelable: true, detail: {detail}}}))"
    )


def voice_push_script(detail: Mapping[str, Any]) -> str:
    """Return JavaScript that dispatches one Voice push with ``detail``."""
    return (
        f"window.dispatchEvent(new CustomEvent({json.dumps(VOICE_PUSH_EVENT)}, "
        f"{{detail: {json.dumps(detail)}}}))"
    )


@dataclass
class _LiveRequest:
    action: str
    source: str
    requested_at: float


@dataclass
class _VoicePush:
    detail: dict[str, Any]

    @property
    def is_status(self) -> bool:
        return self.detail.get("type") == "status"


class PageEventDispatcher:
    """Deliver Live voice requests and Voice pushes to the window's page off the GUI thread.

    Implements the Voice event sink (``publish_status`` / ``publish_event``).
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._condition = threading.Condition()
        self._window: Any = None
        self._queue: deque[_LiveRequest | _VoicePush] = deque()
        self._pending_status: _VoicePush | None = None
        self._live_count = 0
        self._event_count = 0
        self._thread: threading.Thread | None = None
        self._closed = False
        self._failing = False

    def attach_window(self, window: Any) -> None:
        """Bind the pywebview window whose page receives the pushes."""
        with self._condition:
            self._window = window

    def request_live(self, action: str, source: str) -> None:
        """Queue one Live voice request; invalid or excess requests are dropped."""
        if action not in LIVE_REQUEST_ACTIONS or source not in LIVE_REQUEST_SOURCES:
            logger.warning(
                "Ignoring invalid Live voice request (action=%s, source=%s)", action, source
            )
            return
        with self._condition:
            if self._closed:
                return
            if self._live_count >= MAX_PENDING_LIVE_REQUESTS:
                logger.info("Dropping Live voice request; earlier requests are still pending")
                return
            self._live_count += 1
            self._enqueue_locked(_LiveRequest(action, source, self._clock()))
        logger.info("Live voice requested (action=%s, source=%s)", action, source)

    def publish_status(self, status: Mapping[str, Any]) -> None:
        """Queue a Voice status snapshot, replacing one that still waits."""
        detail = {"type": "status", "status": dict(status)}
        with self._condition:
            if self._closed:
                return
            if self._pending_status is not None:
                self._pending_status.detail = detail
                return
            push = _VoicePush(detail)
            self._pending_status = push
            self._enqueue_locked(push)

    def publish_event(self, event: Mapping[str, Any]) -> None:
        """Queue a Voice event; the oldest waiting event is dropped beyond the bound."""
        with self._condition:
            if self._closed:
                return
            self._event_count += 1
            self._enqueue_locked(_VoicePush({"type": "event", "event": dict(event)}))
            if self._event_count > MAX_PENDING_VOICE_EVENTS:
                oldest = next(
                    item
                    for item in self._queue
                    if isinstance(item, _VoicePush) and not item.is_status
                )
                self._queue.remove(oldest)
                self._event_count -= 1
                logger.debug("Dropping the oldest Voice event; the page is not keeping up")

    def close(self) -> None:
        """Stop delivering; waiting pushes are discarded."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
            self._pending_status = None
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(_CLOSE_TIMEOUT_SECONDS)

    def _enqueue_locked(self, item: _LiveRequest | _VoicePush) -> None:
        self._queue.append(item)
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="vbot-desktop-page-events", daemon=True
            )
            self._thread.start()
        self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or bool(self._queue))
                if self._closed:
                    return
                item = self._queue.popleft()
                window = self._window
                if isinstance(item, _LiveRequest):
                    self._live_count -= 1
                elif item is self._pending_status:
                    self._pending_status = None
                else:
                    self._event_count -= 1
                detail = dict(item.detail) if isinstance(item, _VoicePush) else None
            if isinstance(item, _LiveRequest):
                self._deliver_live(window, item)
            elif detail is not None:
                self._deliver_voice(window, detail)

    def _deliver_live(self, window: Any, request: _LiveRequest) -> None:
        if self._clock() - request.requested_at > MAX_LIVE_REQUEST_AGE_SECONDS:
            logger.info(
                "Dropping stale Live voice request (action=%s, source=%s)",
                request.action,
                request.source,
            )
            return
        if window is None:
            logger.debug("Dropping Live voice request; no window is attached")
            return
        handled = self._evaluate(window, live_request_script(request.action, request.source))
        if handled is not True:
            logger.debug(
                "Live voice request was not handled by the page (action=%s, source=%s)",
                request.action,
                request.source,
            )

    def _deliver_voice(self, window: Any, detail: dict[str, Any]) -> None:
        if window is None:
            return
        try:
            script = voice_push_script(detail)
        except (TypeError, ValueError):
            logger.exception("Voice push is not serializable; dropping it")
            return
        self._evaluate(window, script)

    def _evaluate(self, window: Any, script: str) -> Any:
        try:
            result = window.evaluate_js(script)
        except Exception:
            # One warning per failure streak: a closing window fails every push.
            if not self._failing:
                logger.warning("A Desktop push could not reach the page", exc_info=True)
            self._failing = True
            return None
        self._failing = False
        return result
