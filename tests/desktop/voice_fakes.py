"""Shared doubles for the Voice capture, detection, command and controller tests."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np

from desktop.wakeword.capture import AudioBlock, CaptureGap
from desktop.wakeword.engine import WakewordMatch

SPEECH_AMPLITUDE = 3000
"""Samples at or above this level count as speech for :class:`AmplitudeVad`."""


def wait_until(condition: Callable[[], bool], timeout: float = 5.0, message: str = "") -> None:
    """Poll ``condition`` until it holds; fail the test after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(message or "condition was not reached in time")
        time.sleep(0.005)


def tone(seconds: float, rate: int, *, amplitude: int = SPEECH_AMPLITUDE) -> np.ndarray:
    """A 440 Hz int16 tone the amplitude VAD judges as speech."""
    samples = np.arange(round(seconds * rate))
    return (amplitude * np.sin(2 * np.pi * 440 * samples / rate)).astype(np.int16)


def silence(seconds: float, rate: int) -> np.ndarray:
    return np.zeros(round(seconds * rate), dtype=np.int16)


# -- sounddevice ---------------------------------------------------------------------


@dataclass(frozen=True)
class Overflow:
    """Script marker: the next read reports an input overflow."""


class FakeInputStream:
    """One blocking input stream of :class:`FakeSoundDevice`."""

    def __init__(self, sd: FakeSoundDevice, *, samplerate: int, dtype: str, device: int) -> None:
        self.sd = sd
        self.samplerate = samplerate
        self.dtype = dtype
        self.device = device
        self.started = False
        self.closed = False

    @property
    def read_available(self) -> int:
        return self.sd.read_available

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True
        self.sd.log("stream.close")

    def read(self, frames: int) -> tuple[np.ndarray, bool]:
        return self.sd.read(self, frames)


class FakeSoundDevice:
    """sounddevice double: one shared-mode microphone with scripted audio.

    ``feed`` queues int16 sample arrays, :class:`Overflow` markers and
    exceptions; reads consume samples across array boundaries. Once the script
    is empty, reads return silence after ``pace`` seconds so capture threads
    run at a bounded rate instead of spinning.
    """

    def __init__(
        self,
        *,
        default_samplerate: int = 16000,
        rates: Iterable[int] | None = None,
        dtypes: Iterable[str] = ("int16", "float32"),
        pace: float = 0.01,
    ) -> None:
        self.devices: list[dict[str, Any]] = [
            {
                "name": "Mic",
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": default_samplerate,
            }
        ]
        self.host_apis: list[dict[str, Any]] = [
            {"name": "Windows WASAPI", "default_input_device": 0}
        ]
        self.default = SimpleNamespace(device=(0, -1))
        self.rates = set(rates) if rates is not None else None
        self.dtypes = set(dtypes)
        self.pace = pace
        self.read_available = 0
        self.open_failures = 0
        self.streams: list[FakeInputStream] = []
        self.events: list[str] = []
        self._lock = threading.Lock()
        self._script: deque[Any] = deque()
        self._samples = np.zeros(0, dtype=np.int16)

    # sounddevice API --------------------------------------------------------------

    def query_devices(self, device: int | None = None) -> Any:
        return list(self.devices) if device is None else self.devices[device]

    def query_hostapis(self, index: int | None = None) -> Any:
        return list(self.host_apis) if index is None else self.host_apis[index]

    def check_input_settings(
        self, *, device: int, samplerate: int, channels: int, dtype: str
    ) -> None:
        if dtype not in self.dtypes or (self.rates is not None and samplerate not in self.rates):
            raise ValueError("Invalid sample rate or format")

    def InputStream(  # noqa: N802 - mirrors sounddevice.InputStream
        self, *, samplerate: int, channels: int, dtype: str, blocksize: int, device: int
    ) -> FakeInputStream:
        with self._lock:
            if self.open_failures > 0:
                self.open_failures -= 1
                self.events.append("open_failed")
                raise OSError("Device unavailable")
            stream = FakeInputStream(self, samplerate=samplerate, dtype=dtype, device=device)
            self.streams.append(stream)
            self.events.append("stream.open")
        return stream

    def _terminate(self) -> None:
        self.log("terminate")

    def _initialize(self) -> None:
        self.log("initialize")

    # Test controls ----------------------------------------------------------------

    def log(self, event: str) -> None:
        with self._lock:
            self.events.append(event)

    def feed(self, *items: Any) -> None:
        with self._lock:
            self._script.extend(items)

    @property
    def drained(self) -> bool:
        with self._lock:
            return not self._script and not self._samples.size

    def read(self, stream: FakeInputStream, frames: int) -> tuple[np.ndarray, bool]:
        with self._lock:
            overflowed = False
            if self._script and not self._samples.size:
                head = self._script[0]
                if isinstance(head, BaseException):
                    self._script.popleft()
                    raise head
                if isinstance(head, Overflow):
                    self._script.popleft()
                    overflowed = True
            while self._samples.size < frames and self._script:
                if not isinstance(self._script[0], np.ndarray):
                    break
                self._samples = np.concatenate((self._samples, self._script.popleft()))
            scripted = self._samples.size > 0
            taken, self._samples = self._samples[:frames], self._samples[frames:]
        if not scripted:
            time.sleep(self.pace)
        block = np.zeros(frames, dtype=np.int16)
        block[: taken.size] = taken
        if stream.dtype == "float32":
            return (block.astype(np.float32) / 32768.0).reshape(-1, 1), overflowed
        return block.reshape(-1, 1), overflowed


# -- Capture subscription -----------------------------------------------------------------


class FakeSubscription:
    """A capture subscription double the test fills directly.

    ``push_audio`` cuts 16 kHz samples into 40 ms blocks; with a higher
    ``recording_rate`` the recording projection repeats each sample.
    """

    BLOCK_SAMPLES = 640

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._items: deque[Any] = deque()
        self._closed = False
        self._next_index = 0

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def empty(self) -> bool:
        with self._condition:
            return not self._items

    def read(self, timeout: float | None = None) -> Any:
        with self._condition:
            self._condition.wait_for(lambda: self._closed or bool(self._items), timeout)
            if self._closed or not self._items:
                return None
            return self._items.popleft()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def blocks(self, samples: np.ndarray, *, recording_rate: int = 16000) -> list[AudioBlock]:
        """Cut ``samples`` into numbered blocks without queueing them."""
        blocks = []
        for start in range(0, len(samples), self.BLOCK_SAMPLES):
            chunk = samples[start : start + self.BLOCK_SAMPLES].astype(np.int16)
            recording = np.repeat(chunk, recording_rate // 16000).astype(np.int16)
            blocks.append(
                AudioBlock(self._next_index, chunk.tobytes(), recording.tobytes(), recording_rate)
            )
            self._next_index += 1
        return blocks

    def push(self, *items: AudioBlock | CaptureGap) -> None:
        with self._condition:
            self._items.extend(items)
            self._condition.notify_all()

    def push_audio(self, samples: np.ndarray, *, recording_rate: int = 16000) -> list[AudioBlock]:
        blocks = self.blocks(samples, recording_rate=recording_rate)
        self.push(*blocks)
        return blocks

    def push_gap(self, reason: str = "overflow") -> None:
        self.push(CaptureGap(reason))


# -- Echo stage ------------------------------------------------------------------------


class FakeEchoStage:
    """Echo stage double: upsamples by repetition to ``rate`` and records its calls.

    ``hold`` makes every other ``process`` call return nothing and the next one
    both blocks, like a stage waiting for its reference.
    """

    def __init__(self, log: list[str] | None = None, *, rate: int = 48000) -> None:
        self.rate = rate
        self.state = "active"
        self.log = log if log is not None else []
        self.capture_rate = 0
        self.processed: list[tuple[int, str, float]] = []
        self.hold = False
        self.fail_process = False
        self.fail_open = False
        self.flush_output = np.zeros(0, dtype=np.int16)
        self._held = np.zeros(0, dtype=np.int16)

    def open(self, capture_rate: int) -> None:
        self.log.append(f"stage.open:{capture_rate}")
        if self.fail_open:
            raise RuntimeError("echo canceller could not start")
        self.capture_rate = capture_rate

    def process(self, mic: np.ndarray, arrival: float) -> np.ndarray:
        self.processed.append((len(mic), str(mic.dtype), arrival))
        if self.fail_process:
            raise RuntimeError("echo canceller failed")
        output = np.repeat(mic, self.rate // self.capture_rate).astype(np.int16)
        if not self.hold:
            return output
        if not self._held.size:
            self._held = output
            return np.zeros(0, dtype=np.int16)
        output, self._held = np.concatenate((self._held, output)), np.zeros(0, np.int16)
        return output

    def flush(self) -> np.ndarray:
        self.log.append("stage.flush")
        output, self.flush_output = self.flush_output, np.zeros(0, dtype=np.int16)
        return output

    def reset(self) -> None:
        self.log.append("stage.reset")
        self._held = np.zeros(0, dtype=np.int16)

    def close(self) -> None:
        self.log.append("stage.close")


# -- Speech decisions ------------------------------------------------------------------


class AmplitudeVad:
    """WebRTC VAD double: a 10 ms slice is speech when it is loud."""

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        samples = np.frombuffer(frame, dtype=np.int16)
        return bool(samples.size) and int(np.abs(samples.astype(np.int32)).max()) >= (
            SPEECH_AMPLITUDE // 2
        )


# -- Wakeword engine -------------------------------------------------------------------


class ScriptedEngine:
    """WakewordEngine double that reports the matches a test fires."""

    def __init__(
        self,
        model_ids: Iterable[str] = ("builtin/okay_nabu",),
        *,
        score_listener: Callable[[dict[str, float]], None] | None = None,
    ) -> None:
        self.model_ids = tuple(model_ids)
        self.score_listener = score_listener
        self.fail_start = False
        self.fail_detect = False
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.chunks: list[tuple[int, bool]] = []
        self._lock = threading.Lock()
        self._pending: deque[WakewordMatch] = deque()

    def fire(self, model_id: str, *, score: float = 0.9, threshold: float = 0.5) -> None:
        with self._lock:
            self._pending.append(WakewordMatch(model_id, score, threshold))

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("model could not load")
        self.started.set()

    def stop(self) -> None:
        self.stopped.set()

    def detect(self, audio_chunk: bytes, *, speech_present: bool = True) -> WakewordMatch | None:
        with self._lock:
            self.chunks.append((len(audio_chunk), speech_present))
            match = self._pending.popleft() if self._pending else None
        if self.fail_detect:
            raise RuntimeError("inference failed")
        if self.score_listener is not None:
            self.score_listener(
                {
                    model_id: (match.score if match and match.model_id == model_id else 0.1)
                    for model_id in self.model_ids
                }
            )
        return match


# -- Voice server ----------------------------------------------------------------------


@dataclass
class FakeVoiceServer:
    """An ``httpx.MockTransport`` handler answering the Voice RPC and upload calls."""

    transcript: str = "turn on the lights"
    speech: dict[str, Any] = field(default_factory=lambda: {"configured": True, "usable": True})
    agents: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {"main": {"id": "main", "current_session_id": "s-main"}}
    )
    upload_limit: int | None = None
    transcribe_gate: threading.Event | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[bytes] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    transcribing: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/speech/transcribe":
            with self.lock:
                self.uploads.append(request.content)
                self.methods.append("transcribe")
            self.transcribing.set()
            if self.transcribe_gate is not None and not self.transcribe_gate.wait(timeout=10):
                raise httpx.ReadTimeout("gate never opened")
            return httpx.Response(200, json={"text": self.transcript})
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        with self.lock:
            self.methods.append(method)
        if method == "task_model.status":
            return _ok(self.speech)
        if method == "agent.get":
            agent = self.agents.get(params["id"])
            if agent is None:
                return _rpc_error("not_found")
            return _ok(agent)
        if method == "session.create":
            return _ok({"session_id": "s-new"})
        if method == "session.list":
            return _ok({"sessions": []})
        if method == "settings.get_path":
            if self.upload_limit is None:
                return _rpc_error("not_found")
            return _ok({"setting": {"value": self.upload_limit}})
        if method == "chat.stream":
            with self.lock:
                self.sent.append(params)
            return _ok({"run_id": "r1"})
        raise AssertionError(f"unexpected RPC method {method}")


def _ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _rpc_error(code: str) -> httpx.Response:
    return httpx.Response(200, json={"ok": False, "error": {"code": code, "message": code}})
