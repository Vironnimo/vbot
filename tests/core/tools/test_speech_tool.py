"""text_to_speech: the artifact it returns, other names for the text, and what refused or
failed calls say."""

from __future__ import annotations

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
from core.providers.errors import ProviderAuthError
from core.runs.run import RunExecutionOwner
from core.tools.speech import (
    TEXT_TO_SPEECH_TOOL_NAME,
    TEXT_TO_SPEECH_TOOL_PARAMETERS,
    register_text_to_speech_tool,
)
from core.tools.tools import ToolContext, ToolRegistry, tool_failure
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

    async def synthesize_artifact(self, text: str, *, usage_context: TaskUsageContext) -> object:
        self.usage_context = usage_context
        if self._error is not None:
            raise self._error
        self.spoken = text
        return SimpleNamespace(file_path=self._file_path, to_dict=lambda: dict(_ARTIFACT_PAYLOAD))


async def _speak(
    tmp_path: Path, arguments: dict[str, Any], service: _SpeechService, **context: Any
) -> dict[str, Any]:
    registry = ToolRegistry()
    register_text_to_speech_tool(registry, service)
    tool_context = ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="tool-call",
        tool_name=TEXT_TO_SPEECH_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )
    try:
        return await registry.dispatch(replace(tool_context, **context), arguments)
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))


@pytest.mark.asyncio
async def test_speech_is_returned_as_an_artifact_for_the_run(tmp_path: Path) -> None:
    audio_path = tmp_path / "artifact-1.mp3"
    service = _SpeechService(audio_path)
    registry = ToolRegistry()
    register_text_to_speech_tool(registry, service)
    tool = registry.get(TEXT_TO_SPEECH_TOOL_NAME)

    result = await _speak(
        tmp_path,
        {"text": "hello"},
        service,
        project_id="project",
        execution_owner=RunExecutionOwner(
            "extension", "group", "participant", "generation", "epoch"
        ),
    )

    assert tool.parameters == TEXT_TO_SPEECH_TOOL_PARAMETERS
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
                "message": "text_to_speech was not run:\n"
                '- "unexpected" is not a parameter.\n'
                "text_to_speech parameters: text (required).",
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
                "Settings under Specialized Models.",
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
