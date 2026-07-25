"""Provider-neutral speech execution service."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core.model_tasks.artifacts import StoredArtifact, TaskArtifactStore
from core.model_tasks.constants import (
    DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_TO_SPEECH,
)
from core.model_tasks.speech_local import LocalSpeechError, LocalSpeechExecutor
from core.model_tasks.speech_providers import ProviderSpeechClient
from core.model_tasks.speech_types import SpeechSynthesisResult, SpeechTranscriptionResult
from core.model_tasks.task_execution import TaskBindingResolver
from core.providers.task_client import TaskClientRuntime
from core.storage.layout import DataDirectoryLayout
from core.utils.errors import TaskError, VBotError
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


@dataclass(frozen=True)
class _PreparedTranscriptionAudio:
    audio: bytes
    filename: str
    media_type: str


class SpeechService:
    """Execute STT/TTS through configured task-model bindings."""

    def __init__(
        self,
        model_tasks: Any,
        runtime: TaskClientRuntime,
        data_dir: str | Path,
        *,
        local_executor: LocalSpeechExecutor | None = None,
        transcription_audio_getter: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._resolver = TaskBindingResolver(
            model_tasks, configuration_error=SpeechConfigurationError
        )
        self._artifacts = TaskArtifactStore(
            DataDirectoryLayout(data_dir).speech,
            kind="speech",
            error=SpeechConfigurationError,
        )
        self._local_executor = local_executor or LocalSpeechExecutor()
        self._transcription_audio_getter = transcription_audio_getter or (
            lambda: {"transcription_audio": dict(DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS)}
        )

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
        try:
            _binding, options, target_ref = self._resolver.resolve(TASK_SPEECH_TO_TEXT)
        except SpeechConfigurationError as exc:
            _LOGGER.warning("Speech transcription unavailable: %s", exc)
            raise

        try:
            prepared = await asyncio.to_thread(
                _prepare_transcription_audio,
                audio,
                self._transcription_audio_getter(),
            )
        except Exception as exc:
            _LOGGER.warning("Speech transcription audio conversion failed: %s", exc)
            raise SpeechExecutionError(
                "Audio input could not be converted for transcription"
            ) from exc

        if target_ref.kind == "local":
            try:
                return await self._local_executor.transcribe(
                    target_ref.local_id,
                    prepared.audio,
                    filename=prepared.filename,
                    media_type=prepared.media_type,
                    options=options,
                )
            except LocalSpeechError as exc:
                raise SpeechUnsupportedTargetError(str(exc)) from exc

        provider_client = ProviderSpeechClient.from_runtime(self._runtime, target_ref)
        try:
            return await provider_client.transcribe(
                prepared.audio,
                filename=prepared.filename,
                media_type=prepared.media_type,
                options=options,
            )
        except SpeechError:
            raise
        except VBotError as exc:
            # ProviderError / NetworkError / ProviderAuthError / … are
            # expected provider failures, not crashes.
            _LOGGER.warning(
                "Speech transcription failed for target=%s: %s",
                target_ref.target,
                exc,
            )
            raise SpeechExecutionError(str(exc)) from exc
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
        except VBotError as exc:
            # ProviderError / NetworkError / ProviderAuthError / … are
            # expected provider failures, not crashes.
            _LOGGER.warning(
                "Speech synthesis failed for target=%s: %s",
                target_ref.target,
                exc,
            )
            raise SpeechExecutionError(str(exc)) from exc
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


def _prepare_transcription_audio(
    audio: bytes,
    speech_settings: Mapping[str, Any],
) -> _PreparedTranscriptionAudio:
    transcription_audio = speech_settings.get("transcription_audio")
    if not isinstance(transcription_audio, Mapping):
        transcription_audio = DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS

    audio_format = transcription_audio.get("format", DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["format"])
    if not isinstance(audio_format, str) or audio_format not in {"wav", "flac"}:
        raise ValueError("Unsupported transcription audio format")
    sample_rate_hz = transcription_audio.get(
        "sample_rate_hz",
        DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["sample_rate_hz"],
    )
    if not isinstance(sample_rate_hz, int) or isinstance(sample_rate_hz, bool):
        raise ValueError("Unsupported transcription audio sample rate")
    codec = {"wav": "pcm_s16le", "flac": "flac"}[audio_format]
    media_type = {"wav": "audio/wav", "flac": "audio/flac"}[audio_format]

    import av

    input_container = av.open(io.BytesIO(audio), mode="r")
    output_buffer = io.BytesIO()
    output_container = None
    try:
        if not input_container.streams.audio:
            raise ValueError("Audio input has no audio stream")
        output_container = av.open(output_buffer, mode="w", format=audio_format)
        output_stream = cast(Any, output_container.add_stream(codec, rate=sample_rate_hz))
        output_stream.layout = "mono"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate_hz)

        for frame in input_container.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in output_stream.encode(converted):
                    output_container.mux(packet)
        for converted in resampler.resample(None):
            for packet in output_stream.encode(converted):
                output_container.mux(packet)
        for packet in output_stream.encode(None):
            output_container.mux(packet)
    finally:
        input_container.close()
        if output_container is not None:
            output_container.close()

    output_audio = output_buffer.getvalue()
    if not output_audio:
        raise ValueError("Audio conversion produced no output")
    return _PreparedTranscriptionAudio(
        audio=output_audio,
        filename=f"recording.{audio_format}",
        media_type=media_type,
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
