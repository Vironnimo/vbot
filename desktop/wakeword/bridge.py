"""Desktop↔WebUI bridge API for wakeword and voice features.

The DesktopBridge is passed to pywebview as `js_api` so the WebUI can
call its methods from JavaScript via `window.pywebview.api.<method>()`.
All methods return plain Python objects serializable to JSON.

The *same* bridge instance stays the window's single `js_api` across
`Window.load_url` navigation, so it must serve both callers: the shell
connection screen (which calls the connection methods to list/select/add/
remove/connect servers) and the remote WebUI (which calls the wakeword
methods). The connection methods delegate to the injected
``ConnectionController``; the bridge owns no server-selection logic itself.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from desktop.settings import read_wakeword_settings, write_wakeword_settings

if TYPE_CHECKING:
    from desktop.connection import DesktopProbeResult, ServerEntry


class ConnectionDelegate(Protocol):
    """The server-selection surface the bridge delegates connection calls to.

    Declared structurally so the bridge stays decoupled from the concrete
    :class:`desktop.connection.ConnectionController` (and so tests can pass a
    lightweight double). The controller satisfies this interface.
    """

    def connect(self, host: str, port: Any, label: str | None = ...) -> DesktopProbeResult:
        """Probe and navigate to a target, or show the connection screen."""

    def switch_to(self, host: str, port: Any, label: str | None = ...) -> DesktopProbeResult:
        """Connect to a chosen remembered/typed server."""

    def add_server(self, host: str, port: Any, label: str | None = ...) -> ServerEntry:
        """Remember a server without connecting."""

    def remove_server(self, host: str, port: Any) -> bool:
        """Forget a remembered server, reporting whether one was removed."""

    def list_servers(self) -> list[ServerEntry]:
        """Return the remembered servers in stored order."""


_WAKEWORD_STATE_OFF = "off"
_WAKEWORD_STATE_STARTING = "starting"
_WAKEWORD_STATE_LISTENING = "listening"
_WAKEWORD_STATE_DETECTED = "wakeword_detected"
_WAKEWORD_STATE_RECORDING = "recording"
_WAKEWORD_STATE_TRANSCRIBING = "transcribing"
_WAKEWORD_STATE_SENDING = "sending"
_WAKEWORD_STATE_SENT = "sent"
_WAKEWORD_STATE_CANCELLED = "cancelled"
_WAKEWORD_STATE_NO_SPEECH = "no_speech"
_WAKEWORD_STATE_TRANSCRIPTION_FAILED = "transcription_failed"
_WAKEWORD_STATE_ERROR = "error"

_VALID_STATES = frozenset(
    [
        _WAKEWORD_STATE_OFF,
        _WAKEWORD_STATE_STARTING,
        _WAKEWORD_STATE_LISTENING,
        _WAKEWORD_STATE_DETECTED,
        _WAKEWORD_STATE_RECORDING,
        _WAKEWORD_STATE_TRANSCRIBING,
        _WAKEWORD_STATE_SENDING,
        _WAKEWORD_STATE_SENT,
        _WAKEWORD_STATE_CANCELLED,
        _WAKEWORD_STATE_NO_SPEECH,
        _WAKEWORD_STATE_TRANSCRIPTION_FAILED,
        _WAKEWORD_STATE_ERROR,
    ]
)

_KNOWN_WAKEWORD_KEYS = frozenset(
    [
        "enabled",
        "engine",
        "microphone",
        "sensitivity",
        "wake_phrase",
    ]
)

_SERVER_PROFILE_KEYS = frozenset(["target_agent_id", "session_behavior"])
_WAKEWORD_EVENT_HISTORY_LIMIT = 24


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
        connection: ConnectionDelegate | None = None,
        server_url: str = "",
        mock: bool = False,
        mode: str = "real",
    ) -> None:
        self._settings_path = settings_path
        self._worker = worker
        self._worker_factory = worker_factory
        self._connection = connection
        # The server the voice worker sends to. Kept on the bridge (not captured
        # once in the worker factory) so a runtime server switch — first-run
        # connect, saved-server pick, or the "Server" menu — retargets voice too.
        self._server_url = server_url.rstrip("/")
        # Mock is explicit developer/demo mode; missing local dependencies use
        # the separate unavailable mode and never simulate Voice activity.
        self._mock = bool(mock)
        self._mode = "mock" if self._mock and mode == "real" else mode
        self._state = _WAKEWORD_STATE_OFF
        self._error_code: str | None = None
        self._active_microphone: dict[str, Any] | None = None
        self._event_sequence = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=_WAKEWORD_EVENT_HISTORY_LIMIT)
        self._lock = threading.Lock()
        # Server-selection calls mutate shared on-disk state and navigate the
        # single window; a dedicated lock serializes them across pywebview
        # threads without coupling to the wakeword config lock (a different
        # invariant, held while reading settings during status polls).
        self._connection_lock = threading.Lock()
        self._status_event = threading.Event()

    # -- Capabilities --------------------------------------------------------

    def getDesktopCapabilities(self) -> dict[str, bool]:  # noqa: N802
        """Return desktop-only feature flags for the WebUI feature gates."""
        return {"wakeword": True}

    # -- Status polling ------------------------------------------------------

    def getWakewordStatus(self) -> dict[str, Any]:  # noqa: N802
        """Return current wakeword configuration and live worker state."""
        with self._lock:
            config = self._worker_config_locked()
            state = self._state
            error_code = self._error_code
            active_microphone = dict(self._active_microphone) if self._active_microphone else None
            events = [dict(event) for event in self._events]
        return {
            "enabled": config.get("enabled", False),
            "state": state,
            "engine": config.get("engine", "openwakeword"),
            "microphone": config.get("microphone"),
            "sensitivity": config.get("sensitivity", 0.5),
            "target_agent_id": config.get("target_agent_id"),
            "session_behavior": config.get("session_behavior", "active"),
            "wake_phrase": config.get("wake_phrase", "hey_jarvis"),
            "error_code": error_code,
            "active_microphone": active_microphone,
            "events": events,
            # Runtime-only fields (not editable config): the mock flag lets the
            # WebUI warn when detection is not really running.
            "mock": self._mock,
            "mode": self._mode,
        }

    def listMicrophones(self) -> list[dict[str, Any]]:  # noqa: N802
        """Return available input devices and whether Voice can use them."""
        from desktop.wakeword.worker import list_microphones

        return list_microphones()

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
                    current[key] = _validated_config_value(key, config[key])
                    changed = True
            profile_changes = {
                key: _validated_config_value(key, config[key])
                for key in _SERVER_PROFILE_KEYS
                if key in config
            }
            if profile_changes:
                if not self._server_url:
                    raise ValueError("Voice target settings require an active server")
                profiles = current.get("server_profiles")
                if not isinstance(profiles, dict):
                    profiles = {}
                profile = profiles.get(self._server_url)
                if not isinstance(profile, dict):
                    profile = {}
                profile.update(profile_changes)
                profiles[self._server_url] = profile
                current["server_profiles"] = profiles
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

    def retryWakeword(self) -> None:  # noqa: N802
        """Rebuild and restart the worker after a visible recoverable error."""
        with self._lock:
            enabled = bool(read_wakeword_settings(self._settings_path).get("enabled", False))
        if not enabled:
            return
        self._stop_worker()
        self._worker = None
        self._start_worker()

    # -- Connection (server selection) ---------------------------------------

    def connect(self, host: str, port: Any) -> dict[str, str]:
        """Connect the window to a server (called by the shell connection screen).

        The connection screen's JavaScript calls
        ``window.pywebview.api.connect(host, port)``; this delegates to the
        controller, which probes the target and either navigates the window to
        the WebUI or re-renders the connection screen with an inline error.
        Returns the resulting probe ``{"status": …}`` so a caller can react,
        while the visible outcome is the window navigation the controller drives.
        """
        controller = self._require_connection()
        with self._connection_lock:
            result = controller.connect(host, _coerce_port(port))
        return {"status": result.status}

    def listServers(self) -> list[dict[str, Any]]:  # noqa: N802
        """Return the remembered servers as plain dicts for the connection screen."""
        controller = self._require_connection()
        with self._connection_lock:
            servers = controller.list_servers()
        return [_server_to_payload(entry) for entry in servers]

    def addServer(  # noqa: N802
        self, host: str, port: Any, label: str | None = None
    ) -> dict[str, Any]:
        """Remember a server without connecting, returning the stored entry."""
        controller = self._require_connection()
        with self._connection_lock:
            entry = controller.add_server(host, _coerce_port(port), label or None)
        return _server_to_payload(entry)

    def removeServer(self, host: str, port: Any) -> dict[str, bool]:  # noqa: N802
        """Forget a remembered server, reporting whether one was removed."""
        controller = self._require_connection()
        with self._connection_lock:
            removed = controller.remove_server(host, _coerce_port(port))
        return {"removed": removed}

    def selectServer(self, host: str, port: Any) -> dict[str, str]:  # noqa: N802
        """Select and connect to a remembered server (one-click reconnect)."""
        controller = self._require_connection()
        with self._connection_lock:
            result = controller.switch_to(host, _coerce_port(port))
        return {"status": result.status}

    # -- Worker state callbacks ----------------------------------------------

    def publish_state(self, state: str, error_code: str | None = None) -> None:
        """Update the live worker state for WebUI status polling."""
        if state not in _VALID_STATES:
            raise ValueError(f"Invalid wakeword state: {state}")
        with self._lock:
            self._state = state
            self._error_code = error_code if state == _WAKEWORD_STATE_ERROR else None
            self._event_sequence += 1
            self._events.append(
                {
                    "sequence": self._event_sequence,
                    "state": state,
                    "error_code": self._error_code,
                }
            )
        self._status_event.set()

    def publish_runtime_details(self, *, active_microphone: dict[str, Any] | None) -> None:
        """Publish the concrete input device selected by automatic routing."""
        with self._lock:
            self._active_microphone = (
                dict(active_microphone) if active_microphone is not None else None
            )

    # -- Active server (voice target follows the window) ---------------------

    @property
    def server_url(self) -> str:
        """The base URL the voice worker sends transcripts/RPC to."""
        with self._lock:
            return self._server_url

    def set_server_url(self, url: str) -> None:
        """Point the voice worker at a new server (the window's active server).

        Wired to the connection controller so that whenever the window navigates
        to a server — first-run connect, saved-server pick, or a runtime "Server"
        menu switch — voice retargets the same server. A running worker is rebuilt
        so its in-flight target changes; a not-yet-created worker simply picks up
        the new URL from :attr:`server_url` when it next starts. A no-op when the
        URL is unchanged (so the launch auto-connect never needlessly restarts a
        worker already pointed at that server).
        """
        normalized = (url or "").rstrip("/")
        with self._lock:
            if normalized == self._server_url:
                return
            self._server_url = normalized
            enabled = bool(read_wakeword_settings(self._settings_path).get("enabled", False))
        if enabled:
            self._stop_worker()
            self._worker = None
            self._start_worker()

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

    def worker_config(self) -> dict[str, Any]:
        """Return the global Voice config plus this server's safe routing profile."""
        with self._lock:
            return self._worker_config_locked()

    def _worker_config_locked(self) -> dict[str, Any]:
        config = read_wakeword_settings(self._settings_path)
        profiles = config.get("server_profiles")
        profile = profiles.get(self._server_url, {}) if isinstance(profiles, dict) else {}
        if not isinstance(profile, dict):
            profile = {}
        config["target_agent_id"] = profile.get("target_agent_id")
        config["session_behavior"] = profile.get("session_behavior", "active")
        return config

    def _require_connection(self) -> ConnectionDelegate:
        if self._connection is None:
            raise RuntimeError("DesktopBridge has no connection controller attached")
        return self._connection


def _coerce_port(value: Any) -> int | str:
    """Coerce a JS-supplied port to an int where possible for the controller.

    The connection screen sends a parsed number, but a hand-typed value can
    arrive as a string; a numeric string becomes an int so the controller sees a
    real port. A non-numeric string (or any other type) is passed through
    unchanged so the controller's ``validate_port`` rejects it with a clear
    message rather than this helper guessing.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    # Any other type is left for the controller's validate_port to reject; cast
    # narrows the opaque value off Any without changing it.
    return cast("int | str", value)


def _server_to_payload(entry: ServerEntry) -> dict[str, Any]:
    """Render a remembered-server entry as a JSON-serializable bridge payload."""
    return entry.to_storage()


def _validated_config_value(key: str, value: Any) -> Any:
    """Validate the small Desktop-local Voice config surface at the bridge."""
    if key == "sensitivity":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Voice sensitivity must be a number between 0 and 1")
        numeric = float(value)
        if not 0.0 <= numeric <= 1.0:
            raise ValueError("Voice sensitivity must be between 0 and 1")
        return numeric
    if key == "microphone":
        if value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
            return value
        raise ValueError("Voice microphone must be a non-negative device index or null")
    if key == "target_agent_id":
        if value is None:
            return None
        if isinstance(value, str) and value.strip():
            return value.strip()
        raise ValueError("Voice target agent must be a non-empty id or null")
    if key == "session_behavior":
        if value in {"active", "new"}:
            return value
        raise ValueError("Voice session behavior must be 'active' or 'new'")
    if key == "enabled":
        return bool(value)
    if key in {"engine", "wake_phrase"} and isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"Invalid Voice setting: {key}")
