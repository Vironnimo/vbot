"""Microphones."""

from __future__ import annotations

from typing import Any

from desktop.wakeword._audio_capture import (
    CaptureFormat,
    MicrophoneUnavailableError,
)
from desktop.wakeword._worker_constants import (
    _AUDIO_BACKEND_LOCK,
    _CAPTURE_DTYPES,
    _CHANNELS,
    _COMMON_CAPTURE_SAMPLE_RATES,
    _SAMPLE_RATE,
    logger,
)


def _host_api_name(sd: Any, info: Any) -> str:
    """Return a stable host-API label for one sounddevice descriptor."""
    host_api_index = info.get("hostapi")
    if host_api_index is None:
        return ""
    try:
        host_api = sd.query_hostapis(host_api_index)
    except Exception:
        return str(host_api_index)
    name = host_api.get("name") if isinstance(host_api, dict) else None
    return str(name) if name else str(host_api_index)


def _candidate_device_indices(sd: Any, requested_device: dict[str, Any] | None) -> list[int]:
    """Return requested/default/fallback input devices in safe preference order."""
    devices = sd.query_devices()
    if requested_device is not None:
        requested_index = requested_device["index"]
        requested_name = requested_device["name"]
        requested_host_api = requested_device["host_api"]
        matches = [
            index
            for index, info in enumerate(devices)
            if int(info.get("max_input_channels", 0)) > 0
            and str(info.get("name", f"Device {index}")) == requested_name
            and _host_api_name(sd, info) == requested_host_api
        ]
        if requested_index in matches:
            return [requested_index]
        return matches if len(matches) == 1 else []

    candidates: list[int] = []
    try:
        default_input = int(sd.default.device[0])
    except (IndexError, TypeError, ValueError):
        default_input = -1
    if default_input >= 0:
        candidates.append(default_input)
    for host_api in sd.query_hostapis():
        host_default = host_api.get("default_input_device", -1)
        if isinstance(host_default, int) and host_default >= 0:
            candidates.append(host_default)
    candidates.extend(
        index for index, info in enumerate(devices) if int(info.get("max_input_channels", 0)) > 0
    )
    return list(dict.fromkeys(candidates))


def _capture_format_for_device(sd: Any, device: int) -> CaptureFormat | None:
    """Find the best native format that can be normalized to 16 kHz mono PCM."""
    try:
        info = sd.query_devices(device)
    except Exception:
        return None
    if int(info.get("max_input_channels", 0)) <= 0:
        return None

    default_rate = int(info.get("default_samplerate", 0) or 0)
    sample_rates = list(dict.fromkeys([_SAMPLE_RATE, default_rate, *_COMMON_CAPTURE_SAMPLE_RATES]))
    for sample_rate in sample_rates:
        if sample_rate < _SAMPLE_RATE:
            continue
        for dtype in _CAPTURE_DTYPES:
            try:
                sd.check_input_settings(
                    device=device,
                    samplerate=sample_rate,
                    channels=_CHANNELS,
                    dtype=dtype,
                )
            except Exception:
                continue
            return CaptureFormat(
                device=device,
                name=str(info.get("name", f"Device {device}")),
                sample_rate=sample_rate,
                dtype=dtype,
                host_api=_host_api_name(sd, info),
            )
    return None


def _select_capture_format(sd: Any, requested_device: dict[str, Any] | None) -> CaptureFormat:
    """Select a usable requested or automatic input format."""
    for device in _candidate_device_indices(sd, requested_device):
        capture_format = _capture_format_for_device(sd, device)
        if capture_format is not None:
            return capture_format
    raise MicrophoneUnavailableError("No input device supports Voice capture")


def list_microphones() -> list[dict[str, Any]]:
    """Enumerate input devices and surface Voice-format compatibility."""
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except ImportError:
        return []

    devices: list[dict[str, Any]] = []
    try:
        with _AUDIO_BACKEND_LOCK:
            for i, info in enumerate(sd.query_devices()):
                if int(info.get("max_input_channels", 0)) > 0:
                    capture_format = _capture_format_for_device(sd, i)
                    devices.append(
                        {
                            "index": i,
                            "name": info.get("name", f"Device {i}"),
                            "host_api": _host_api_name(sd, info),
                            "default_sample_rate": int(
                                info.get("default_samplerate", _SAMPLE_RATE)
                            ),
                            "supported": capture_format is not None,
                            "capture_sample_rate": (
                                capture_format.sample_rate if capture_format is not None else None
                            ),
                        }
                    )
    except Exception:
        logger.warning("Failed to enumerate microphones", exc_info=True)
    return devices


def refresh_microphone_devices() -> bool:
    """Reinitialize PortAudio so a retry sees devices connected after startup."""
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except ImportError:
        return False

    terminate = getattr(sd, "_terminate", None)
    initialize = getattr(sd, "_initialize", None)
    if not callable(terminate) or not callable(initialize):
        logger.warning("sounddevice does not expose PortAudio device refresh hooks")
        return False

    try:
        with _AUDIO_BACKEND_LOCK:
            terminate()
            initialize()
    except Exception:
        logger.warning("Failed to refresh microphone devices", exc_info=True)
        return False
    return True
