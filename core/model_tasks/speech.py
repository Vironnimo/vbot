"""Provider-neutral speech execution service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.model_tasks.artifacts import StoredArtifact, TaskArtifactStore
from core.model_tasks.constants import TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH
from core.model_tasks.speech_local import LocalSpeechError, LocalSpeechExecutor
from core.model_tasks.speech_providers import ProviderSpeechClient
from core.model_tasks.speech_types import SpeechSynthesisResult, SpeechTranscriptionResult
from core.model_tasks.task_execution import TaskBindingResolver
from core.providers.task_client import TaskClientRuntime
from core.utils.errors import TaskError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]
_LOGGER = get_logger("speech")


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
        self._runtime = runtime
        self._resolver = TaskBindingResolver(
            model_tasks, configuration_error=SpeechConfigurationError
        )
        self._artifacts = TaskArtifactStore(
            Path(data_dir) / "speech", kind="speech", error=SpeechConfigurationError
        )
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
        _binding, options, target_ref = self._resolver.resolve(TASK_SPEECH_TO_TEXT)

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

        _binding, options, target_ref = self._resolver.resolve(TASK_TEXT_TO_SPEECH)

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
        stored = self._artifacts.write(
            result.audio,
            extension=_extension_for_audio(result.media_type, result.format),
            media_type=result.media_type,
        )
        return _speech_artifact(stored)

    def get_artifact(self, artifact_id: str) -> SpeechArtifact:
        """Return a persisted speech artifact by id."""

        return _speech_artifact(self._artifacts.read(artifact_id))


def _speech_artifact(stored: StoredArtifact) -> SpeechArtifact:
    return SpeechArtifact(
        id=stored.id,
        filename=stored.filename,
        media_type=stored.media_type,
        size_bytes=stored.size_bytes,
        file_path=stored.file_path,
    )


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
