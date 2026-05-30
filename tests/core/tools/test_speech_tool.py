"""Tests for the text_to_speech built-in tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.tools.speech import TEXT_TO_SPEECH_TOOL_NAME, register_text_to_speech_tool
from core.tools.tools import ToolContext, ToolRegistry


@pytest.mark.asyncio
async def test_text_to_speech_tool_returns_artifact_payload(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_text_to_speech_tool(registry, _SpeechService())
    context = ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="tool-call",
        tool_name=TEXT_TO_SPEECH_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        app_root=tmp_path,
        data_root=tmp_path,
    )

    result = await registry.dispatch(context, {"text": "hello"})

    assert result["ok"] is True
    assert result["artifacts"] == [
        {
            "id": "artifact-1",
            "kind": "speech",
            "filename": "artifact-1.mp3",
            "media_type": "audio/mpeg",
            "size_bytes": 5,
            "url": "/api/speech/artifacts/artifact-1",
        }
    ]


class _SpeechService:
    async def synthesize_artifact(self, _text: str) -> object:
        return SimpleNamespace(
            to_dict=lambda: {
                "id": "artifact-1",
                "kind": "speech",
                "filename": "artifact-1.mp3",
                "media_type": "audio/mpeg",
                "size_bytes": 5,
                "url": "/api/speech/artifacts/artifact-1",
            }
        )
