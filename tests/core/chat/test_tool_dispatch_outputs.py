"""Tests for tool dispatch outputs."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.chat.messages import JsonObject, ToolCall
from core.runs import Run
from core.tools import (
    ToolContext,
    ToolRegistry,
    tool_success,
)
from tests.core.chat.tool_dispatch_test_support import (
    _build_runtime_and_agent,
    _build_session,
    _decode_tool_result,
    _dispatch_tool_calls,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class TestReadMediaOutputs:
    """``read_media`` artifacts surface as rich Tool Result descriptors."""

    @pytest.mark.asyncio
    async def test_read_media_artifact_becomes_media_output(self, tmp_path: Path) -> None:
        def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            return tool_success(
                {"content": "loaded"},
                artifacts=[
                    {
                        "kind": "read_media",
                        "attachment_id": "att-1",
                        "filename": "diagram.png",
                        "media_type": "image/png",
                    }
                ],
            )

        tools = ToolRegistry()
        tools.register("read", "Reads media.", {"type": "object"}, handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="read", arguments={})]

        tool_messages, media_outputs = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        assert len(tool_messages) == 1
        assert media_outputs == [
            {
                "tool_call_id": "call-1",
                "attachment_id": "att-1",
                "filename": "diagram.png",
                "media_type": "image/png",
            }
        ]

    @pytest.mark.asyncio
    async def test_non_read_media_artifacts_produce_no_media_output(self, tmp_path: Path) -> None:
        def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            return tool_success(
                {"message": "image generated"},
                artifacts=[{"kind": "image", "url": "/api/x", "id": "img-1"}],
            )

        tools = ToolRegistry()
        tools.register("image_generation", "Generates images.", {"type": "object"}, handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="image_generation", arguments={})]

        tool_messages, media_outputs = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        assert len(tool_messages) == 1
        assert media_outputs == []


class TestUnexpectedToolCrashLogging:
    """An unexpected handler crash is logged before being folded into a result."""

    @pytest.mark.asyncio
    async def test_unexpected_tool_crash_logs_error_with_traceback(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        def crashing_handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
            raise RuntimeError("handler exploded")

        tools = ToolRegistry()
        tools.register("boom", "Tool that crashes.", {"type": "object"}, crashing_handler)
        runtime, agent = _build_runtime_and_agent(tmp_path, tools)
        session = _build_session(tmp_path)
        run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
        tool_calls = [ToolCall(id="call-1", name="boom", arguments={})]

        caplog.set_level(logging.ERROR, logger="vbot.chat")
        messages, _ = await _dispatch_tool_calls(
            runtime, agent, tool_calls, session, run, nesting_depth=0
        )

        # The crash is converted to a tool_execution_error envelope (run continues)...
        result = _decode_tool_result(messages[0].content)
        assert result["ok"] is False
        assert result["error"]["code"] == "tool_execution_error"

        # ...and logged at ERROR with the originating exception and tool name.
        error_records = [
            record
            for record in caplog.records
            if record.name == "vbot.chat"
            and record.levelno == logging.ERROR
            and "crashed unexpectedly" in record.getMessage()
        ]
        assert len(error_records) == 1
        record = error_records[0]
        assert "boom" in record.getMessage()
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], RuntimeError)
