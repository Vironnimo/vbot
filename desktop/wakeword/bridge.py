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

import base64
import binascii
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from desktop.settings import read_wakeword_settings, write_wakeword_settings
from desktop.wakeword.engine import (
    DEFAULT_WAKEWORD_SENSITIVITY,
    MAX_ACTIVE_WAKEWORD_MODELS,
    MAX_CUSTOM_WAKEWORD_MODEL_BYTES,
    MAX_WAKEWORD_SENSITIVITY,
    MIN_WAKEWORD_SENSITIVITY,
    WakewordModelCatalog,
    WakewordModelError,
)

if TYPE_CHECKING:
    from desktop.connection import PreparedConnection, ServerEntry

logger = logging.getLogger("vbot.desktop.wakeword.bridge")

_MAX_CUSTOM_WAKEWORD_MODEL_BASE64_CHARS = 4 * ((MAX_CUSTOM_WAKEWORD_MODEL_BYTES + 2) // 3)


class ConnectionDelegate(Protocol):
    """The server-selection surface the bridge delegates connection calls to.

    Declared structurally so the bridge stays decoupled from the concrete
    :class:`desktop.connection.ConnectionController` (and so tests can pass a
    lightweight double). The controller satisfies this interface.
    """

    def prepare_connect(self, host: str, port: Any, label: str | None = ...) -> PreparedConnection:
        """Probe and persist a target without replacing the calling document."""

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
        "microphone",
    ]
)

_SERVER_PROFILE_KEYS = frozenset(["target_agent_id", "session_behavior"])
_WAKEWORD_EVENT_HISTORY_LIMIT = 24
_CALIBRATION_TIMEOUT_SECONDS = 5 * 60
_CALIBRATION_NOISE_SECONDS = 3.0
_CALIBRATION_REQUIRED_SAMPLES = 3
_CALIBRATION_RELEASE_FRAMES = 2
_CALIBRATION_NOISE_PERCENTILE = 0.95
_CALIBRATION_NOISE_MARGIN = 0.02
_CALIBRATION_PHRASE_MARGIN = 0.02
_CALIBRATION_THRESHOLD_GAP_RATIO = 0.4
_CALIBRATION_SENSITIVITY_STEP = 0.05


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
        model_catalog: Any = None,
        speech_readiness_checker: Callable[[str], str | None] | None = None,
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
        self._model_catalog = (
            model_catalog if model_catalog is not None else WakewordModelCatalog(settings_path)
        )
        self._speech_readiness_checker = speech_readiness_checker
        self._state = _WAKEWORD_STATE_OFF
        self._error_code: str | None = None
        self._active_microphone: dict[str, Any] | None = None
        self._event_sequence = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=_WAKEWORD_EVENT_HISTORY_LIMIT)
        self._calibration_active = False
        self._calibration_deadline = 0.0
        self._calibration_scores: dict[str, float] = {}
        self._calibration_peaks: dict[str, float] = {}
        self._calibration_phase: str | None = None
        self._calibration_noise_deadline = 0.0
        self._calibration_model_ids: tuple[str, ...] = ()
        self._calibration_noise_samples: dict[str, list[float]] = {}
        self._calibration_noise_levels: dict[str, float] = {}
        self._calibration_sample_peaks: dict[str, list[float]] = {}
        self._calibration_recommendations: dict[str, float] = {}
        self._calibration_target_index = 0
        self._calibration_candidate_peak = 0.0
        self._calibration_release_frames = 0
        self._calibration_sample_armed = False
        self._lock = threading.Lock()
        # Import validation can load a full TFLite model. Serialize catalog I/O
        # separately so status polling and worker state never wait on it.
        self._model_lock = threading.Lock()
        # Server-selection calls mutate shared on-disk state and navigate the
        # single window; a dedicated lock serializes them across pywebview
        # threads without coupling to the wakeword config lock (a different
        # invariant, held while reading settings during status polls).
        self._connection_lock = threading.Lock()
        self._status_event = threading.Event()

    # -- Capabilities --------------------------------------------------------

    def getDesktopCapabilities(self) -> dict[str, bool]:  # noqa: N802
        """Return desktop-only feature flags for the WebUI feature gates."""
        return {"wakeword": True, "serverSelection": True}

    # -- Status polling ------------------------------------------------------

    def getWakewordStatus(self) -> dict[str, Any]:  # noqa: N802
        """Return current wakeword configuration and live worker state."""
        with self._lock:
            config = self._worker_config_locked()
            state = self._state
            error_code = self._error_code
            active_microphone = dict(self._active_microphone) if self._active_microphone else None
            events = [dict(event) for event in self._events]
            calibration = self._calibration_status_locked()
        return {
            "enabled": config.get("enabled", False),
            "state": state,
            "engine": "pyopen_wakeword",
            "microphone": config.get("microphone"),
            "active_model_ids": config["active_model_ids"],
            "model_sensitivities": config["model_sensitivities"],
            "target_agent_id": config.get("target_agent_id"),
            "session_behavior": config.get("session_behavior", "active"),
            "error_code": error_code,
            "active_microphone": active_microphone,
            "events": events,
            # Runtime-only fields (not editable config): the mock flag lets the
            # WebUI warn when detection is not really running.
            "mock": self._mock,
            "mode": self._mode,
            "calibration": calibration,
        }

    def listMicrophones(self) -> list[dict[str, Any]]:  # noqa: N802
        """Return available input devices and whether Voice can use them."""
        from desktop.wakeword.worker import list_microphones

        return list_microphones()

    def listWakewordModels(self) -> list[dict[str, Any]]:  # noqa: N802
        """Return curated built-ins and Desktop-local imported models."""
        with self._model_lock:
            return [model.to_dict() for model in self._model_catalog.list_models()]

    def importWakewordModel(self, filename: str, content_base64: str) -> dict[str, Any]:  # noqa: N802
        """Validate, persist, and optionally activate a base64 TFLite model."""
        if not isinstance(content_base64, str):
            raise WakewordModelError("Wakeword model content must be base64 text")
        if len(content_base64) > _MAX_CUSTOM_WAKEWORD_MODEL_BASE64_CHARS:
            raise WakewordModelError("Wakeword model exceeds the import size limit")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WakewordModelError("Wakeword model content is not valid base64") from exc
        with self._model_lock:
            descriptor = self._model_catalog.import_model(filename, content)
        activated = False
        with self._lock:
            current = read_wakeword_settings(self._settings_path)
            active_model_ids = list(current["active_model_ids"])
            if len(active_model_ids) < MAX_ACTIVE_WAKEWORD_MODELS:
                active_model_ids.append(descriptor.id)
                current["active_model_ids"] = active_model_ids
                write_wakeword_settings(current, self._settings_path)
                activated = True
            enabled = bool(current.get("enabled", False))
        if activated:
            self._restart_worker(enabled)
        return {**descriptor.to_dict(), "activated": activated}

    def deleteWakewordModel(self, model_id: str) -> dict[str, bool]:  # noqa: N802
        """Permanently remove an inactive imported wakeword model."""
        if not isinstance(model_id, str) or not model_id.strip():
            raise WakewordModelError("Wakeword model id must be a non-empty string")
        model_id = model_id.strip()
        with self._lock:
            current = read_wakeword_settings(self._settings_path)
            if model_id in current["active_model_ids"]:
                raise WakewordModelError("The active wakeword model cannot be removed")
            with self._model_lock:
                self._model_catalog.delete_model(model_id)
            sensitivities = current.get("model_sensitivities")
            if isinstance(sensitivities, dict) and model_id in sensitivities:
                del sensitivities[model_id]
                current["model_sensitivities"] = sensitivities
                write_wakeword_settings(current, self._settings_path)
        return {"deleted": True}

    # -- Actions from WebUI --------------------------------------------------

    def setWakewordEnabled(self, enabled: bool) -> dict[str, Any]:  # noqa: N802
        """Enable or disable wakeword listening."""
        enabled = bool(enabled)
        if enabled and not self._mock and self._speech_readiness_checker is not None:
            readiness_error = self._speech_readiness_checker(self.server_url)
            if readiness_error is not None:
                logger.warning("Wakeword activation rejected (reason=%s)", readiness_error)
                self.publish_state(_WAKEWORD_STATE_ERROR, readiness_error)
                return {"enabled": False, "error_code": readiness_error}
        with self._lock:
            config = read_wakeword_settings(self._settings_path)
            config["enabled"] = enabled
            write_wakeword_settings(config, self._settings_path)
        if enabled:
            self._start_worker()
        else:
            self._stop_worker()
            self.publish_state(_WAKEWORD_STATE_OFF)
        return {"enabled": enabled, "error_code": None}

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
            if "active_model_ids" in config:
                active_model_ids = _validated_active_model_ids(config["active_model_ids"])
                with self._model_lock:
                    for model_id in active_model_ids:
                        self._model_catalog.resolve(model_id)
                current["active_model_ids"] = active_model_ids
                changed = True
            if "model_sensitivities" in config:
                sensitivity_updates = _validated_model_sensitivities(config["model_sensitivities"])
                with self._model_lock:
                    for model_id in sensitivity_updates:
                        self._model_catalog.resolve(model_id)
                sensitivities = current.get("model_sensitivities")
                if not isinstance(sensitivities, dict):
                    sensitivities = {}
                sensitivities.update(sensitivity_updates)
                current["model_sensitivities"] = sensitivities
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
        self._restart_worker(enabled)

    def retryWakeword(self) -> None:  # noqa: N802
        """Rebuild and restart the worker after a visible recoverable error."""
        with self._lock:
            enabled = bool(read_wakeword_settings(self._settings_path).get("enabled", False))
        if not enabled:
            return
        self._stop_worker()
        self._worker = None
        if self._mode == "real":
            from desktop.wakeword.worker import refresh_microphone_devices

            refresh_microphone_devices()
        self._start_worker()

    def startWakewordCalibration(self) -> dict[str, Any]:  # noqa: N802
        """Pause command activation and expose raw per-model detector scores."""
        with self._lock:
            config = self._worker_config_locked()
            if not config.get("enabled", False):
                raise RuntimeError("Wakeword listening must be enabled for calibration")
            if self._mode != "real":
                raise RuntimeError("Wakeword calibration requires the real Voice stack")
            if self._state != _WAKEWORD_STATE_LISTENING:
                raise RuntimeError("Wakeword calibration requires the listener to be ready")
            if self._worker is None or not self._worker.is_running():
                raise RuntimeError("Wakeword calibration requires a running listener")
            model_ids = config["active_model_ids"]
            self._start_calibration_locked(model_ids)
        logger.info("Wakeword calibration started")
        return self.getWakewordStatus()

    def stopWakewordCalibration(self) -> dict[str, Any]:  # noqa: N802
        """Resume normal command activation and clear transient score data."""
        stopped = self._end_calibration()
        if stopped:
            logger.info("Wakeword calibration stopped")
        return self.getWakewordStatus()

    def restartWakewordCalibration(self) -> dict[str, Any]:  # noqa: N802
        """Restart ambient-noise measurement and discard captured repetitions."""
        with self._lock:
            if not self._calibration_active_locked():
                raise RuntimeError("Wakeword calibration is not active")
            self._start_calibration_locked(list(self._calibration_model_ids))
        logger.info("Wakeword calibration restarted")
        return self.getWakewordStatus()

    # -- Connection (server selection) ---------------------------------------

    def connect(self, host: str, port: Any) -> dict[str, str]:
        """Connect the window to a server (called by the shell connection screen).

        The connection screen's JavaScript calls
        ``window.pywebview.api.connect(host, port)``; this delegates to the
        controller, which probes and persists the target without navigating.
        JavaScript awaits this method's payload before it navigates or renders
        the inline error, keeping pywebview's return callback alive until the
        Promise is resolved.
        """
        controller = self._require_connection()
        with self._connection_lock:
            prepared = controller.prepare_connect(host, _coerce_port(port))
        return prepared.to_bridge_payload()

    def listServers(self) -> list[dict[str, Any]]:  # noqa: N802
        """Return remembered servers and identify the window's active target."""
        controller = self._require_connection()
        with self._connection_lock:
            servers = controller.list_servers()
        active_server_url = self.server_url
        return [
            _server_to_payload(entry, active=_server_url(entry) == active_server_url)
            for entry in servers
        ]

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
        """Prepare a remembered server connection for bridge-safe navigation."""
        controller = self._require_connection()
        with self._connection_lock:
            prepared = controller.prepare_connect(host, _coerce_port(port))
        return prepared.to_bridge_payload()

    # -- Worker state callbacks ----------------------------------------------

    def publish_state(self, state: str, error_code: str | None = None) -> None:
        """Update the live worker state for WebUI status polling."""
        if state not in _VALID_STATES:
            raise ValueError(f"Invalid wakeword state: {state}")
        with self._lock:
            previous_state = self._state
            previous_error_code = self._error_code
            if state != _WAKEWORD_STATE_LISTENING:
                self._clear_calibration_locked()
            self._state = state
            self._error_code = error_code if state == _WAKEWORD_STATE_ERROR else None
            self._event_sequence += 1
            event_sequence = self._event_sequence
            self._events.append(
                {
                    "sequence": event_sequence,
                    "state": state,
                    "error_code": self._error_code,
                }
            )
            current_error_code = self._error_code
        self._status_event.set()
        if state == previous_state and current_error_code == previous_error_code:
            return
        if current_error_code is None:
            logger.info(
                "Voice state changed (sequence=%s, from=%s, to=%s)",
                event_sequence,
                previous_state,
                state,
            )
            return
        logger.info(
            "Voice state changed (sequence=%s, from=%s, to=%s, error_code=%s)",
            event_sequence,
            previous_state,
            state,
            current_error_code,
        )

    def publish_runtime_details(self, *, active_microphone: dict[str, Any] | None) -> None:
        """Publish the concrete input device selected by automatic routing."""
        with self._lock:
            self._active_microphone = (
                dict(active_microphone) if active_microphone is not None else None
            )

    def publish_calibration_scores(self, scores: dict[str, float]) -> None:
        """Advance guided calibration with one raw detector-score frame."""
        with self._lock:
            if not self._calibration_active_locked():
                return
            now = time.monotonic()
            self._advance_calibration_phase_locked(now)
            for model_id in self._calibration_scores:
                score = max(0.0, min(1.0, float(scores.get(model_id, 0.0))))
                self._calibration_scores[model_id] = score
                self._calibration_peaks[model_id] = max(
                    self._calibration_peaks[model_id],
                    score,
                )
                if self._calibration_phase == "noise":
                    self._calibration_noise_samples[model_id].append(score)
            if self._calibration_phase == "phrases":
                self._capture_calibration_sample_locked()

    def wakeword_calibration_active(self) -> bool:
        """Return whether detections must remain non-operative for calibration."""
        with self._lock:
            return self._calibration_active_locked()

    def _set_mode(self, mode: str) -> None:
        """Publish the lazily resolved local Voice implementation mode."""
        if mode not in {"real", "mock", "unavailable"}:
            raise ValueError(f"Invalid wakeword mode: {mode}")
        with self._lock:
            self._mode = mode
            if mode != "real":
                self._clear_calibration_locked()

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
            try:
                self._worker = self._worker_factory(self)
            except WakewordModelError as exc:
                logger.warning("Wakeword model is unavailable: %s", exc)
                self.publish_state(_WAKEWORD_STATE_ERROR, exc.error_code)
                return
            except Exception:
                logger.warning("Failed to create wakeword worker", exc_info=True)
                self.publish_state(_WAKEWORD_STATE_ERROR, "engine_start_failed")
                return
        if self._worker is None:
            self.publish_state(_WAKEWORD_STATE_ERROR)
            return
        self._worker.start()

    def _stop_worker(self) -> None:
        self._end_calibration()
        if self._worker:
            self._worker.stop()

    def worker_config(self) -> dict[str, Any]:
        """Return the global Voice config plus this server's safe routing profile."""
        with self._lock:
            return self._worker_config_locked()

    def _create_wakeword_engine(self) -> Any:
        """Create one detector serving every active model."""
        with self._lock:
            config = self._worker_config_locked()
            with self._model_lock:
                return self._model_catalog.create_engine(
                    config["active_model_ids"],
                    config["model_sensitivities"],
                    score_listener=self.publish_calibration_scores,
                )

    def _worker_config_locked(self) -> dict[str, Any]:
        config = read_wakeword_settings(self._settings_path)
        active_model_ids = config["active_model_ids"]
        sensitivities = config.get("model_sensitivities")
        normalized_sensitivities: dict[str, float] = {}
        for model_id in active_model_ids:
            sensitivity = (
                sensitivities.get(model_id, DEFAULT_WAKEWORD_SENSITIVITY)
                if isinstance(sensitivities, dict)
                else DEFAULT_WAKEWORD_SENSITIVITY
            )
            if isinstance(sensitivity, bool) or not isinstance(sensitivity, (int, float)):
                sensitivity = DEFAULT_WAKEWORD_SENSITIVITY
            normalized_sensitivities[model_id] = max(
                MIN_WAKEWORD_SENSITIVITY,
                min(MAX_WAKEWORD_SENSITIVITY, float(sensitivity)),
            )
        config["active_model_ids"] = list(active_model_ids)
        config["model_sensitivities"] = normalized_sensitivities
        profiles = config.get("server_profiles")
        profile = profiles.get(self._server_url, {}) if isinstance(profiles, dict) else {}
        if not isinstance(profile, dict):
            profile = {}
        config["target_agent_id"] = profile.get("target_agent_id")
        config["session_behavior"] = profile.get("session_behavior", "active")
        return config

    def _restart_worker(self, enabled: bool) -> None:
        """Rebuild a running worker or start a newly enabled configuration."""
        if self._worker and self._worker.is_running():
            self._stop_worker()
            self._worker = None
            if enabled:
                self._start_worker()
        elif enabled:
            self._worker = None
            self._start_worker()

    def _calibration_status_locked(self) -> dict[str, Any]:
        active = self._calibration_active_locked()
        now = time.monotonic()
        noise_seconds_remaining = (
            max(0, math.ceil(self._calibration_noise_deadline - now))
            if active and self._calibration_phase == "noise"
            else 0
        )
        target_model_id = (
            self._calibration_model_ids[self._calibration_target_index]
            if active
            and self._calibration_phase == "phrases"
            and self._calibration_target_index < len(self._calibration_model_ids)
            else None
        )
        return {
            "active": active,
            "phase": self._calibration_phase if active else None,
            "scores": dict(self._calibration_scores) if active else {},
            "peaks": dict(self._calibration_peaks) if active else {},
            "noise_levels": dict(self._calibration_noise_levels) if active else {},
            "sample_counts": (
                {
                    model_id: len(self._calibration_sample_peaks.get(model_id, []))
                    for model_id in self._calibration_model_ids
                }
                if active
                else {}
            ),
            "required_samples": _CALIBRATION_REQUIRED_SAMPLES,
            "target_model_id": target_model_id,
            "recommended_sensitivities": (
                dict(self._calibration_recommendations) if active else {}
            ),
            "noise_seconds_remaining": noise_seconds_remaining,
        }

    def _calibration_active_locked(self) -> bool:
        if self._calibration_active:
            now = time.monotonic()
            if now >= self._calibration_deadline:
                self._clear_calibration_locked()
            else:
                self._advance_calibration_phase_locked(now)
        return self._calibration_active

    def _clear_calibration_locked(self) -> None:
        self._calibration_active = False
        self._calibration_deadline = 0.0
        self._calibration_scores = {}
        self._calibration_peaks = {}
        self._calibration_phase = None
        self._calibration_noise_deadline = 0.0
        self._calibration_model_ids = ()
        self._calibration_noise_samples = {}
        self._calibration_noise_levels = {}
        self._calibration_sample_peaks = {}
        self._calibration_recommendations = {}
        self._calibration_target_index = 0
        self._calibration_candidate_peak = 0.0
        self._calibration_release_frames = 0
        self._calibration_sample_armed = False

    def _end_calibration(self) -> bool:
        with self._lock:
            was_active = self._calibration_active
            self._clear_calibration_locked()
        return was_active

    def _start_calibration_locked(self, model_ids: list[str]) -> None:
        now = time.monotonic()
        self._calibration_active = True
        self._calibration_deadline = now + _CALIBRATION_TIMEOUT_SECONDS
        self._calibration_phase = "noise"
        self._calibration_noise_deadline = now + _CALIBRATION_NOISE_SECONDS
        self._calibration_model_ids = tuple(model_ids)
        self._calibration_scores = dict.fromkeys(model_ids, 0.0)
        self._calibration_peaks = dict.fromkeys(model_ids, 0.0)
        self._calibration_noise_samples = {model_id: [] for model_id in model_ids}
        self._calibration_noise_levels = {}
        self._calibration_sample_peaks = {model_id: [] for model_id in model_ids}
        self._calibration_recommendations = {}
        self._calibration_target_index = 0
        self._reset_calibration_candidate_locked()

    def _advance_calibration_phase_locked(self, now: float) -> None:
        if (
            not self._calibration_active
            or self._calibration_phase != "noise"
            or now < self._calibration_noise_deadline
        ):
            return
        self._calibration_noise_levels = {
            model_id: _percentile(samples, _CALIBRATION_NOISE_PERCENTILE)
            for model_id, samples in self._calibration_noise_samples.items()
        }
        self._calibration_phase = "phrases"
        self._reset_calibration_candidate_locked()

    def _capture_calibration_sample_locked(self) -> None:
        if self._calibration_target_index >= len(self._calibration_model_ids):
            return
        model_id = self._calibration_model_ids[self._calibration_target_index]
        score = self._calibration_scores[model_id]
        noise_level = self._calibration_noise_levels.get(model_id, 0.0)
        signal_gate = _calibration_signal_gate(noise_level)
        release_level = min(
            signal_gate * 0.9,
            max(
                noise_level + (_CALIBRATION_NOISE_MARGIN / 2),
                signal_gate * 0.6,
            ),
        )

        if self._calibration_candidate_peak > 0.0:
            self._calibration_candidate_peak = max(self._calibration_candidate_peak, score)
            if score < release_level:
                self._calibration_release_frames += 1
                if self._calibration_release_frames >= _CALIBRATION_RELEASE_FRAMES:
                    self._record_calibration_sample_locked(
                        model_id,
                        self._calibration_candidate_peak,
                    )
            else:
                self._calibration_release_frames = 0
            return

        if not self._calibration_sample_armed:
            if score < release_level:
                self._calibration_release_frames += 1
                if self._calibration_release_frames >= _CALIBRATION_RELEASE_FRAMES:
                    self._calibration_sample_armed = True
                    self._calibration_release_frames = 0
            else:
                self._calibration_release_frames = 0
            return

        if score >= signal_gate:
            self._calibration_candidate_peak = score
            self._calibration_sample_armed = False
            self._calibration_release_frames = 0

    def _record_calibration_sample_locked(self, model_id: str, peak: float) -> None:
        samples = self._calibration_sample_peaks[model_id]
        samples.append(peak)
        self._reset_calibration_candidate_locked()
        if len(samples) < _CALIBRATION_REQUIRED_SAMPLES:
            return

        self._calibration_recommendations[model_id] = _recommended_sensitivity(
            self._calibration_noise_levels.get(model_id, 0.0),
            samples,
        )
        self._calibration_target_index += 1
        if self._calibration_target_index >= len(self._calibration_model_ids):
            self._calibration_phase = "ready"
            logger.info(
                "Wakeword calibration completed (models=%s)",
                ",".join(self._calibration_model_ids),
            )

    def _reset_calibration_candidate_locked(self) -> None:
        self._calibration_candidate_peak = 0.0
        self._calibration_release_frames = 0
        self._calibration_sample_armed = False

    def _require_connection(self) -> ConnectionDelegate:
        if self._connection is None:
            raise RuntimeError("DesktopBridge has no connection controller attached")
        return self._connection


def _percentile(values: list[float], percentile: float) -> float:
    """Return a linearly interpolated percentile without a numeric dependency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + ((ordered[upper_index] - ordered[lower_index]) * fraction)


def _recommended_sensitivity(noise_level: float, phrase_peaks: list[float]) -> float:
    """Place a quantized threshold safely between noise and the weakest phrase."""
    weakest_phrase = min(phrase_peaks)
    separation = max(0.0, weakest_phrase - noise_level)
    minimum_threshold = 1.0 - MAX_WAKEWORD_SENSITIVITY
    maximum_threshold = 1.0 - MIN_WAKEWORD_SENSITIVITY
    minimum_reliable_threshold = max(
        minimum_threshold,
        noise_level + _CALIBRATION_NOISE_MARGIN,
    )
    maximum_reliable_threshold = min(
        maximum_threshold,
        weakest_phrase - _CALIBRATION_PHRASE_MARGIN,
    )
    target_threshold = noise_level + (separation * _CALIBRATION_THRESHOLD_GAP_RATIO)
    supported_thresholds = [
        round(step * _CALIBRATION_SENSITIVITY_STEP, 2)
        for step in range(1, 20)
        if minimum_reliable_threshold - 1e-9
        <= round(step * _CALIBRATION_SENSITIVITY_STEP, 2)
        <= maximum_reliable_threshold + 1e-9
    ]
    if not supported_thresholds:
        threshold = max(
            minimum_threshold,
            min(maximum_threshold, maximum_reliable_threshold),
        )
    else:
        threshold = min(
            supported_thresholds,
            key=lambda candidate: (abs(candidate - target_threshold), -candidate),
        )
    return round(1.0 - threshold, 2)


def _calibration_signal_gate(noise_level: float) -> float:
    """Require a phrase peak above the first supported noise-safe threshold."""
    minimum_threshold = 1.0 - MAX_WAKEWORD_SENSITIVITY
    noise_safe_threshold = max(
        minimum_threshold,
        noise_level + _CALIBRATION_NOISE_MARGIN,
    )
    quantized_threshold = (
        math.ceil((noise_safe_threshold - 1e-9) / _CALIBRATION_SENSITIVITY_STEP)
        * _CALIBRATION_SENSITIVITY_STEP
    )
    return min(1.0, quantized_threshold + _CALIBRATION_PHRASE_MARGIN)


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


def _server_to_payload(entry: ServerEntry, *, active: bool | None = None) -> dict[str, Any]:
    """Render a remembered-server entry as a JSON-serializable bridge payload."""

    payload = entry.to_storage()
    if active is not None:
        payload["active"] = active
    return payload


def _server_url(entry: ServerEntry) -> str:
    """Build the normalized Desktop URL used for active-target comparison."""

    return f"http://{entry.host}:{entry.port}"


def _validated_config_value(key: str, value: Any) -> Any:
    """Validate the small Desktop-local Voice config surface at the bridge."""
    if key == "sensitivity":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Voice sensitivity must be a number between 0 and 1")
        numeric = float(value)
        if not MIN_WAKEWORD_SENSITIVITY <= numeric <= MAX_WAKEWORD_SENSITIVITY:
            raise ValueError(
                f"Voice sensitivity must be between {MIN_WAKEWORD_SENSITIVITY} "
                f"and {MAX_WAKEWORD_SENSITIVITY}"
            )
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
    raise ValueError(f"Invalid Voice setting: {key}")


def _validated_active_model_ids(value: Any) -> list[str]:
    """Validate the ordered one-to-two model selection from JavaScript."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ACTIVE_WAKEWORD_MODELS:
        raise WakewordModelError(
            f"Choose between 1 and {MAX_ACTIVE_WAKEWORD_MODELS} wakeword models"
        )
    model_ids: list[str] = []
    for model_id in value:
        if not isinstance(model_id, str) or not model_id.strip():
            raise WakewordModelError("Wakeword model ids must be non-empty strings")
        model_ids.append(model_id.strip())
    if len(set(model_ids)) != len(model_ids):
        raise WakewordModelError("Active wakeword models must be unique")
    return model_ids


def _validated_model_sensitivities(value: Any) -> dict[str, float]:
    """Validate keyed sensitivity updates from JavaScript."""
    if not isinstance(value, dict):
        raise ValueError("Voice model sensitivities must be an object")
    sensitivities: dict[str, float] = {}
    for model_id, sensitivity in value.items():
        if not isinstance(model_id, str) or not model_id.strip():
            raise WakewordModelError("Wakeword model ids must be non-empty strings")
        sensitivities[model_id.strip()] = _validated_config_value("sensitivity", sensitivity)
    return sensitivities
