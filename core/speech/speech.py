"""Provider-neutral speech execution service."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.model_tasks import TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH, TaskModelError
from core.providers.task_client import TaskClientRuntime
from core.speech.local import LocalSpeechError, LocalSpeechExecutor
from core.speech.providers import ProviderSpeechClient
from core.speech.types import SpeechSynthesisResult, SpeechTranscriptionResult
from core.utils.errors import TaskError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]
_LOGGER = get_logger("speech")
_ARTIFACT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class SpeechError(TaskError):
    """Base class for expected speech errors."""


class SpeechConfigurationError(SpeechError):
    """Raised when STT/TTS is not configured."""


class SpeechUnsupportedTargetError(SpeechError):
    """Raised when a configured speech target has no execution adapter."""


class SpeechExecutionError(SpeechError):
    """Raised when a provider speech request fails."""


@dataclass(frozen=True)
class SpeechArtifact:
    """Persisted TTS artifact metadata."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    file_path: Path

    @property
    def url(self) -> str:
        return f"/api/speech/artifacts/{self.id}"

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "kind": "speech",
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "url": self.url,
        }


class SpeechService:
    """Execute STT/TTS through configured task-model bindings."""

    def __init__(
        self,
        model_tasks: Any,
        runtime: TaskClientRuntime,
        data_dir: str | Path,
        *,
        local_executor: LocalSpeechExecutor | None = None,
    ) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._artifact_dir = Path(data_dir) / "speech"
        self._local_executor = local_executor or LocalSpeechExecutor()

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "recording.webm",
        media_type: str = "application/octet-stream",
    ) -> SpeechTranscriptionResult:
        """Transcribe one audio blob using the configured STT binding."""

        if not audio:
            raise SpeechConfigurationError("Audio input is empty")
        binding = self._binding_for(TASK_SPEECH_TO_TEXT)
        options = self._model_tasks.options_with_defaults(binding)
        target_ref = self._parse_target(binding.target)

        if target_ref.kind == "local":
            try:
                return await self._local_executor.transcribe(
                    target_ref.local_id,
                    audio,
                    filename=filename,
                    media_type=media_type,
                    options=options,
                )
            except LocalSpeechError as exc:
                raise SpeechUnsupportedTargetError(str(exc)) from exc

        provider_client = ProviderSpeechClient.from_runtime(self._runtime, target_ref)
        try:
            return await provider_client.transcribe(
                audio,
                filename=filename,
                media_type=media_type,
                options=options,
            )
        except SpeechError:
            raise
        except Exception as exc:
            _LOGGER.error("Speech transcription failed", exc_info=True)
            raise SpeechExecutionError(str(exc)) from exc

    async def synthesize(self, text: str) -> SpeechSynthesisResult:
        """Synthesize one text string using the configured TTS binding."""

        normalized_text = text.strip() if isinstance(text, str) else ""
        if not normalized_text:
            raise SpeechConfigurationError("Text to synthesize must not be empty")

        binding = self._binding_for(TASK_TEXT_TO_SPEECH)
        options = self._model_tasks.options_with_defaults(binding)
        target_ref = self._parse_target(binding.target)

        if target_ref.kind == "local":
            try:
                return await self._local_executor.synthesize(
                    target_ref.local_id,
                    normalized_text,
                    options=options,
                )
            except LocalSpeechError as exc:
                raise SpeechUnsupportedTargetError(str(exc)) from exc

        provider_client = ProviderSpeechClient.from_runtime(self._runtime, target_ref)
        try:
            return await provider_client.synthesize(normalized_text, options=options)
        except SpeechError:
            raise
        except Exception as exc:
            _LOGGER.error("Speech synthesis failed", exc_info=True)
            raise SpeechExecutionError(str(exc)) from exc

    async def synthesize_artifact(self, text: str) -> SpeechArtifact:
        """Synthesize speech and persist it as a runtime artifact."""

        result = await self.synthesize(text)
        artifact_id = uuid4().hex
        extension = _extension_for_audio(result.media_type, result.format)
        filename = f"{artifact_id}.{extension}"
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        file_path = self._artifact_dir / filename
        metadata_path = self._artifact_dir / f"{artifact_id}.json"
        file_path.write_bytes(result.audio)
        metadata = {
            "id": artifact_id,
            "filename": filename,
            "media_type": result.media_type,
            "size_bytes": len(result.audio),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        return SpeechArtifact(
            id=artifact_id,
            filename=filename,
            media_type=result.media_type,
            size_bytes=len(result.audio),
            file_path=file_path,
        )

    def get_artifact(self, artifact_id: str) -> SpeechArtifact:
        """Return a persisted speech artifact by id."""

        if not isinstance(artifact_id, str) or _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
            raise SpeechConfigurationError("Invalid speech artifact id")
        metadata_path = self._artifact_dir / f"{artifact_id}.json"
        if not metadata_path.is_file():
            raise SpeechConfigurationError("Speech artifact not found")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SpeechConfigurationError("Speech artifact metadata is unreadable") from exc

        filename = metadata.get("filename")
        media_type = metadata.get("media_type")
        size_bytes = metadata.get("size_bytes")
        if not isinstance(filename, str) or not isinstance(media_type, str):
            raise SpeechConfigurationError("Speech artifact metadata is invalid")
        file_path = self._artifact_dir / filename
        if not file_path.is_file():
            raise SpeechConfigurationError("Speech artifact file not found")
        return SpeechArtifact(
            id=artifact_id,
            filename=filename,
            media_type=media_type,
            size_bytes=size_bytes if isinstance(size_bytes, int) else file_path.stat().st_size,
            file_path=file_path,
        )

    def _binding_for(self, task_type: str) -> Any:
        try:
            return self._model_tasks.binding_for(task_type)
        except TaskModelError as exc:
            raise SpeechConfigurationError(str(exc)) from exc

    def _parse_target(self, target: str) -> Any:
        try:
            from core.model_tasks import parse_task_model_target_id

            return parse_task_model_target_id(target)
        except TaskModelError as exc:
            raise SpeechConfigurationError(str(exc)) from exc


def _extension_for_audio(media_type: str, fallback_format: str) -> str:
    media_type_lower = media_type.split(";", 1)[0].lower().strip()
    if media_type_lower in {"audio/mpeg", "audio/mp3"}:
        return "mp3"
    if media_type_lower == "audio/wav":
        return "wav"
    if media_type_lower == "audio/aac":
        return "aac"
    if media_type_lower == "audio/flac":
        return "flac"
    if media_type_lower == "audio/opus":
        return "opus"
    if media_type_lower == "audio/pcm":
        return "pcm"
    fallback = fallback_format.lower().strip()
    return fallback if fallback in {"mp3", "wav", "aac", "flac", "opus", "pcm"} else "bin"
