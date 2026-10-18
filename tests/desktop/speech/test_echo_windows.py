"""WASAPI callback formats, overflow reporting and PortAudio resource ownership."""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest

from desktop.speech import _echo_windows as windows


class FakePyAudioStream:
    def __init__(self, **options: Any) -> None:
        self.options = options
        self.stopped = False
        self.closed = False

    def is_active(self) -> bool:
        return not self.stopped

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakePyAudio:
    instances: list[FakePyAudio] = []
    loopback: dict[str, Any] | Exception = {}

    def __init__(self) -> None:
        self.terminated = False
        self.stream: FakePyAudioStream | None = None
        FakePyAudio.instances.append(self)

    def get_default_wasapi_loopback(self) -> dict[str, Any]:
        if isinstance(FakePyAudio.loopback, Exception):
            raise FakePyAudio.loopback
        return FakePyAudio.loopback

    def open(self, **options: Any) -> FakePyAudioStream:
        self.stream = FakePyAudioStream(**options)
        return self.stream

    def terminate(self) -> None:
        self.terminated = True


@pytest.fixture
def fake_pyaudiowpatch(monkeypatch: pytest.MonkeyPatch) -> type[FakePyAudio]:
    FakePyAudio.instances = []
    FakePyAudio.loopback = {
        "index": 24,
        "name": "Speakers [Loopback]",
        "defaultSampleRate": 44100.0,
        "maxInputChannels": 2,
    }
    module = types.ModuleType("pyaudiowpatch")
    module.PyAudio = FakePyAudio  # type: ignore[attr-defined]
    module.paFloat32 = 1  # type: ignore[attr-defined]
    module.paContinue = 0  # type: ignore[attr-defined]
    module.paInputOverflow = 2  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", module)
    return FakePyAudio


def test_wasapi_loopback_opens_10ms_float_callbacks_and_times_them(
    fake_pyaudiowpatch: type[FakePyAudio],
) -> None:
    arrival_time = 1005.0

    def clock() -> float:
        return arrival_time

    backend = windows.WasapiLoopbackBackend(clock=clock)
    blocks: list[tuple[Any, ...]] = []

    stream = backend.open_default(lambda *block: blocks.append(block))

    audio = fake_pyaudiowpatch.instances[0]
    assert audio.stream is not None
    options = audio.stream.options
    assert options["frames_per_buffer"] == 441 and options["rate"] == 44100
    assert options["input_device_index"] == 24 and options["channels"] == 2
    assert stream.name.startswith("Speakers (44100 Hz")
    callback = options["stream_callback"]
    samples = np.arange(8, dtype=np.float32)
    assert callback(samples.tobytes(), 4, {}, 2) == (None, 0)
    block, rate, arrival, discontinuity = blocks[0]
    assert block.shape == (4, 2) and rate == 44100 and arrival == arrival_time and discontinuity
    assert callback(samples.tobytes(), 3, {}, 0) == (None, 0)  # malformed size: dropped
    assert len(blocks) == 1

    stream.close()
    assert audio.stream.stopped and audio.stream.closed and audio.terminated


def test_wasapi_loopback_callback_survives_a_failing_sink(
    fake_pyaudiowpatch: type[FakePyAudio],
) -> None:
    def failing(*_block: Any) -> None:
        raise RuntimeError("sink broke")

    backend = windows.WasapiLoopbackBackend()
    backend.open_default(failing)
    stream = fake_pyaudiowpatch.instances[0].stream
    assert stream is not None
    callback = stream.options["stream_callback"]

    assert callback(np.zeros(4, np.float32).tobytes(), 2, {}, 0) == (None, 0)


def test_wasapi_loopback_without_an_endpoint_terminates_portaudio(
    fake_pyaudiowpatch: type[FakePyAudio],
) -> None:
    fake_pyaudiowpatch.loopback = LookupError("no analogue")

    with pytest.raises(LookupError):
        windows.WasapiLoopbackBackend().open_default(lambda *_block: None)
    assert fake_pyaudiowpatch.instances[0].terminated
