"""Tests for speech HTTP endpoints."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.model_tasks import (
    SpeechConfigurationError,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.runs import ChatRunManager
from server.app import _stream_speech, create_app


def test_transcribe_endpoint_returns_normalized_json(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/speech/transcribe",
            files={"file": ("clip.webm", b"audio", "audio/webm")},
        )

    assert response.status_code == 200
    assert response.json() == {"text": "hello"}


@pytest.mark.parametrize("fail", [False, True])
def test_transcribe_progress_stream_has_one_terminal_event(tmp_path: Path, fail: bool) -> None:
    with _create_client(tmp_path, fail=fail) as client:
        response = client.post(
            "/api/speech/transcribe",
            headers={"Accept": "application/x-ndjson"},
            files={"file": ("clip.webm", b"audio", "audio/webm")},
        )
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "progress"
    if fail:
        assert events[-1]["type"] == "error" and events[-1]["status"] == 409
    else:
        assert events[-1] == {"type": "result", "result": {"text": "hello"}}
    assert len([event for event in events if event["type"] != "progress"]) == 1


@pytest.mark.asyncio
async def test_transcription_stream_reports_each_live_phase_and_reaps_disconnect() -> None:
    advance = asyncio.Event()
    cancelled = asyncio.Event()

    class Speech:
        async def transcribe(self, audio, *, filename, media_type, progress):
            try:
                for phase in ("downloading", "loading", "transcribing"):
                    progress.update(phase)
                    await advance.wait()
                    advance.clear()
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    stream = _stream_speech(
        lambda progress: Speech().transcribe(
            b"audio", filename="clip.wav", media_type="audio/wav", progress=progress
        )
    )
    assert json.loads(await anext(stream))["phase"] == "preparing"
    for phase in ("downloading", "loading", "transcribing"):
        event = json.loads(await anext(stream))
        assert event["phase"] == phase
        assert event["elapsed_seconds"] >= 0
        advance.set()
    await stream.aclose()
    assert cancelled.is_set()


def test_synthesize_endpoint_returns_audio_bytes(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post("/api/speech/synthesize", json={"text": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"audio"


def test_synthesis_preview_stream_returns_progress_and_artifact(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/speech/synthesize",
            json={"text": "hello"},
            headers={"Accept": "application/x-ndjson"},
        )
    assert response.headers["x-accel-buffering"] == "no"
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "progress"
    assert events[-1] == {"type": "result", "result": {"url": "/api/speech/artifacts/aud_test"}}
    assert len([event for event in events if event["type"] != "progress"]) == 1


def test_synthesize_endpoint_rejects_malformed_json_before_speech_call(tmp_path: Path) -> None:
    runtime = _SpeechRuntime(tmp_path / "data", fail=False)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.post(
            "/api/speech/synthesize",
            content="{",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Request body must be valid JSON"
    assert runtime.speech.synthesize_calls == 0


def test_synthesize_endpoint_rejects_non_json_media_type_before_speech_call(
    tmp_path: Path,
) -> None:
    runtime = _SpeechRuntime(tmp_path / "data", fail=False)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.post(
            "/api/speech/synthesize",
            content='{"text":"hello"}',
            headers={"content-type": "text/plain"},
        )

    assert response.status_code == 415
    assert runtime.speech.synthesize_calls == 0


def test_synthesize_endpoint_rejects_invalid_utf8_before_speech_call(tmp_path: Path) -> None:
    runtime = _SpeechRuntime(tmp_path / "data", fail=False)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.post(
            "/api/speech/synthesize",
            content=b"\xff",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Request body must be valid JSON"
    assert runtime.speech.synthesize_calls == 0


def test_speech_expected_errors_map_to_http_status(tmp_path: Path) -> None:
    with _create_client(tmp_path, fail=True) as client:
        response = client.post(
            "/api/speech/transcribe",
            files={"file": ("clip.webm", b"audio", "audio/webm")},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Speech is not configured"


def test_transcribe_rejects_payload_before_speech_call(tmp_path: Path) -> None:
    runtime = _SpeechRuntime(tmp_path / "data", fail=False, speech_upload_max_size_bytes=3)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.post(
            "/api/speech/transcribe",
            files={"file": ("clip.webm", b"audio", "audio/webm")},
        )

    assert response.status_code == 413
    assert runtime.speech.transcribe_calls == 0


def _create_client(tmp_path: Path, *, fail: bool = False) -> TestClient:
    runtime = _SpeechRuntime(tmp_path / "data", fail=fail)
    app = create_app(runtime=cast(Any, runtime))
    return TestClient(app)


class _SpeechRuntime:
    def __init__(
        self,
        data_dir: Path,
        *,
        fail: bool,
        speech_upload_max_size_bytes: int = 104_857_600,
    ) -> None:
        self.storage = type("Storage", (), {"data_dir": data_dir})()
        self.chat_runs = ChatRunManager()
        self.chat_run_manager = self.chat_runs
        self.chat_loop = object()
        self.streaming_chat_loop = object()
        self.command_dispatcher = object()
        self.speech = _FailingSpeech() if fail else _Speech()
        self.speech_upload_max_size_bytes = speech_upload_max_size_bytes

    def start(self) -> None:
        self.storage.data_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        return None


class _Speech:
    def __init__(self) -> None:
        self.transcribe_calls = 0
        self.synthesize_calls = 0

    async def transcribe(
        self,
        _audio: bytes,
        *,
        filename: str,
        media_type: str,
        progress: Any = None,
    ) -> SpeechTranscriptionResult:
        self.transcribe_calls += 1
        return SpeechTranscriptionResult(text="hello")

    async def synthesize(self, _text: str) -> SpeechSynthesisResult:
        self.synthesize_calls += 1
        return SpeechSynthesisResult(audio=b"audio", media_type="audio/mpeg", format="mp3")

    async def synthesize_artifact(self, text: str, *, progress: Any):
        assert text == "hello"
        progress.update("synthesizing")
        return SimpleNamespace(to_dict=lambda: {"url": "/api/speech/artifacts/aud_test"})


class _FailingSpeech(_Speech):
    async def transcribe(
        self,
        _audio: bytes,
        *,
        filename: str,
        media_type: str,
        progress: Any = None,
    ) -> SpeechTranscriptionResult:
        raise SpeechConfigurationError("Speech is not configured")
