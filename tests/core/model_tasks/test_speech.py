"""Tests for the provider-neutral speech service."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from core.model_tasks import (
    LocalSpeechExecutor,
    SpeechConfigurationError,
    SpeechExecutionError,
    SpeechService,
    SpeechSynthesisResult,
    TaskModelError,
)
from core.providers.errors import ProviderError


@pytest.mark.asyncio
async def test_transcribe_without_configured_binding_is_expected_error(tmp_path: Path) -> None:
    service = SpeechService(_MissingModelTasks(), cast(Any, object()), tmp_path)

    with pytest.raises(SpeechConfigurationError, match="configured"):
        await service.transcribe(b"audio")


@pytest.mark.asyncio
async def test_synthesize_artifact_persists_metadata(tmp_path: Path) -> None:
    service = SpeechService(
        _TtsModelTasks(), cast(Any, object()), tmp_path, local_executor=_LocalTts()
    )

    artifact = await service.synthesize_artifact("hello")

    assert artifact.media_type == "audio/mpeg"
    assert artifact.size_bytes == 5
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


class _FailingProviderSpeechClient:
    def __init__(self, exception: Exception) -> None:
        self._exception = exception

    async def transcribe(self, *_args: object, **_kwargs: object) -> object:
        raise self._exception


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
        await service.transcribe(b"audio")

    relevant = [r for r in caplog.records if "Speech transcription failed" in r.getMessage()]
    assert relevant, "expected a log record for the failed transcription"
    assert all(r.levelno == logging.WARNING for r in relevant)
    assert all(r.exc_info is None for r in relevant)
