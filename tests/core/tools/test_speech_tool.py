"""Tests for the text_to_speech built-in tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.model_tasks import (
    SpeechConfigurationError,
    SpeechExecutionError,
    SpeechOutcomeUnknownError,
    TaskUsageContext,
)
from core.providers.errors import ProviderAuthError
from core.runs.run import RunExecutionOwner
from core.tools.contracts import ToolContractError
from core.tools.speech import (
    TEXT_TO_SPEECH_TOOL_NAME,
    TEXT_TO_SPEECH_TOOL_PARAMETERS,
    register_text_to_speech_tool,
)
from core.tools.tools import ToolContext, ToolRegistry
from core.utils.paths import model_path


@pytest.mark.asyncio
async def test_text_to_speech_tool_returns_artifact_payload(tmp_path: Path) -> None:
    audio_path = tmp_path / "artifact-1.mp3"
    registry = ToolRegistry()
    service = _SpeechService(audio_path)
    register_text_to_speech_tool(registry, service)
    tool = registry.get(TEXT_TO_SPEECH_TOOL_NAME)
    assert tool.parameters == TEXT_TO_SPEECH_TOOL_PARAMETERS
    assert tool.open_input_schema is True
    assert "additionalProperties" not in tool.parameters
    context = ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="tool-call",
        tool_name=TEXT_TO_SPEECH_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        project_id="project",
        execution_owner=RunExecutionOwner(
            "extension", "group", "participant", "generation", "epoch"
        ),
    )

    result = await registry.dispatch(context, {"text": "hello"})

    assert result["ok"] is True
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
    data = result["data"]
    assert isinstance(data, dict)
    # The model-facing copy carries the absolute file path for out-of-chat delivery.
    assert data["artifact"] == {**_ARTIFACT_PAYLOAD, "path": model_path(audio_path)}
    assert "message" not in data


@pytest.mark.asyncio
async def test_text_to_speech_tool_rejects_unknown_arguments(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_text_to_speech_tool(registry, _SpeechService(tmp_path / "unused.mp3"))
    context = ToolContext(
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

    with pytest.raises(ToolContractError, match='"unexpected" is not a parameter'):
        await registry.dispatch(context, {"text": "hello", "unexpected": True})


@pytest.mark.asyncio
async def test_text_to_speech_tool_exposes_unknown_provider_outcome(tmp_path: Path) -> None:
    registry = ToolRegistry()
    service = _SpeechService(
        tmp_path / "unused.mp3",
        error=SpeechOutcomeUnknownError(
            "provider_outcome_unknown (operation_key=speech-op): request may have completed",
            operation_key="speech-op",
        ),
    )
    register_text_to_speech_tool(registry, service)
    context = ToolContext(
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

    result = await registry.dispatch(context, {"text": "hello"})

    assert result["error"]["code"] == "provider_outcome_unknown"
    assert result["error"]["retryable"] is False
    assert "operation_key=speech-op" in result["error"]["message"]


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(
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


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["input", "content", "Transcript"])
async def test_text_to_speech_accepts_other_names_for_the_text(tmp_path: Path, field: str) -> None:
    service = _SpeechService(tmp_path / "artifact-1.mp3")
    registry = ToolRegistry()
    register_text_to_speech_tool(registry, service)

    result = await registry.dispatch(_context(tmp_path), {field: "Hello there"})

    assert result["ok"] is True
    assert service.spoken == "Hello there"


@pytest.mark.asyncio
async def test_text_to_speech_refuses_two_different_texts(tmp_path: Path) -> None:
    service = _SpeechService(tmp_path / "artifact-1.mp3")
    registry = ToolRegistry()
    register_text_to_speech_tool(registry, service)

    with pytest.raises(ToolContractError, match="Conflicting values for text"):
        await registry.dispatch(_context(tmp_path), {"text": "Hello", "input": "Goodbye"})
    assert service.spoken is None


def _auth_failure() -> SpeechExecutionError:
    cause = ProviderAuthError('Authentication error: 401 {"error": {"message": "Invalid key"}}')
    cause.status_code = 401  # type: ignore[attr-defined]
    try:
        raise SpeechExecutionError(str(cause)) from cause
    except SpeechExecutionError as error:
        return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            _auth_failure(),
            "The text-to-speech provider rejected its credentials (HTTP 401: Invalid key). "
            "Tell the user to check that provider's API key or sign-in in Settings.",
        ),
        (
            SpeechConfigurationError("No task model configured for text_to_speech"),
            "Text to speech is not available (No task model configured for text_to_speech). "
            "Tell the user to choose a working Text to speech model in Settings under "
            "Specialized Models.",
        ),
    ],
)
async def test_text_to_speech_failures_name_the_next_step(
    tmp_path: Path, error: Exception, message: str
) -> None:
    registry = ToolRegistry()
    register_text_to_speech_tool(registry, _SpeechService(tmp_path / "unused.mp3", error=error))

    result = await registry.dispatch(_context(tmp_path), {"text": "hello"})

    assert result["error"] == {"code": "speech_error", "message": message, "retryable": False}


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

    async def synthesize_artifact(self, text: str, *, usage_context: TaskUsageContext) -> object:
        self.usage_context = usage_context
        self.spoken = text
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            file_path=self._file_path,
            to_dict=lambda: dict(_ARTIFACT_PAYLOAD),
        )
