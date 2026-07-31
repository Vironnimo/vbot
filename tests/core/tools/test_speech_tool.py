"""Tests for the text_to_speech built-in tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.model_tasks import SpeechOutcomeUnknownError
from core.tools.speech import (
    TEXT_TO_SPEECH_TOOL_NAME,
    TEXT_TO_SPEECH_TOOL_PARAMETERS,
    register_text_to_speech_tool,
)
from core.tools.tools import ToolContext, ToolRegistry


@pytest.mark.asyncio
async def test_text_to_speech_tool_returns_artifact_payload(tmp_path: Path) -> None:
    audio_path = tmp_path / "artifact-1.mp3"
    registry = ToolRegistry()
    register_text_to_speech_tool(registry, _SpeechService(audio_path))
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
    )

    result = await registry.dispatch(context, {"text": "hello"})

    assert result["ok"] is True
    # The UI-facing artifacts payload stays path-free; the WebUI renders from url.
    assert result["artifacts"] == [_ARTIFACT_PAYLOAD]
    data = result["data"]
    assert isinstance(data, dict)
    # The model-facing copy carries the absolute file path for out-of-chat delivery.
    assert data["artifact"] == {**_ARTIFACT_PAYLOAD, "path": str(audio_path)}
    assert str(audio_path) in data["message"]


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

    result = await registry.dispatch(context, {"text": "hello", "unexpected": True})

    assert result["ok"] is False
    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "Unknown argument(s): unexpected",
    }


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

    async def synthesize_artifact(self, _text: str) -> object:
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            file_path=self._file_path,
            to_dict=lambda: dict(_ARTIFACT_PAYLOAD),
        )
