"""Worker: microphones behavior."""

from __future__ import annotations

from tests.desktop.worker_helpers import (
    no_real_neural_speech_detector as no_real_neural_speech_detector,
)
from tests.desktop.worker_helpers import (
    ready_speech_to_text as ready_speech_to_text,
)


def test_list_microphones_graceful_when_no_sounddevice(monkeypatch) -> None:
    """list_microphones should return empty list when sounddevice unavailable."""
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", None)

    from desktop.wakeword.worker import list_microphones

    devices = list_microphones()
    assert devices == []


def test_refresh_microphone_devices_reinitializes_portaudio_for_hotplug(monkeypatch) -> None:
    """A retry refresh makes a microphone connected after startup discoverable."""

    class HotplugSoundDevice:
        def __init__(self) -> None:
            self.connected_devices: list[dict[str, object]] = []
            self.cached_devices: list[dict[str, object]] = []
            self.lifecycle: list[str] = []

        def query_devices(self, device: int | None = None):
            if device is None:
                return list(self.cached_devices)
            return self.cached_devices[device]

        def check_input_settings(self, **_kwargs) -> None:
            return

        def _terminate(self) -> None:
            self.lifecycle.append("terminate")

        def _initialize(self) -> None:
            self.lifecycle.append("initialize")
            self.cached_devices = list(self.connected_devices)

    sounddevice = HotplugSoundDevice()
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", sounddevice)

    from desktop.wakeword.worker import list_microphones, refresh_microphone_devices

    assert list_microphones() == []
    sounddevice.connected_devices = [
        {
            "name": "USB microphone",
            "max_input_channels": 1,
            "default_samplerate": 48000,
        }
    ]
    assert list_microphones() == []

    assert refresh_microphone_devices() is True

    assert sounddevice.lifecycle == ["terminate", "initialize"]
    assert list_microphones() == [
        {
            "index": 0,
            "name": "USB microphone",
            "host_api": "",
            "default_sample_rate": 48000,
            "supported": True,
            "capture_sample_rate": 16000,
        }
    ]


def test_saved_microphone_identity_survives_device_reordering() -> None:
    from desktop.wakeword._microphones import (
        _candidate_device_indices,
    )

    class ReorderedSoundDevice:
        @staticmethod
        def query_devices() -> list[dict[str, object]]:
            return [
                {"name": "Webcam mic", "hostapi": 0, "max_input_channels": 1},
                {"name": "Studio mic", "hostapi": 1, "max_input_channels": 1},
            ]

        @staticmethod
        def query_hostapis(index: int) -> dict[str, object]:
            return {"name": ["WASAPI", "ASIO"][index]}

    requested = {"index": 0, "name": "Studio mic", "host_api": "ASIO"}

    assert _candidate_device_indices(ReorderedSoundDevice(), requested) == [1]


def test_saved_microphone_never_uses_recycled_index() -> None:
    from desktop.wakeword._microphones import (
        _candidate_device_indices,
    )

    class RecycledSoundDevice:
        @staticmethod
        def query_devices() -> list[dict[str, object]]:
            return [{"name": "Webcam mic", "hostapi": 0, "max_input_channels": 1}]

        @staticmethod
        def query_hostapis(_index: int) -> dict[str, object]:
            return {"name": "WASAPI"}

    requested = {"index": 0, "name": "Studio mic", "host_api": "ASIO"}

    assert _candidate_device_indices(RecycledSoundDevice(), requested) == []
