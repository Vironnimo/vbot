"""Microphone discovery, capture-format selection and the PortAudio backend lock.

Voice opens every sounddevice (PortAudio) input stream through
:func:`open_input_stream`, which serializes backend access behind
:data:`AUDIO_BACKEND_LOCK` and counts the open streams: re-initializing
PortAudio (:func:`refresh_microphone_devices`) would invalidate every open
stream, so a refresh is skipped while one is open.

Device identity is the stored ``{index, name, host_api}`` triple: the stored
index is used only while name and host API still match, so a reordered device
list never routes audio to a recycled index. Exclusive-mode host APIs
(WDM-KS) are never used because they lock every other client, including a Live
voice call's microphone in the WebView, out of the device.

Import cost: this module never imports numpy or sounddevice at import time;
callers pass the sounddevice module (or a test double) as ``sd``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("vbot.desktop.wakeword.microphones")

AUDIO_BACKEND_LOCK = threading.Lock()
"""Serializes every PortAudio call that queries, opens, closes or refreshes devices."""

MIN_CAPTURE_SAMPLE_RATE = 16000
"""Lowest native rate Voice accepts (the detection rate)."""

_COMMON_CAPTURE_SAMPLE_RATES = (48000, 44100, 32000, 16000)
_CAPTURE_DTYPES = ("int16", "float32")
CAPTURE_CHANNELS = 1

# WDM-KS opens kernel-streaming capture exclusively and can block every other
# client, including a Live voice call's microphone in the WebView. The shared
# mode host APIs (WASAPI, MME, DirectSound) expose the same hardware.
_EXCLUSIVE_HOST_APIS = frozenset({"Windows WDM-KS"})

_open_stream_count = 0  # guarded by AUDIO_BACKEND_LOCK


class MicrophoneUnavailableError(RuntimeError):
    """No usable input-device format could supply Voice-quality audio."""


@dataclass(frozen=True)
class CaptureFormat:
    """Concrete device format used before conversion to mono int16."""

    device: int
    name: str
    sample_rate: int
    dtype: str
    host_api: str = ""

    def to_status(self) -> dict[str, Any]:
        """Return the status form ``{index, name, host_api, sample_rate}``."""
        return {
            "index": self.device,
            "name": self.name,
            "host_api": self.host_api,
            "sample_rate": self.sample_rate,
        }


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


def _is_shared_input(sd: Any, info: Any) -> bool:
    """Return whether a device captures audio without locking out other clients."""
    return (
        int(info.get("max_input_channels", 0)) > 0
        and _host_api_name(sd, info) not in _EXCLUSIVE_HOST_APIS
    )


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
            if _is_shared_input(sd, info)
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
    candidates.extend(range(len(devices)))
    return [
        index
        for index in dict.fromkeys(candidates)
        if 0 <= index < len(devices) and _is_shared_input(sd, devices[index])
    ]


def _capture_format_for_device(sd: Any, device: int) -> CaptureFormat | None:
    """Find the best native format that can be normalized to mono int16.

    The device's own rate comes first: audio is resampled once, by Voice, and
    the command recording keeps the bandwidth the device delivers.
    """
    try:
        info = sd.query_devices(device)
    except Exception:
        return None
    if int(info.get("max_input_channels", 0)) <= 0:
        return None

    default_rate = int(info.get("default_samplerate", 0) or 0)
    sample_rates = list(dict.fromkeys([default_rate, *_COMMON_CAPTURE_SAMPLE_RATES]))
    for sample_rate in sample_rates:
        if sample_rate < MIN_CAPTURE_SAMPLE_RATE:
            continue
        for dtype in _CAPTURE_DTYPES:
            try:
                sd.check_input_settings(
                    device=device,
                    samplerate=sample_rate,
                    channels=CAPTURE_CHANNELS,
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


def open_input_stream(
    sd: Any,
    requested_device: dict[str, Any] | None,
) -> tuple[Any, CaptureFormat]:
    """Select a format, open and start a blocking input stream for it.

    ``requested_device`` is the stored ``{index, name, host_api}`` selection or
    ``None`` for automatic selection. PortAudio chooses the host buffer size;
    callers read any frame count. Raises :class:`MicrophoneUnavailableError` when no device
    fits, or the backend's own error when opening fails. The stream counts as
    open until :func:`close_input_stream`.
    """
    global _open_stream_count
    with AUDIO_BACKEND_LOCK:
        capture_format = _select_capture_format(sd, requested_device)
        stream = sd.InputStream(
            samplerate=capture_format.sample_rate,
            channels=CAPTURE_CHANNELS,
            dtype=capture_format.dtype,
            blocksize=0,
            device=capture_format.device,
        )
        _open_stream_count += 1
        try:
            stream.start()
        except BaseException:
            _open_stream_count -= 1
            _close_quietly(stream)
            raise
    return stream, capture_format


def close_input_stream(stream: Any) -> None:
    """Stop and close a stream from :func:`open_input_stream`; never raises."""
    global _open_stream_count
    with AUDIO_BACKEND_LOCK:
        _open_stream_count = max(0, _open_stream_count - 1)
        try:
            stream.stop()
        except Exception:
            logger.debug("Microphone stream did not stop cleanly", exc_info=True)
        _close_quietly(stream)


def _close_quietly(stream: Any) -> None:
    try:
        stream.close()
    except Exception:
        logger.debug("Microphone stream did not close cleanly", exc_info=True)


def _import_sounddevice() -> Any:
    try:
        import sounddevice  # type: ignore[import-untyped]
    except ImportError:
        return None
    return sounddevice


def list_microphones(sd: Any = None) -> list[dict[str, Any]]:
    """Enumerate shared-mode input devices and whether Voice can use them.

    ``sd`` is the sounddevice module (imported on demand when ``None``); an
    absent backend yields an empty list.
    """
    if sd is None:
        sd = _import_sounddevice()
        if sd is None:
            return []

    devices: list[dict[str, Any]] = []
    try:
        with AUDIO_BACKEND_LOCK:
            for i, info in enumerate(sd.query_devices()):
                if _is_shared_input(sd, info):
                    capture_format = _capture_format_for_device(sd, i)
                    devices.append(
                        {
                            "index": i,
                            "name": info.get("name", f"Device {i}"),
                            "host_api": _host_api_name(sd, info),
                            "default_sample_rate": int(
                                info.get("default_samplerate", MIN_CAPTURE_SAMPLE_RATE)
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


def refresh_microphone_devices(sd: Any = None) -> bool:
    """Reinitialize PortAudio so a retry sees devices connected after startup.

    Skipped (``False``) while any Voice input stream is open, because a
    re-initialization would invalidate it, and when the backend offers no
    refresh hooks.
    """
    if sd is None:
        sd = _import_sounddevice()
        if sd is None:
            return False

    terminate = getattr(sd, "_terminate", None)
    initialize = getattr(sd, "_initialize", None)
    if not callable(terminate) or not callable(initialize):
        logger.warning("sounddevice does not expose PortAudio device refresh hooks")
        return False

    try:
        with AUDIO_BACKEND_LOCK:
            if _open_stream_count > 0:
                logger.debug("Skipping the microphone refresh while a stream is open")
                return False
            terminate()
            initialize()
    except Exception:
        logger.warning("Failed to refresh microphone devices", exc_info=True)
        return False
    return True
