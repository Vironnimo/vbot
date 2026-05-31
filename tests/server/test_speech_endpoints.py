"""Tests for speech HTTP endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.runs import ChatRunManager
from core.speech import SpeechConfigurationError, SpeechSynthesisResult, SpeechTranscriptionResult
from server.app import create_app


def test_transcribe_endpoint_returns_normalized_json(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/speech/transcribe",
            files={"file": ("clip.webm", b"audio", "audio/webm")},
        )

    assert response.status_code == 200
    assert response.json() == {"text": "hello"}


def test_synthesize_endpoint_returns_audio_bytes(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post("/api/speech/synthesize", json={"text": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"audio"


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
        speech_upload_max_size_bytes: int = 20_971_520,
    ) -> None:
        self.storage = type("Storage", (), {"data_dir": data_dir})()
        self.chat_runs = ChatRunManager()
        self.speech = _FailingSpeech() if fail else _Speech()
        self.speech_upload_max_size_bytes = speech_upload_max_size_bytes

    def start(self) -> None:
        self.storage.data_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        return None


class _Speech:
    def __init__(self) -> None:
        self.transcribe_calls = 0

    async def transcribe(
        self,
        _audio: bytes,
        *,
        filename: str,
        media_type: str,
    ) -> SpeechTranscriptionResult:
        self.transcribe_calls += 1
        return SpeechTranscriptionResult(text="hello")

    async def synthesize(self, _text: str) -> SpeechSynthesisResult:
        return SpeechSynthesisResult(audio=b"audio", media_type="audio/mpeg", format="mp3")


class _FailingSpeech(_Speech):
    async def transcribe(
        self,
        _audio: bytes,
        *,
        filename: str,
        media_type: str,
    ) -> SpeechTranscriptionResult:
        raise SpeechConfigurationError("Speech is not configured")
