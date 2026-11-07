"""The real SDK control protocol, with only native process I/O replaced."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import claude_agent_sdk as sdk
import pytest

from core.providers._claude_agent_sdk import PendingTools, options
from core.providers._claude_agent_sessions import NativeSession
from core.providers._claude_agent_wire import StreamState
from tests.core.providers.claude_agent_test_support import ClaudeProtocolTransport


@pytest.mark.asyncio
async def test_real_sdk_native_session_control_protocol_preserves_canonical_tool_results() -> None:
    transport = ClaudeProtocolTransport()
    pending = PendingTools(
        [
            {
                "name": "echo",
                "description": "Echo the given count",
                "parameters": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            }
        ]
    )

    def client_factory(options: sdk.ClaudeAgentOptions) -> sdk.ClaudeSDKClient:
        return sdk.ClaudeSDKClient(options=options, transport=transport)

    native = NativeSession(
        options(sdk, "not-launched", pending=pending),
        pending,
        SimpleNamespace(ClaudeSDKClient=client_factory),
    )

    async def step() -> tuple[StreamState, list[dict[str, Any]]]:
        state = StreamState(pending.names, subscription=True)
        parts = []
        while True:
            message = await native.events.get()
            if isinstance(message, Exception):
                raise message
            parts.extend(state.consume(message))
            if any(part["type"] == "finish" for part in parts):
                return state, parts

    try:
        async with asyncio.timeout(2):
            assert (await native.start())["subscription"]
            await native.send([{"type": "text", "text": "Work"}])
            first, parts = await step()
            assert parts[-1] == {"type": "finish", "reason": "tool_calls"}
            assert first.calls == [{"id": "tool-1", "name": "echo", "arguments": {"count": "2"}}]
            assert first.usage["output_tokens"] == 3
            future = pending.expect("tool-1", "echo")
            assert not future.done()
            assert transport.tool_result is None
            release = await pending.prepare_results(
                [{"tool_call_id": "tool-1", "content": "masked result"}]
            )
            release()
            last, parts = await step()
            assert last.text == "Done" and last.usage["input_tokens"] == 12
            assert last.usage["output_tokens"] == 4
            assert parts[-1] == {"type": "finish", "reason": "stop"}
            assert transport.tool_result == {
                "content": [{"type": "text", "text": "masked result"}],
                "isError": False,
            }
            assert pending.waiting == {}
    finally:
        await native.stop()
    assert transport.control == ["initialize", "mcp-init", "mcp-ready", "before", "call", "after"]
    assert transport.closed and native.task.done()
