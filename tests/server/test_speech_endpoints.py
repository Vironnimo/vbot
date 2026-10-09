"""Speech HTTP endpoints: JSON and progress-stream answers, body guards and live phases."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, override

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]
from starlette.requests import ClientDisconnect  # type: ignore[import-not-found]

import server.app as server_app
from core.model_tasks import (
    SpeechConfigurationError,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.model_tasks.speech_playback import PLAYBACK_MEDIA_TYPE, SpeechPlaybackStore
from core.model_tasks.speech_types import SpeechAudioChunk
from server.app import JSON_REQUEST_BODY_MAX_BYTES, _stream_speech, _stream_synthesis, create_app
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
        events = [json.loads(line) for line in streamed.text.splitlines()]
        playback_event = next(event for event in events if event["type"] == "playback")
        playback = client.get(playback_event["url"])
        unavailable = client.get("/api/speech/playback/expired")

    assert audio.headers["content-type"].startswith("audio/mpeg")
    assert audio.content == b"audio"
    assert streamed.headers["x-accel-buffering"] == "no"
    assert events[0]["type"] == "progress"
    assert events[-1] == {"type": "result", "result": {"url": "/api/speech/artifacts/aud_test"}}
    assert [event["type"] for event in events if event["type"] != "progress"] == [
        "playback",
        "result",
    ]
    assert playback.headers["content-type"] == PLAYBACK_MEDIA_TYPE
    assert playback.headers["cache-control"] == "no-store"
    assert playback.content == struct.pack("<II", 24_000, 2) + bytes(2) + bytes(8)
    assert unavailable.status_code == 410


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
async def test_transcription_stream_reports_the_live_phase_and_reaps_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TestClient reads whole responses, so the heartbeat and disconnect reaping
    # are observed on the endpoint's progress stream directly. The shortened
    # heartbeat is the one real wait here.
    monkeypatch.setattr(server_app, "SPEECH_PROGRESS_HEARTBEAT_SECONDS", 0.01)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_synthesis_publishes_playable_audio_before_artifact_and_reaps_disconnect(
    disconnect: bool,
) -> None:
    release = asyncio.Event()
    ended = asyncio.Event()

    class PendingSpeech(_Speech):
        @override
        async def synthesize_artifact(self, text: str, *, progress: Any, on_audio: Any) -> Any:
            result = await super().synthesize_artifact(text, progress=progress, on_audio=on_audio)
            try:
                await release.wait()
            finally:
                ended.set()
            return result

    speech = PendingSpeech()
    stream = _stream_synthesis(speech, "hello")
    assert json.loads(await anext(stream))["type"] == "progress"
    event = json.loads(await anext(stream))
    assert event["type"] == "playback"
    assert not ended.is_set()
    playback = speech.playbacks.get(event["url"].rsplit("/", 1)[-1])
    assert playback is not None
    audio = playback.frames()
    assert await anext(audio) == struct.pack("<II", 24_000, 2) + bytes(2)
    if disconnect:
        await stream.aclose()
        assert (await anext(audio))[8:] == b"cancelled"
    else:
        release.set()
        events = [json.loads(line) async for line in stream]
        assert events[-1]["type"] == "result"
        assert await anext(audio) == bytes(8)
    assert ended.is_set()
    await audio.aclose()
    await speech.playbacks.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["transcribe", "synthesize", "playback"])
@pytest.mark.parametrize("ending", ["send_failure", "disconnect"])
async def test_speech_responses_close_generators_when_connection_ends_during_send(
    monkeypatch: pytest.MonkeyPatch, surface: str, ending: str
) -> None:
    started, suspended, ended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    retained_responses: list[Any] = []
    original_call = server_app._SpeechStreamingResponse.__call__

    async def retain_response(response: Any, *args: Any, **kwargs: Any) -> None:
        # Retain the generator too: cleanup must happen before GC can finalize it.
        retained_responses.append(response)
        await original_call(response, *args, **kwargs)

    monkeypatch.setattr(server_app._SpeechStreamingResponse, "__call__", retain_response)

    async def pending(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    speech = _Speech()
    speech.transcribe = pending  # type: ignore[method-assign]
    speech.synthesize_artifact = pending  # type: ignore[method-assign]
    path = f"/api/speech/{surface}"
    headers = [(b"accept", b"application/x-ndjson")]
    if surface == "transcribe":
        body = (
            b"--speech-upload\r\n"
            b'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\naudio\r\n--speech-upload--\r\n"
        )
        headers.append((b"content-type", b"multipart/form-data; boundary=speech-upload"))
    elif surface == "synthesize":
        body = b'{"text":"hello"}'
        headers.append((b"content-type", b"application/json"))
    else:
        playback = speech.playbacks.create()
        assert playback is not None
        await playback.append(bytes(2), 24_000)
        frames = playback.frames()

        async def observed_frames() -> AsyncGenerator[bytes]:
            try:
                async for frame in frames:
                    yield frame
            finally:
                await frames.aclose()
                ended.set()

        monkeypatch.setattr(playback, "frames", observed_frames)
        path = playback.url
        body = b""
        started.set()

    runtime = SimpleNamespace(speech=speech, speech_upload_max_size_bytes=1024)
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4" if ending == "send_failure" else "2.3"},
        "http_version": "1.1",
        "method": "GET" if surface == "playback" else "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8420),
    }
    incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})

    async def send(message: Any) -> None:
        if message["type"] == "http.response.body":
            await started.wait()
            suspended.set()
            if ending == "send_failure":
                raise OSError("Connection closed during send")
            await asyncio.Event().wait()

    before = asyncio.all_tasks()
    task = asyncio.create_task(app(scope, incoming.get, send))
    try:
        async with asyncio.timeout(1):
            await suspended.wait()
            if ending == "disconnect":
                incoming.put_nowait({"type": "http.disconnect"})
                await task
            else:
                with pytest.raises(ClientDisconnect):
                    await task
        assert retained_responses and ended.is_set()
        assert not (asyncio.all_tasks() - before)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await speech.playbacks.aclose()


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
        self.playbacks = SpeechPlaybackStore()

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

    async def synthesize_artifact(self, text: str, *, progress: Any, on_audio: Any) -> Any:
        assert text == "hello"
        progress.update("synthesizing")
        await on_audio(SpeechAudioChunk(audio=bytes(2), sample_rate_hz=24_000))
        return SimpleNamespace(to_dict=lambda: {"url": "/api/speech/artifacts/aud_test"})


class _FailingSpeech(_Speech):
    @override
    async def transcribe(
        self,
        _audio: bytes,
        *,
        filename: str,
        media_type: str,
        progress: Any = None,
    ) -> SpeechTranscriptionResult:
        raise SpeechConfigurationError("Speech is not configured")
