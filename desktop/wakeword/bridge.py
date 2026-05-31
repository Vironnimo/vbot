"""Desktop↔WebUI bridge API for wakeword and voice features.

The DesktopBridge is passed to pywebview as `js_api` so the WebUI can
call its methods from JavaScript via `window.pywebview.api.<method>()`.
All methods return plain Python objects serializable to JSON.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from desktop.main import read_wakeword_settings, write_wakeword_settings

_WAKEWORD_STATE_OFF = "off"
_WAKEWORD_STATE_LISTENING = "listening"
_WAKEWORD_STATE_DETECTED = "wakeword_detected"
_WAKEWORD_STATE_RECORDING = "recording"
_WAKEWORD_STATE_TRANSCRIBING = "transcribing"
_WAKEWORD_STATE_SENDING = "sending"
_WAKEWORD_STATE_ERROR = "error"

_VALID_STATES = frozenset(
    [
        _WAKEWORD_STATE_OFF,
        _WAKEWORD_STATE_LISTENING,
        _WAKEWORD_STATE_DETECTED,
        _WAKEWORD_STATE_RECORDING,
        _WAKEWORD_STATE_TRANSCRIBING,
        _WAKEWORD_STATE_SENDING,
        _WAKEWORD_STATE_ERROR,
    ]
)

_KNOWN_WAKEWORD_KEYS = frozenset(
    [
        "enabled",
        "engine",
        "microphone",
        "sensitivity",
        "target_agent_id",
        "session_behavior",
        "wake_phrase",
    ]
)


class DesktopBridge:
    """Bridge API exposed to the WebUI via pywebview `js_api`.

    Thread-safe: config access is protected by a lock, and worker
    control signals use threading.Event for cross-thread coordination.
    """

    def __init__(
        self,
        *,
        settings_path: Path | None = None,
        worker: Any = None,
        worker_factory: Callable[[DesktopBridge], Any] | None = None,
    ) -> None:
        self._settings_path = settings_path
        self._worker = worker
        self._worker_factory = worker_factory
        self._state = _WAKEWORD_STATE_OFF
        self._lock = threading.Lock()
        self._status_event = threading.Event()

    # -- Capabilities --------------------------------------------------------

    def getDesktopCapabilities(self) -> dict[str, bool]:  # noqa: N802
        """Return desktop-only feature flags for the WebUI feature gates."""
        return {"wakeword": True}

    # -- Status polling ------------------------------------------------------

    def getWakewordStatus(self) -> dict[str, Any]:  # noqa: N802
        """Return current wakeword configuration and live worker state."""
        with self._lock:
            config = read_wakeword_settings(self._settings_path)
            state = self._state
        return {
            "enabled": config.get("enabled", False),
            "state": state,
            "engine": config.get("engine", "openwakeword"),
            "microphone": config.get("microphone"),
            "sensitivity": config.get("sensitivity", 0.5),
            "target_agent_id": config.get("target_agent_id"),
            "session_behavior": config.get("session_behavior", "active"),
            "wake_phrase": config.get("wake_phrase", "hey_jarvis"),
        }

    # -- Actions from WebUI --------------------------------------------------

    def setWakewordEnabled(self, enabled: bool) -> None:  # noqa: N802
        """Enable or disable wakeword listening."""
        enabled = bool(enabled)
        with self._lock:
            config = read_wakeword_settings(self._settings_path)
            config["enabled"] = enabled
            write_wakeword_settings(config, self._settings_path)
        if enabled:
            self._start_worker()
        else:
            self._stop_worker()
            self.publish_state(_WAKEWORD_STATE_OFF)

    def setWakewordConfig(self, config: dict[str, Any]) -> None:  # noqa: N802
        """Apply a partial wakeword configuration update from the WebUI."""
        if not isinstance(config, dict):
            return
        with self._lock:
            current = read_wakeword_settings(self._settings_path)
            changed = False
            for key in _KNOWN_WAKEWORD_KEYS:
                if key in config:
                    current[key] = config[key]
                    changed = True
            if not changed:
                return
            write_wakeword_settings(current, self._settings_path)
            enabled = bool(current.get("enabled", False))
        if self._worker and self._worker.is_running():
            self._stop_worker()
            self._worker = None
            if enabled:
                self._start_worker()
        elif enabled:
            self._worker = None
            self._start_worker()

    # -- Worker state callbacks ----------------------------------------------

    def publish_state(self, state: str) -> None:
        """Update the live worker state for WebUI status polling."""
        if state not in _VALID_STATES:
            raise ValueError(f"Invalid wakeword state: {state}")
        with self._lock:
            self._state = state
        self._status_event.set()

    # -- Internal ------------------------------------------------------------

    def _start_worker(self) -> None:
        if self._worker is None and self._worker_factory is not None:
            self._worker = self._worker_factory(self)
        if self._worker is None:
            self.publish_state(_WAKEWORD_STATE_ERROR)
            return
        self._worker.start()

    def _stop_worker(self) -> None:
        if self._worker:
            self._worker.stop()
