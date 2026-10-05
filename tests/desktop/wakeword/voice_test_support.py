"""Shared doubles for the Voice detection, command and controller tests."""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np

from desktop.wakeword.engine import WakewordMatch
from tests.desktop.speech.speech_test_support import SPEECH_AMPLITUDE

# -- Speech decisions ------------------------------------------------------------------


class AmplitudeDetector:
    """Neural speech detector double: loud audio is speech; counts resets."""

    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def is_speech(self, pcm16: bytes) -> bool:
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.int32)
        return bool(samples.size) and int(np.abs(samples).max()) >= SPEECH_AMPLITUDE // 2

    def speech_probability(self, detection_pcm16: bytes) -> float:
        return 1.0 if self.is_speech(detection_pcm16) else 0.0


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
        # Peak amplitude per chunk, so a test can see which audio detection reached.
        self.peaks: list[int] = []
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
            samples = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.int32)
            self.peaks.append(int(np.abs(samples).max()) if samples.size else 0)
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
        if method == "speech.prepare_transcription":
            return _ok({"state": "loading"})
        if method == "agent.get":
            agent = self.agents.get(params["id"])
            if agent is None:
                return _rpc_error("not_found")
            return _ok(agent)
        if method == "session.list":
            return _ok({"sessions": []})
        if method == "settings.get_path":
            if self.upload_limit is None:
                return _rpc_error("not_found")
            return _ok({"setting": {"value": self.upload_limit}})
        if method == "chat.stream":
            with self.lock:
                self.sent.append(params)
            # A message for a new Session gets the Session the server created for it.
            return _ok({"run_id": "r1", "session_id": params.get("session_id", "s-new")})
        raise AssertionError(f"unexpected RPC method {method}")


def _ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _rpc_error(code: str) -> httpx.Response:
    return httpx.Response(200, json={"ok": False, "error": {"code": code, "message": code}})
