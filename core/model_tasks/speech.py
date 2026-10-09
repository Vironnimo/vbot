"""Provider-neutral speech execution service."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.model_tasks.artifacts import StoredArtifact, TaskArtifactStore
from core.model_tasks.constants import (
    DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_TO_SPEECH,
)
from core.model_tasks.speech_input import (
    PreparedTranscriptionAudio,
    SpeechInputError,
    TranscriptionInputPolicy,
    prepare_transcription_audio,
)
from core.model_tasks.speech_local import (
    PRELOAD_OPTION,
    LocalSpeechError,
    LocalSpeechExecutionError,
    LocalSpeechExecutor,
    LocalSpeechSetup,
)
from core.model_tasks.speech_playback import SpeechPlaybackStore
from core.model_tasks.speech_providers import ProviderSpeechClient
from core.model_tasks.speech_types import (
    SpeechAudioCallback,
    SpeechBusyError,
    SpeechError,
    SpeechProgress,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.model_tasks.task_execution import TaskBindingResolver, TaskUsage, TaskUsageContext
from core.providers.errors import ProviderOutcomeUnknownError
from core.providers.task_client import TaskClientRuntime
from core.settings import DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
from core.storage.layout import DataDirectoryLayout
from core.usage import UsageRecorder
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

JsonObject = dict[str, Any]
_LOGGER = get_logger("speech")
_MAX_CONCURRENT_TRANSCRIPTIONS = 2
_MAX_PENDING_TRANSCRIPTIONS = 4


class SpeechConfigurationError(SpeechError):
    """Raised when STT/TTS is not configured."""


class SpeechUnsupportedTargetError(SpeechError):
    """Raised when a configured speech target has no execution adapter."""


class SpeechExecutionError(SpeechError):
    """Raised when a provider speech request fails."""


class SpeechOutcomeUnknownError(SpeechExecutionError):
    """Raised when speech synthesis may have completed at the provider."""

    code = ProviderOutcomeUnknownError.code

    def __init__(self, message: str, *, operation_key: str) -> None:
        self.operation_key = operation_key
        super().__init__(message)


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
        transcription_audio_getter: Callable[[], Mapping[str, Any]] | None = None,
        usage_recorder: UsageRecorder | None = None,
        max_input_bytes: int = DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES,
    ) -> None:
        self._runtime = runtime
        self._max_input_bytes = max_input_bytes
        self._transcriptions = asyncio.Semaphore(_MAX_CONCURRENT_TRANSCRIPTIONS)
        self._pending_transcriptions = 0
        self._input_workers = BoundedWorkerPool(name="speech_input", max_workers=2)
        self._closed = False
        self.playbacks = SpeechPlaybackStore()
        self._usage_recorder = usage_recorder
        self._resolver = TaskBindingResolver(
            model_tasks, configuration_error=SpeechConfigurationError
        )
        self._artifacts = TaskArtifactStore(
            DataDirectoryLayout(data_dir).speech,
            kind="speech",
            error=SpeechConfigurationError,
        )
        self._local_executor = local_executor or LocalSpeechExecutor(
            engines_dir=DataDirectoryLayout(data_dir).speech_engines
        )
        self._transcription_audio_getter = transcription_audio_getter or (
            lambda: {"transcription_audio": dict(DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS)}
        )

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "recording.webm",
        media_type: str = "application/octet-stream",
        progress: SpeechProgress | None = None,
    ) -> SpeechTranscriptionResult:
        """Transcribe one audio blob using the configured STT binding."""

        if not audio:
            raise SpeechConfigurationError("Audio input is empty")
        if len(audio) > self._max_input_bytes:
            raise SpeechInputError(
                "Audio input exceeds the configured upload limit", too_large=True
            )
        if self._closed:
            raise SpeechConfigurationError("Speech service is closed")
        if self._pending_transcriptions >= _MAX_PENDING_TRANSCRIPTIONS:
            raise SpeechBusyError(
                "Transcription is busy. Retry after an active recording finishes."
            )
        # Admission and the counter run on the Event Loop before the first wait.
        # Bound retained queued recordings as well as decoder/Provider work.
        self._pending_transcriptions += 1
        try:
            if progress is not None:
                progress.update("queued")
            async with self._transcriptions:
                if self._closed:
                    raise SpeechConfigurationError("Speech service is closed")
                return await self._transcribe(audio, filename, media_type, progress)
        finally:
            self._pending_transcriptions -= 1

    async def _transcribe(
        self, audio: bytes, filename: str, media_type: str, progress: SpeechProgress | None
    ) -> SpeechTranscriptionResult:
        try:
            _binding, options, target_ref = self._resolver.resolve(TASK_SPEECH_TO_TEXT)
        except SpeechConfigurationError as exc:
            _LOGGER.warning("Speech transcription unavailable: %s", exc)
            raise

        usage = TaskUsage(self._usage_recorder, TASK_SPEECH_TO_TEXT, target_ref)
        if target_ref.kind == "local":
            try:
                async with usage.attempt() as call_id:
                    result = await self._local_executor.transcribe(
                        target_ref.local_id,
                        audio,
                        filename=filename,
                        media_type=media_type,
                        options=options,
                        progress=progress,
                    )
                    await usage.update(call_id, result.usage)
                    return result
            except LocalSpeechExecutionError as exc:
                raise SpeechExecutionError(str(exc)) from exc
            except LocalSpeechError as exc:
                raise SpeechUnsupportedTargetError(str(exc)) from exc

        try:
            provider_client = ProviderSpeechClient.from_runtime(
                self._runtime, target_ref, usage_observer=usage
            )
            if progress is not None:
                progress.update("preparing")
            prepared = await self._prepare_input(
                audio, provider_client.transcription_input_policy()
            )
            if self._closed:
                raise SpeechConfigurationError("Speech service is closed")
            if progress is not None:
                progress.update("transcribing")
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

    async def _prepare_input(
        self, audio: bytes, policy: TranscriptionInputPolicy
    ) -> PreparedTranscriptionAudio:
        cancelled = threading.Event()

        def check_cancel() -> None:
            if cancelled.is_set():
                raise asyncio.CancelledError()

        work = asyncio.create_task(
            self._input_workers.run(
                prepare_transcription_audio,
                audio,
                self._transcription_audio_getter(),
                policy,
                check_cancel=check_cancel,
            )
        )
        try:
            await asyncio.wait((work,))
        except asyncio.CancelledError:
            cancelled.set()
            work.cancel()
            await settle_before_cancelling(asyncio.gather(work, return_exceptions=True))
            raise
        return work.result()

    def prepare_transcription(self) -> str:
        """Start loading the bound local STT engine because a transcription is coming.

        Returns without waiting: ``loaded`` or ``loading`` for a local engine,
        ``not_local`` when a Provider transcribes, ``unavailable`` when no
        usable binding exists. Must run on the Event Loop.
        """
        try:
            _binding, options, target_ref = self._resolver.resolve(TASK_SPEECH_TO_TEXT)
        except SpeechConfigurationError:
            return "unavailable"
        if target_ref.kind != "local":
            return "not_local"
        return self._local_executor.prepare(target_ref.local_id, options)

    def preload_configured(
        self, task_types: Iterable[str] = (TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH)
    ) -> None:
        """Start loading each bound local speech engine whose binding asks to preload.

        Called for both bindings after Runtime startup, and for a binding after it
        changed. A model that is already loaded stays; turning the option off
        unloads nothing.
        """
        for task_type in task_types:
            try:
                _binding, options, target_ref = self._resolver.resolve(task_type)
            except SpeechConfigurationError:
                continue
            if target_ref.kind == "local" and options.get(PRELOAD_OPTION) is True:
                self._local_executor.prepare(target_ref.local_id, options)

    def local_setup_for(self, target: str) -> LocalSpeechSetup:
        return self._local_executor.setup_for(target)

    def local_memory_status(self) -> dict[str, Any]:
        return self._local_executor.memory_status()

    def local_activities(self) -> list[dict[str, Any]]:
        return self._local_executor.activities()

    async def unload_local(self, target: str) -> dict[str, Any]:
        return await self._local_executor.release_memory(target)

    def close(self) -> None:
        self._closed = True
        self.playbacks.close()
        self._local_executor.close()
        self._input_workers.shutdown()

    async def aclose(self) -> None:
        self._closed = True
        await self.playbacks.aclose()
        await self._local_executor.aclose()
        await settle_before_cancelling(asyncio.to_thread(self._input_workers.shutdown))

    async def synthesize(
        self,
        text: str,
        *,
        progress: SpeechProgress | None = None,
        usage_context: TaskUsageContext | None = None,
        on_audio: SpeechAudioCallback | None = None,
    ) -> SpeechSynthesisResult:
        """Synthesize one text string using the configured TTS binding."""

        normalized_text = text.strip() if isinstance(text, str) else ""
        if not normalized_text:
            raise SpeechConfigurationError("Text to synthesize must not be empty")

        _binding, options, target_ref = self._resolver.resolve(TASK_TEXT_TO_SPEECH)

        usage = TaskUsage(
            self._usage_recorder, TASK_TEXT_TO_SPEECH, target_ref, context=usage_context
        )
        if target_ref.kind == "local":
            try:
                async with usage.attempt():
                    return await self._local_executor.synthesize(
                        target_ref.local_id,
                        normalized_text,
                        options=options,
                        progress=progress,
                        **({"on_audio": on_audio} if on_audio is not None else {}),
                    )
            except LocalSpeechExecutionError as exc:
                raise SpeechExecutionError(str(exc)) from exc
            except LocalSpeechError as exc:
                raise SpeechUnsupportedTargetError(str(exc)) from exc

        if progress is not None:
            progress.update("synthesizing")
        provider_client = ProviderSpeechClient.from_runtime(
            self._runtime, target_ref, usage_observer=usage
        )
        try:
            return await provider_client.synthesize(
                normalized_text,
                options=options,
                **({"on_audio": on_audio} if on_audio is not None else {}),
            )
        except SpeechError:
            raise
        except ProviderOutcomeUnknownError as exc:
            _LOGGER.warning(
                "Speech synthesis failed for target=%s: %s",
                target_ref.target,
                exc,
            )
            raise SpeechOutcomeUnknownError(
                str(exc),
                operation_key=exc.operation_key,
            ) from exc
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

    async def synthesize_artifact(
        self,
        text: str,
        *,
        progress: SpeechProgress | None = None,
        usage_context: TaskUsageContext | None = None,
        on_audio: SpeechAudioCallback | None = None,
    ) -> SpeechArtifact:
        """Synthesize speech and persist it as a runtime artifact."""

        result = await self.synthesize(
            text, progress=progress, usage_context=usage_context, on_audio=on_audio
        )
        stored = await settle_before_cancelling(
            asyncio.to_thread(
                self._artifacts.write,
                result.audio,
                extension=_extension_for_audio(result.media_type, result.format),
                media_type=result.media_type,
            )
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
