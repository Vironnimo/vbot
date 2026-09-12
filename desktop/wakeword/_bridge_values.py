"""Bridge values."""

from __future__ import annotations

import math
from statistics import median
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit

from desktop.wakeword.engine import (
    MAX_ACTIVE_WAKEWORD_MODELS,
    MAX_CUSTOM_WAKEWORD_MODEL_BYTES,
    MAX_WAKEWORD_SENSITIVITY,
    MIN_WAKEWORD_SENSITIVITY,
    WakewordModelError,
)

if TYPE_CHECKING:
    from desktop.connection import PreparedConnection, ServerEntry

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

_WAKEWORD_STATE_MICROPHONE_DISCONNECTED = "microphone_disconnected"

_WAKEWORD_STATE_ERROR = "error"

_WAKEWORD_STATES_WITH_REASON = frozenset(
    [_WAKEWORD_STATE_MICROPHONE_DISCONNECTED, _WAKEWORD_STATE_ERROR]
)

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
        _WAKEWORD_STATE_MICROPHONE_DISCONNECTED,
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

_CALIBRATION_TIMEOUT_SECONDS = 3 * 60

_CALIBRATION_NOISE_SECONDS = 3.0

_CALIBRATION_REQUIRED_SAMPLES = 5

_CALIBRATION_RELEASE_FRAMES = 2

_CALIBRATION_NOISE_PERCENTILE = 0.95

_CALIBRATION_NOISE_MARGIN = 0.02

_CALIBRATION_PHRASE_MARGIN = 0.02

_CALIBRATION_THRESHOLD_GAP_RATIO = 0.5

_CALIBRATION_SENSITIVITY_STEP = 0.05

_CALIBRATION_NOISE_WARNING_LEVEL = 0.30


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
    """Place a quantized threshold safely between noise and the median phrase."""
    reference_phrase = median(phrase_peaks) if phrase_peaks else 0.0
    separation = max(0.0, reference_phrase - noise_level)
    minimum_threshold = 1.0 - MAX_WAKEWORD_SENSITIVITY
    maximum_threshold = 1.0 - MIN_WAKEWORD_SENSITIVITY
    minimum_reliable_threshold = max(
        minimum_threshold,
        noise_level + _CALIBRATION_NOISE_MARGIN,
    )
    maximum_reliable_threshold = min(
        maximum_threshold,
        reference_phrase - _CALIBRATION_PHRASE_MARGIN,
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


def _canonical_profile_key(server_url: str) -> str:
    """Return the Voice profile key for a server URL, merging loopback aliases.

    The same local server is reachable as ``localhost`` and ``127.0.0.1``;
    profiles keyed by the raw URL spelling would lose the stored target agent
    whenever the launch host spelling changes between the two forms.
    """

    url = (server_url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if (parts.hostname or "").lower() == "localhost":
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme or 'http'}://127.0.0.1{port}"
    return url


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
        if value is None:
            return value
        if not isinstance(value, dict):
            raise ValueError("Voice microphone must be a device descriptor or null")
        index = value.get("index")
        name = value.get("name")
        host_api = value.get("host_api")
        if (
            isinstance(index, int)
            and not isinstance(index, bool)
            and index >= 0
            and isinstance(name, str)
            and name.strip()
            and isinstance(host_api, str)
        ):
            return {
                "index": index,
                "name": name.strip(),
                "host_api": host_api.strip(),
            }
        raise ValueError("Voice microphone descriptor is invalid")
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
