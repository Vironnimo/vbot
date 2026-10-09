"""generate_speech: the artifact it returns, other names for the text, and what refused or
failed calls say."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.model_tasks import (
    SpeechConfigurationError,
    SpeechExecutionError,
    SpeechOutcomeUnknownError,
    TaskUsageContext,
)
from core.model_tasks.speech_playback import SpeechPlaybackStore
from core.model_tasks.speech_types import SpeechAudioChunk
from core.providers.errors import ProviderAuthError
from core.runs.run import RunExecutionOwner
from core.tools.speech import (
    GENERATE_SPEECH_TOOL_NAME,
    GENERATE_SPEECH_TOOL_PARAMETERS,
    register_generate_speech_tool,
)
from core.tools.tools import ToolContext, ToolRegistry, tool_failure_for_exception
from core.utils.paths import model_path

_ARTIFACT_PAYLOAD = {
    "id": "artifact-1",
    "kind": "speech",
    "filename": "artifact-1.mp3",
    "media_type": "audio/mpeg",
    "size_bytes": 5,
    "url": "/api/speech/artifacts/artifact-1",
}


class _SpeechService:
    def __init__(self, file_path: Path, *, error: Exception | None = None) -> None:
        self._file_path = file_path
        self._error = error
        self.spoken: str | None = None
        self.usage_context: TaskUsageContext | None = None
        self.playbacks = SpeechPlaybackStore()
        self.after_audio: Any = None

    async def synthesize_artifact(
        self, text: str, *, usage_context: TaskUsageContext, on_audio: Any
    ) -> object:
        self.usage_context = usage_context
        if self._error is not None:
            raise self._error
        self.spoken = text
        await on_audio(SpeechAudioChunk(audio=b"\x01\x00", sample_rate_hz=24_000))
        if self.after_audio is not None:
            await self.after_audio()
        return SimpleNamespace(file_path=self._file_path, to_dict=lambda: dict(_ARTIFACT_PAYLOAD))


async def _speak(
    tmp_path: Path, arguments: dict[str, Any], service: _SpeechService, **context: Any
) -> dict[str, Any]:
    registry = ToolRegistry()
    register_generate_speech_tool(registry, service)
    tool_context = ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="tool-call",
        tool_name=GENERATE_SPEECH_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )
    try:
        return await registry.dispatch(replace(tool_context, **context), arguments)
    except Exception as error:
        return tool_failure_for_exception(GENERATE_SPEECH_TOOL_NAME, error)


@pytest.mark.asyncio
async def test_speech_is_returned_as_an_artifact_for_the_run(tmp_path: Path) -> None:
    audio_path = tmp_path / "artifact-1.mp3"
    service = _SpeechService(audio_path)
    registry = ToolRegistry()
    register_generate_speech_tool(registry, service)
    tool = registry.get(GENERATE_SPEECH_TOOL_NAME)
    events = []

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        events.append((event_type, payload))
        playback = service.playbacks.get(payload["url"].rsplit("/", 1)[-1])
        assert playback is not None
        assert playback.finished_at is None
        assert await anext(playback.frames()) == struct.pack("<II", 24_000, 2) + b"\x01\x00"

    result = await _speak(
        tmp_path,
        {"text": "hello"},
        service,
        emit_hook=emit,
        project_id="project",
        execution_owner=RunExecutionOwner(
            "extension", "group", "participant", "generation", "epoch"
        ),
    )

    assert tool.parameters == GENERATE_SPEECH_TOOL_PARAMETERS
    assert "additionalProperties" not in tool.parameters
    assert tool.open_input_schema is True
    assert service.usage_context == TaskUsageContext(
        agent_id="agent",
        project_id="project",
        session_id="session",
        run_id="run",
        owner_name="extension",
        group_id="group",
    )
    # The UI-facing artifacts payload stays path-free; the WebUI renders from url.
    assert result["artifacts"] == [_ARTIFACT_PAYLOAD]
    # The model-facing copy carries the absolute file path for out-of-chat delivery.
    assert result["data"] == {"artifact": {**_ARTIFACT_PAYLOAD, "path": model_path(audio_path)}}
    assert len(events) == 1
    assert events[0][0] == "speech_playback"
    assert events[0][1]["tool_call_id"] == "tool-call"
    playback = service.playbacks.get(events[0][1]["url"].rsplit("/", 1)[-1])
    assert playback is not None
    assert [frame async for frame in playback.frames()][-1] == bytes(8)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_speech_terminates_playback_without_a_completed_artifact(
    tmp_path: Path, cancel: bool
) -> None:
    service = _SpeechService(tmp_path / "unused.mp3")
    playbacks = []

    async def emit(_event_type: str, payload: dict[str, Any]) -> None:
        playback = service.playbacks.get(payload["url"].rsplit("/", 1)[-1])
        assert playback is not None
        playbacks.append(playback)

    async def fail() -> None:
        if cancel:
            raise asyncio.CancelledError
        raise SpeechExecutionError("synthesis interrupted")

    service.after_audio = fail
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await _speak(tmp_path, {"text": "hello"}, service, emit_hook=emit)
    else:
        result = await _speak(tmp_path, {"text": "hello"}, service, emit_hook=emit)
        assert result["ok"] is False
        assert result["artifacts"] == []
    playback = playbacks[0]
    frames = [frame async for frame in playback.frames()]
    assert frames[-1][8:] == (b"cancelled" if cancel else b"failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["input", "content", "Transcript"])
async def test_other_names_for_the_text_are_spoken(tmp_path: Path, field: str) -> None:
    service = _SpeechService(tmp_path / "artifact-1.mp3")

    result = await _speak(tmp_path, {field: "Hello there"}, service)

    assert result["ok"] is True
    assert service.spoken == "Hello there"


def _auth_failure() -> SpeechExecutionError:
    cause = ProviderAuthError('Authentication error: 401 {"error": {"message": "Invalid key"}}')
    cause.status_code = 401  # type: ignore[attr-defined]
    try:
        raise SpeechExecutionError(str(cause)) from cause
    except SpeechExecutionError as error:
        return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error", "expected"),
    [
        pytest.param(
            {"text": "hello", "unexpected": True},
            None,
            {
                "code": "invalid_arguments",
                "message": "generate_speech was not run:\n"
                '- "unexpected" is not a parameter.\n'
                "generate_speech parameters: text (required).",
            },
            id="unknown-argument",
        ),
        pytest.param(
            {"text": "Hello", "input": "Goodbye"},
            None,
            {
                "code": "invalid_arguments",
                "message": 'Conflicting values for text: text is "Hello" and input is '
                '"Goodbye". Send only the intended one.',
            },
            id="two-texts",
        ),
        pytest.param(
            {"text": "hello"},
            SpeechOutcomeUnknownError(
                "provider_outcome_unknown (operation_key=speech-op): request may have completed",
                operation_key="speech-op",
            ),
            {
                "code": "provider_outcome_unknown",
                "message": "provider_outcome_unknown (operation_key=speech-op): request may "
                "have completed",
                "retryable": False,
            },
            id="outcome-unknown",
        ),
        pytest.param(
            {"text": "hello"},
            _auth_failure(),
            {
                "code": "speech_error",
                "message": "The text-to-speech provider rejected its credentials (HTTP 401: "
                "Invalid key). Tell the user to check that provider's API key or sign-in in "
                "Settings.",
                "retryable": False,
            },
            id="credentials",
        ),
        pytest.param(
            {"text": "hello"},
            SpeechConfigurationError("No task model configured for text_to_speech"),
            {
                "code": "speech_error",
                "message": "Text to speech is not available (No task model configured for "
                "text_to_speech). Tell the user to choose a working Text to speech model in "
                "Settings → Voice → Speech models.",
                "retryable": False,
            },
            id="not-configured",
        ),
    ],
)
async def test_refused_and_failed_calls_say_what_happened(
    tmp_path: Path, arguments: dict[str, Any], error: Exception | None, expected: dict[str, Any]
) -> None:
    service = _SpeechService(tmp_path / "unused.mp3", error=error)

    result = await _speak(tmp_path, arguments, service)

    assert result["error"] == expected
    assert service.spoken is None
