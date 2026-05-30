"""Tests for the provider-neutral speech service."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.model_tasks import TaskModelError
from core.speech import (
    LocalSpeechExecutor,
    SpeechConfigurationError,
    SpeechService,
    SpeechSynthesisResult,
)


@pytest.mark.asyncio
async def test_transcribe_without_configured_binding_is_expected_error(tmp_path: Path) -> None:
    service = SpeechService(_MissingModelTasks(), object(), tmp_path)

    with pytest.raises(SpeechConfigurationError, match="configured"):
        await service.transcribe(b"audio")


@pytest.mark.asyncio
async def test_synthesize_artifact_persists_metadata(tmp_path: Path) -> None:
    service = SpeechService(_TtsModelTasks(), object(), tmp_path, local_executor=_LocalTts())

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
