"""Speech HTTP endpoints: JSON and progress-stream answers, body guards and live phases."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.model_tasks import (
    SpeechConfigurationError,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from server.app import JSON_REQUEST_BODY_MAX_BYTES, _stream_speech, create_app
from tests.server.app_test_support import ServerStubRuntime

_AUDIO_FILE = {"file": ("clip.webm", b"audio", "audio/webm")}
_NDJSON = {"Accept": "application/x-ndjson"}


@pytest.mark.parametrize("fail", [False, True])
def test_transcribe_answers_json_or_one_terminal_progress_event(tmp_path: Path, fail: bool) -> None:
    with _client(tmp_path, _FailingSpeech() if fail else _Speech()) as client:
        plain = client.post("/api/speech/transcribe", files=_AUDIO_FILE)
        streamed = client.post("/api/speech/transcribe", headers=_NDJSON, files=_AUDIO_FILE)

    events = [json.loads(line) for line in streamed.text.splitlines()]
    assert streamed.headers["content-type"].startswith("application/x-ndjson")
    assert events[0]["type"] == "progress"
    assert len([event for event in events if event["type"] != "progress"]) == 1
    if fail:
        assert (plain.status_code, plain.json()["detail"]) == (409, "Speech is not configured")
        assert events[-1]["type"] == "error" and events[-1]["status"] == 409
    else:
        assert (plain.status_code, plain.json()) == (200, {"text": "hello"})
        assert events[-1] == {"type": "result", "result": {"text": "hello"}}


def test_synthesize_answers_audio_or_a_progress_stream_with_the_artifact(tmp_path: Path) -> None:
    with _client(tmp_path, _Speech()) as client:
        audio = client.post("/api/speech/synthesize", json={"text": "hello"})
        streamed = client.post("/api/speech/synthesize", json={"text": "hello"}, headers=_NDJSON)

    assert audio.headers["content-type"].startswith("audio/mpeg")
    assert audio.content == b"audio"
    assert streamed.headers["x-accel-buffering"] == "no"
    events = [json.loads(line) for line in streamed.text.splitlines()]
    assert events[0]["type"] == "progress"
    assert events[-1] == {"type": "result", "result": {"url": "/api/speech/artifacts/aud_test"}}
    assert len([event for event in events if event["type"] != "progress"]) == 1


def test_speech_endpoints_reject_bad_bodies_before_calling_speech(tmp_path: Path) -> None:
    speech = _Speech()
    json_type = {"content-type": "application/json"}

    with _client(tmp_path, speech, speech_upload_max_size_bytes=3) as client:
        malformed = [
            client.post("/api/speech/synthesize", content=content, headers=json_type)
            for content in ("{", b"\xff")
        ]
        wrong_media_type = client.post(
            "/api/speech/synthesize",
            content='{"text":"hello"}',
            headers={"content-type": "text/plain"},
        )
        oversized = client.post(
            "/api/speech/synthesize",
            content=b"x" * (JSON_REQUEST_BODY_MAX_BYTES + 1),
            headers=json_type,
        )
        oversized_audio = client.post("/api/speech/transcribe", files=_AUDIO_FILE)

    for response in malformed:
        assert (response.status_code, response.json()["detail"]) == (
            400,
            "Request body must be valid JSON",
        )
    assert wrong_media_type.status_code == 415
    assert oversized.status_code == oversized_audio.status_code == 413
    assert speech.synthesize_calls == speech.transcribe_calls == 0


@pytest.mark.asyncio
async def test_transcription_stream_reports_the_live_phase_and_reaps_disconnect() -> None:
    # TestClient reads whole responses, so the heartbeat and disconnect reaping
    # are observed on the endpoint's progress stream directly. The stream's
    # fixed 0.5 s heartbeat is the one real wait here.
    cancelled = asyncio.Event()

    async def transcribe(progress: Any) -> Any:
        try:
            progress.update("transcribing")
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    stream = _stream_speech(transcribe)
    assert json.loads(await anext(stream))["phase"] == "preparing"
    event = json.loads(await anext(stream))
    assert event["phase"] == "transcribing"
    assert event["elapsed_seconds"] >= 0
    await stream.aclose()
    assert cancelled.is_set()


def _client(
    tmp_path: Path, speech: _Speech, *, speech_upload_max_size_bytes: int = 104_857_600
) -> TestClient:
    runtime = ServerStubRuntime(
        tmp_path / "data",
        speech=speech,
        speech_upload_max_size_bytes=speech_upload_max_size_bytes,
    )
    return TestClient(create_app(runtime=runtime))


class _Speech:
    def __init__(self) -> None:
        self.transcribe_calls = 0
        self.synthesize_calls = 0

    def preload_configured(self) -> None:
        return None

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

    async def synthesize_artifact(self, text: str, *, progress: Any) -> Any:
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
