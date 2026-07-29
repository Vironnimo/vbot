"""Tests for the provider-neutral speech service."""

from __future__ import annotations

import io
import logging
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from core.model_tasks import (
    LocalSpeechExecutor,
    SpeechConfigurationError,
    SpeechExecutionError,
    SpeechOutcomeUnknownError,
    SpeechService,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
    TaskModelError,
)
from core.providers.errors import ProviderError, ProviderOutcomeUnknownError
from core.storage.layout import DataDirectoryLayout


@pytest.mark.asyncio
async def test_transcribe_without_configured_binding_is_logged_expected_error(
    tmp_path: Path,
    caplog: Any,
) -> None:
    service = SpeechService(_MissingModelTasks(), cast(Any, object()), tmp_path)

    with (
        caplog.at_level(logging.WARNING, logger="vbot.speech"),
        pytest.raises(SpeechConfigurationError, match="configured"),
    ):
        await service.transcribe(b"audio")

    assert "Speech transcription unavailable" in caplog.text
    assert "No task model configured" in caplog.text


@pytest.mark.asyncio
async def test_synthesize_artifact_persists_metadata(tmp_path: Path) -> None:
    service = SpeechService(
        _TtsModelTasks(), cast(Any, object()), tmp_path, local_executor=_LocalTts()
    )

    artifact = await service.synthesize_artifact("hello")

    assert artifact.media_type == "audio/mpeg"
    assert artifact.size_bytes == 5
    assert artifact.file_path.parent == DataDirectoryLayout(tmp_path).speech
    assert artifact.file_path.read_bytes() == b"audio"
    assert (
        service.get_artifact(artifact.id).to_dict()["url"] == f"/api/speech/artifacts/{artifact.id}"
    )


class _MissingModelTasks:
    def binding_for(self, _task_type: str) -> object:
        raise TaskModelError("No task model configured")


class _TtsModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(task_type=task_type, target="local/piper", options={})

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}


class _LocalTts(LocalSpeechExecutor):
    async def synthesize(
        self,
        _local_id: str,
        _text: str,
        *,
        options: dict[str, object],
    ) -> SpeechSynthesisResult:
        return SpeechSynthesisResult(audio=b"audio", media_type="audio/mpeg", format="mp3")


class _ProviderSttModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(
            task_type=task_type,
            target="openrouter/whisper-large-v3::api-key",
            options={},
        )

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}


class _ProviderTtsModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(
            task_type=task_type,
            target="openrouter/openai/gpt-4o-mini-tts::api-key",
            options={},
        )

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}


class _FailingProviderSpeechClient:
    def __init__(self, exception: Exception) -> None:
        self._exception = exception

    async def transcribe(self, *_args: object, **_kwargs: object) -> object:
        raise self._exception

    async def synthesize(self, *_args: object, **_kwargs: object) -> object:
        raise self._exception


class _CapturingProviderSpeechClient:
    def __init__(self) -> None:
        self.audio = b""
        self.filename = ""
        self.media_type = ""

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        media_type: str,
        options: dict[str, object],
    ) -> SpeechTranscriptionResult:
        self.audio = audio
        self.filename = filename
        self.media_type = media_type
        return SpeechTranscriptionResult(text="hello")


@pytest.mark.asyncio
async def test_transcribe_normalizes_provider_audio_to_configured_profile(
    tmp_path: Path,
) -> None:
    import av

    client = _CapturingProviderSpeechClient()
    service = SpeechService(
        _ProviderSttModelTasks(),
        cast(Any, object()),
        tmp_path,
        transcription_audio_getter=lambda: {
            "transcription_audio": {
                "profile": "custom",
                "format": "flac",
                "sample_rate_hz": 24_000,
            }
        },
    )

    with patch(
        "core.model_tasks.speech.ProviderSpeechClient.from_runtime",
        return_value=client,
    ):
        result = await service.transcribe(
            _webm_audio_bytes(),
            filename="browser.webm",
            media_type="audio/webm",
        )

    assert result.text == "hello"
    assert client.filename == "recording.flac"
    assert client.media_type == "audio/flac"
    container = av.open(io.BytesIO(client.audio), mode="r")
    try:
        stream = container.streams.audio[0]
        assert stream.codec_context.name == "flac"
        assert stream.sample_rate == 24_000
        assert stream.channels == 1
    finally:
        container.close()


@pytest.mark.asyncio
async def test_transcribe_logs_provider_error_at_warning_without_traceback(
    tmp_path: Path,
    caplog: Any,
) -> None:
    """A provider :class:`ProviderError` (a VBotError) logs at warning, no traceback."""

    service = SpeechService(_ProviderSttModelTasks(), cast(Any, object()), tmp_path)
    failing_client = _FailingProviderSpeechClient(ProviderError("rate limited"))

    with (
        patch(
            "core.model_tasks.speech.ProviderSpeechClient.from_runtime",
            return_value=failing_client,
        ),
        caplog.at_level(logging.WARNING, logger="vbot.speech"),
        pytest.raises(SpeechExecutionError, match="rate limited"),
    ):
        await service.transcribe(_wav_audio_bytes())

    relevant = [r for r in caplog.records if "Speech transcription failed" in r.getMessage()]
    assert relevant, "expected a log record for the failed transcription"
    assert all(r.levelno == logging.WARNING for r in relevant)
    assert all(r.exc_info is None for r in relevant)


@pytest.mark.asyncio
async def test_synthesize_preserves_unknown_provider_outcome(
    tmp_path: Path,
    caplog: Any,
) -> None:
    service = SpeechService(_ProviderTtsModelTasks(), cast(Any, object()), tmp_path)
    failing_client = _FailingProviderSpeechClient(
        ProviderOutcomeUnknownError("request may have completed", operation_key="speech-op")
    )

    with (
        patch(
            "core.model_tasks.speech.ProviderSpeechClient.from_runtime",
            return_value=failing_client,
        ),
        caplog.at_level(logging.WARNING, logger="vbot.speech"),
        pytest.raises(SpeechOutcomeUnknownError) as exc_info,
    ):
        await service.synthesize("hello")

    assert exc_info.value.code == "provider_outcome_unknown"
    assert exc_info.value.operation_key == "speech-op"
    assert "provider_outcome_unknown" in caplog.text


def _wav_audio_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(48_000)
        wav_file.writeframes(b"\x00\x00" * 4_800)
    return output.getvalue()


def _webm_audio_bytes() -> bytes:
    import av

    source = av.open(io.BytesIO(_wav_audio_bytes()), mode="r")
    output = io.BytesIO()
    target = av.open(output, mode="w", format="webm")
    try:
        stream = target.add_stream("libopus", rate=48_000)
        stream.layout = "mono"
        for frame in source.decode(audio=0):
            for packet in stream.encode(frame):
                target.mux(packet)
        for packet in stream.encode(None):
            target.mux(packet)
    finally:
        source.close()
        target.close()
    return output.getvalue()
