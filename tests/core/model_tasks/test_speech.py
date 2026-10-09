"""Tests for the provider-neutral speech service."""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override
from unittest.mock import AsyncMock, MagicMock, patch

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
from core.model_tasks.constants import TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH
from core.model_tasks.speech_input import (
    PreparedTranscriptionAudio,
    SpeechInputError,
    TranscriptionInputPolicy,
)
from core.model_tasks.speech_local import LocalSpeechExecutionError
from core.model_tasks.speech_types import SpeechAudioCallback, SpeechAudioChunk, SpeechBusyError
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
        pytest.raises(SpeechConfigurationError),
    ):
        await service.transcribe(b"audio")

    assert caplog.records


@pytest.mark.asyncio
async def test_synthesize_artifact_persists_metadata(tmp_path: Path) -> None:
    service = SpeechService(
        _TtsModelTasks(), cast(Any, object()), tmp_path, local_executor=_LocalTts()
    )
    chunks = []

    async def on_audio(chunk: SpeechAudioChunk) -> None:
        chunks.append(chunk)
        assert not DataDirectoryLayout(tmp_path).speech.exists()

    artifact = await service.synthesize_artifact("hello", on_audio=on_audio)
    assert chunks == [SpeechAudioChunk(b"\0\0", 24000)]

    assert artifact.media_type == "audio/mpeg"
    assert artifact.size_bytes == 5
    assert artifact.file_path.parent == DataDirectoryLayout(tmp_path).speech
    assert artifact.file_path.read_bytes() == b"audio"
    assert (
        service.get_artifact(artifact.id).to_dict()["url"] == f"/api/speech/artifacts/{artifact.id}"
    )


@pytest.mark.asyncio
async def test_cancelled_artifact_publication_finishes_without_blocking_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SpeechService(
        _TtsModelTasks(), cast(Any, object()), tmp_path, local_executor=_LocalTts()
    )
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    published = []
    write = service._artifacts.write

    def publish(*args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        result = write(*args, **kwargs)
        published.append(result.id)
        return result

    monkeypatch.setattr(service._artifacts, "write", publish)
    task = asyncio.create_task(service.synthesize_artifact("hello"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.get_artifact(published[0]).file_path.read_bytes() == b"audio"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.aclose()


class _MissingModelTasks:
    def binding_for(self, _task_type: str) -> object:
        raise TaskModelError("No task model configured")


@pytest.mark.asyncio
async def test_local_inference_failure_is_execution_error(tmp_path: Path) -> None:
    executor = LocalSpeechExecutor(engines=[])
    executor.transcribe = AsyncMock(side_effect=LocalSpeechExecutionError("GPU unavailable"))  # type: ignore[method-assign]
    service = SpeechService(
        _TtsModelTasks(), cast(Any, object()), tmp_path, local_executor=executor
    )
    try:
        with pytest.raises(SpeechExecutionError, match="GPU unavailable"):
            await service.transcribe(_wav_audio_bytes())
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_local_transcription_receives_original_input_without_reading_conversion_profile(
    tmp_path: Path,
) -> None:
    executor = LocalSpeechExecutor(engines=[])
    executor.transcribe = AsyncMock(return_value=SpeechTranscriptionResult(text="hello"))  # type: ignore[method-assign]
    profile = MagicMock(side_effect=AssertionError("Local STT must not read the upload profile"))
    service = SpeechService(
        _SttModelTasks("local/parakeet", {"language": "de"}),
        cast(Any, object()),
        tmp_path,
        local_executor=executor,
        transcription_audio_getter=profile,
    )
    audio = _webm_audio_bytes()
    try:
        result = await service.transcribe(
            audio, filename="browser.webm", media_type="audio/webm;codecs=opus"
        )
        assert result.text == "hello"
        executor.transcribe.assert_awaited_once_with(
            "parakeet",
            audio,
            filename="browser.webm",
            media_type="audio/webm;codecs=opus",
            options={"language": "de"},
            progress=None,
        )
        profile.assert_not_called()
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_transcription_admission_limits_active_requests_and_discards_cancelled_waiters(
    tmp_path: Path,
) -> None:
    entered: asyncio.Queue[bytes] = asyncio.Queue()
    release = asyncio.Event()

    async def transcribe(_local_id: str, audio: bytes, **_kwargs: Any) -> SpeechTranscriptionResult:
        entered.put_nowait(audio)
        await release.wait()
        return SpeechTranscriptionResult(text="hello")

    executor = LocalSpeechExecutor(engines=[])
    executor.transcribe = AsyncMock(side_effect=transcribe)  # type: ignore[method-assign]
    service = SpeechService(
        _SttModelTasks("local/parakeet", {}),
        cast(Any, object()),
        tmp_path,
        local_executor=executor,
    )
    tasks = [asyncio.create_task(service.transcribe(audio)) for audio in (b"first", b"second")]
    try:
        assert await asyncio.wait_for(entered.get(), 1) == b"first"
        assert await asyncio.wait_for(entered.get(), 1) == b"second"
        waiter = asyncio.create_task(service.transcribe(b"cancelled"))
        queued = asyncio.create_task(service.transcribe(b"queued"))
        tasks.extend((waiter, queued))
        await asyncio.sleep(0)
        assert entered.empty()
        with pytest.raises(SpeechBusyError):
            await asyncio.wait_for(service.transcribe(b"rejected"), 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        replacement = asyncio.create_task(service.transcribe(b"replacement"))
        tasks.append(replacement)
        await asyncio.sleep(0)
        assert not replacement.done()
        assert entered.empty()
        with pytest.raises(SpeechBusyError):
            await asyncio.wait_for(service.transcribe(b"still full"), 1)
        release.set()
        await asyncio.gather(*tasks[:2], queued, replacement)
        assert {
            await asyncio.wait_for(entered.get(), 1),
            await asyncio.wait_for(entered.get(), 1),
        } == {b"queued", b"replacement"}
        assert (await service.transcribe(b"later")).text == "hello"
        assert await asyncio.wait_for(entered.get(), 1) == b"later"
        assert executor.transcribe.await_count == 5
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["local/parakeet", "openrouter/whisper-large-v3::api-key"])
async def test_transcription_rejects_oversized_source_before_target_execution(
    tmp_path: Path, target: str
) -> None:
    executor = LocalSpeechExecutor(engines=[])
    executor.transcribe = AsyncMock()  # type: ignore[method-assign]
    service = SpeechService(
        _SttModelTasks(target, {}),
        cast(Any, object()),
        tmp_path,
        local_executor=executor,
        max_input_bytes=4,
    )
    try:
        with (
            patch("core.model_tasks.speech.ProviderSpeechClient.from_runtime") as factory,
            pytest.raises(SpeechInputError) as error,
        ):
            await service.transcribe(b"oversized")
        assert error.value.too_large
        executor.transcribe.assert_not_awaited()
        factory.assert_not_called()
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_cancelled_audio_preparation_joins_worker_without_sending_to_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    release, finished, cancellation_observed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    loop = asyncio.get_running_loop()

    def prepare(
        audio: bytes, _settings: object, _policy: object, *, check_cancel: Any = None
    ) -> PreparedTranscriptionAudio:
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(1)
            assert check_cancel is not None
            try:
                check_cancel()
            except BaseException:
                cancellation_observed.set()
                raise
            return PreparedTranscriptionAudio(audio, "recording.wav", "audio/wav")
        finally:
            finished.set()

    monkeypatch.setattr("core.model_tasks.speech.prepare_transcription_audio", prepare)
    client = _CapturingProviderSpeechClient()
    service = SpeechService(_ProviderSttModelTasks(), cast(Any, object()), tmp_path)
    task: asyncio.Task[SpeechTranscriptionResult] | None = None
    try:
        with patch(
            "core.model_tasks.speech.ProviderSpeechClient.from_runtime", return_value=client
        ):
            task = asyncio.create_task(service.transcribe(_wav_audio_bytes()))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        assert finished.is_set()
        assert cancellation_observed.is_set()
        assert client.audio == b""
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await service.aclose()


@pytest.mark.asyncio
async def test_provider_transcription_preserves_independent_local_models(tmp_path: Path) -> None:
    executor = LocalSpeechExecutor(engines=[])
    executor.release_memory = AsyncMock()  # type: ignore[method-assign]
    service = SpeechService(
        _ProviderSttModelTasks(), cast(Any, object()), tmp_path, local_executor=executor
    )
    client = _CapturingProviderSpeechClient()
    try:
        with patch(
            "core.model_tasks.speech.ProviderSpeechClient.from_runtime", return_value=client
        ):
            assert (await service.transcribe(_wav_audio_bytes())).text == "hello"
        executor.release_memory.assert_not_awaited()
    finally:
        await service.aclose()


@pytest.mark.parametrize(
    ("target", "options", "task_type", "state", "preloads"),
    [
        (None, {}, TASK_SPEECH_TO_TEXT, "unavailable", False),
        (
            "openrouter/whisper-large-v3::api-key",
            {"preload": True},
            TASK_SPEECH_TO_TEXT,
            "not_local",
            False,
        ),
        ("local/parakeet", {}, TASK_SPEECH_TO_TEXT, "loading", False),
        ("local/parakeet", {"preload": True}, TASK_SPEECH_TO_TEXT, "loading", True),
        # A text-to-speech binding preloads too, but never prepares a transcription.
        ("local/chatterbox", {"preload": True}, TASK_TEXT_TO_SPEECH, "unavailable", True),
    ],
)
def test_preparation_and_preload_load_only_the_bound_local_engine(
    tmp_path: Path,
    target: str | None,
    options: dict[str, object],
    task_type: str,
    state: str,
    preloads: bool,
) -> None:
    executor = LocalSpeechExecutor(engines=[])
    executor.prepare = MagicMock(return_value="loading")  # type: ignore[method-assign]
    service = SpeechService(
        _SttModelTasks(target, options, task_type),
        cast(Any, object()),
        tmp_path,
        local_executor=executor,
    )
    try:
        assert service.prepare_transcription() == state
        assert executor.prepare.call_count == (state == "loading")
        executor.prepare.reset_mock()
        service.preload_configured()
        if preloads:
            executor.prepare.assert_called_once_with(
                cast(str, target).removeprefix("local/"), options
            )
        else:
            executor.prepare.assert_not_called()
    finally:
        executor.close()


class _SttModelTasks:
    def __init__(
        self,
        target: str | None,
        options: dict[str, object],
        bound_task: str = TASK_SPEECH_TO_TEXT,
    ) -> None:
        self._target = target
        self._options = options
        self._bound_task = bound_task

    def binding_for(self, task_type: str) -> object:
        if self._target is None or task_type != self._bound_task:
            raise TaskModelError("No task model configured")
        return SimpleNamespace(task_type=task_type, target=self._target, options=self._options)

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return dict(self._options)


class _TtsModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(task_type=task_type, target="local/piper", options={})

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}


class _LocalTts(LocalSpeechExecutor):
    @override
    async def synthesize(
        self,
        _local_id: str,
        _text: str,
        *,
        options: dict[str, object],
        progress: Any = None,
        on_audio: SpeechAudioCallback | None = None,
    ) -> SpeechSynthesisResult:
        if on_audio is not None:
            await on_audio(SpeechAudioChunk(b"\0\0", 24000))
        return SpeechSynthesisResult(audio=b"audio", media_type="audio/mpeg", format="mp3")


class _ProviderSttModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(
            task_type=task_type,
            target="openrouter/whisper-large-v3::api-key",
            options={},
        )

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}


class _ProviderTtsModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(
            task_type=task_type,
            target="openrouter/openai/gpt-4o-mini-tts::api-key",
            options={},
        )

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}


class _FailingProviderSpeechClient:
    def __init__(self, exception: Exception) -> None:
        self._exception = exception

    def transcription_input_policy(self) -> TranscriptionInputPolicy:
        return TranscriptionInputPolicy(accepted_formats=frozenset({"wav"}))

    async def transcribe(self, *_args: object, **_kwargs: object) -> object:
        raise self._exception

    async def synthesize(self, *_args: object, **_kwargs: object) -> object:
        raise self._exception


class _CapturingProviderSpeechClient:
    def __init__(
        self,
        accepted_formats: frozenset[str] = frozenset({"wav", "flac"}),
        *,
        max_audio_bytes: int = 25_000_000,
    ) -> None:
        self._policy = TranscriptionInputPolicy(
            accepted_formats=accepted_formats, max_audio_bytes=max_audio_bytes
        )
        self.audio = b""
        self.filename = ""
        self.media_type = ""

    def transcription_input_policy(self) -> TranscriptionInputPolicy:
        return self._policy

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
async def test_transcribe_normalizes_unsupported_provider_audio_to_configured_profile(
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

    try:
        with patch(
            "core.model_tasks.speech.ProviderSpeechClient.from_runtime",
            return_value=client,
        ):
            result = await service.transcribe(
                _webm_audio_bytes(),
                filename="browser.webm",
                media_type="audio/webm",
            )
    finally:
        await service.aclose()

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
async def test_transcribe_preserves_accepted_compressed_audio_and_detects_its_format(
    tmp_path: Path,
) -> None:
    client = _CapturingProviderSpeechClient(frozenset({"webm", "wav"}))
    service = SpeechService(_ProviderSttModelTasks(), cast(Any, object()), tmp_path)
    audio = _webm_audio_bytes()
    try:
        with patch(
            "core.model_tasks.speech.ProviderSpeechClient.from_runtime", return_value=client
        ):
            result = await service.transcribe(
                audio, filename="misleading.wav", media_type="audio/wav"
            )
        assert result.text == "hello"
        assert client.audio == audio
        assert client.filename == "recording.webm"
        assert client.media_type == "audio/webm"
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("too_large", [False, True], ids=["undecodable", "converted-limit"])
async def test_provider_input_rejection_remains_an_input_error_without_sending(
    tmp_path: Path, too_large: bool
) -> None:
    client = _CapturingProviderSpeechClient(frozenset({"wav"}), max_audio_bytes=1024)
    service = SpeechService(_ProviderSttModelTasks(), cast(Any, object()), tmp_path)
    audio = _webm_audio_bytes() if too_large else b"not an audio recording"
    try:
        with (
            patch("core.model_tasks.speech.ProviderSpeechClient.from_runtime", return_value=client),
            pytest.raises(SpeechInputError) as error,
        ):
            await service.transcribe(audio)
        assert error.value.too_large is too_large
        assert client.audio == b""
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_transcribe_logs_provider_error_at_warning_without_traceback(
    tmp_path: Path,
    caplog: Any,
) -> None:
    """A provider :class:`ProviderError` (a VBotError) logs at warning, no traceback."""

    service = SpeechService(_ProviderSttModelTasks(), cast(Any, object()), tmp_path)
    failing_client = _FailingProviderSpeechClient(ProviderError("rate limited"))

    try:
        with (
            patch(
                "core.model_tasks.speech.ProviderSpeechClient.from_runtime",
                return_value=failing_client,
            ),
            caplog.at_level(logging.WARNING, logger="vbot.speech"),
            pytest.raises(SpeechExecutionError, match="rate limited"),
        ):
            await service.transcribe(_wav_audio_bytes())
    finally:
        await service.aclose()

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
    assert caplog.records


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
