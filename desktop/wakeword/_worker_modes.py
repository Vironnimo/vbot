"""Worker modes."""

from __future__ import annotations

import threading
from typing import Any

from desktop.wakeword._worker_constants import (
    _MOCK_DEFAULT_SCORES,
    _MOCK_FRAME_SECONDS,
    _MOCK_STAGE_SECONDS,
    logger,
)
from desktop.wakeword._worker_support import (
    _sleep_while_running,
)
from desktop.wakeword.engine import MockWakewordEngine


class UnavailableWakewordWorker:
    """Stable non-simulating worker used when the local Voice stack is absent."""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    def start(self) -> None:
        self._bridge.publish_state("error", "voice_stack_unavailable")

    def stop(self) -> None:
        return

    def is_running(self) -> bool:
        return False


class MockWakewordWorker:
    """No-microphone worker used when the real wakeword stack is unavailable.

    Drives the *same* detection → recording → transcribing → sending state cycle
    the real worker publishes, but from a :class:`MockWakewordEngine` score script
    instead of a live microphone, and with no network calls. This lets the WebUI
    status indicator be validated with ``--mock-wakeword`` (and makes the mock
    fallback visibly "alive" rather than frozen on ``listening``).
    """

    def __init__(self, bridge: Any, engine: Any = None) -> None:
        self._bridge = bridge
        self._engine = engine if engine is not None else MockWakewordEngine(_MOCK_DEFAULT_SCORES)
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> None:
        """Start the simulated state loop without opening audio devices."""
        if self.is_running():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the simulated state loop."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def stop_recording(self) -> None:
        """No-op: the mock has no real capture to end (keeps the bridge contract)."""

    def is_running(self) -> bool:
        """True while the mock loop thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        try:
            self._engine.start()
        except Exception:
            logger.warning("Mock wakeword engine failed to start", exc_info=True)
        self._bridge.publish_state("listening")
        while self._running.is_set():
            _sleep_while_running(self._running, _MOCK_FRAME_SECONDS)
            if not self._running.is_set():
                break
            match = self._engine.detect(b"")
            if match is not None:
                self._simulate_cycle()
                if self._running.is_set():
                    self._bridge.publish_state("listening")

    def _simulate_cycle(self) -> None:
        """Publish one full post-detection state sequence with brief dwells."""
        for state in ("wakeword_detected", "recording", "transcribing", "sending", "sent"):
            if not self._running.is_set():
                return
            self._bridge.publish_state(state)
            _sleep_while_running(self._running, _MOCK_STAGE_SECONDS)
