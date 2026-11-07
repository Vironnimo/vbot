"""Native Claude stand-in shared by Adapter and Chat-loop contract tests."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, override

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
from claude_agent_sdk._internal.transport import Transport
from claude_agent_sdk.types import StreamEvent

from core.providers import _claude_agent_sessions as sessions_module
from core.providers._claude_agent_sdk import PendingTools
from core.providers._claude_agent_sessions import ClaudeAgentSessions
from core.providers._claude_agent_wire import record
from core.providers.claude_agent import ClaudeAgentAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


class FakeNative:
    instances: list[FakeNative] = []
    tool_count = 1

    def __init__(self, options: Any, pending: PendingTools, _sdk: Any) -> None:
        self.options = options
        self.pending = pending
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.info = {
            "connected": True,
            "status": "ready",
            "models": [{"value": "opus"}],
            "subscription": True,
        }
        self.ready.set_result(self.info)
        self.version = "2.1.257"
        self.client = self
        self.closed = False
        self.sent: list[Any] = []
        self.result_received = asyncio.Event()
        self.waiter: asyncio.Task[None] | None = None
        self.instances.append(self)

    async def start(self) -> dict:
        return self.info

    def begin_step(self) -> None:
        pass

    async def set_model(self, _model: str) -> None:
        pass

    async def get_context_usage(self) -> dict:
        return {"rawMaxTokens": 1_000_000, "maxTokens": 800_000}

    async def send(self, blocks: list[dict], *, priority: bool = False) -> None:
        self.sent.append((copy.deepcopy(blocks), priority))
        if self.pending.tools and len(self.sent) == 1:
            calls = [
                ToolUseBlock(f"tool-{index + 1}", "mcp__vbot__echo", {"count": "2"})
                for index in range(self.tool_count)
            ]
            futures = [
                self.pending.expect(call.id, self.pending.names[call.name]) for call in calls
            ]
            await self.response(
                "m1",
                calls,
                {"input_tokens": 10, "cache_read_input_tokens": 20, "output_tokens": 3},
                "tool_use",
            )

            async def complete() -> None:
                for future in futures:
                    result, _ = await future
                    assert result["content"][0]["text"] == "masked result"
                self.result_received.set()
                await self.answer()

            self.waiter = asyncio.create_task(complete())
        else:
            await self.answer()

    async def answer(self) -> None:
        await self.response(
            "m2", [TextBlock("Done")], {"input_tokens": 12, "output_tokens": 4}, "end_turn"
        )
        await self.events.put(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {"input_tokens": 9999, "output_tokens": 9999},
            }
        )

    async def response(
        self,
        message_id: str,
        blocks: list[Any],
        usage: dict[str, int],
        stop_reason: str | None,
        *,
        message_stop: bool = True,
    ) -> None:
        """Match SDK block-wise AssistantMessages before their block/message stops."""

        async def event(value: dict[str, Any]) -> None:
            await self.events.put(record(StreamEvent("event", "native", value)))

        initial_usage = {**usage, "output_tokens": 1}
        await event(
            {"type": "message_start", "message": {"id": message_id, "usage": initial_usage}}
        )
        for index, block in enumerate(blocks):
            wire_block = (
                {"type": "text", "text": ""}
                if isinstance(block, TextBlock)
                else {"type": "tool_use", "id": block.id, "name": block.name, "input": {}}
            )
            await event(
                {"type": "content_block_start", "index": index, "content_block": wire_block}
            )
            if isinstance(block, TextBlock):
                await event(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": block.text},
                    }
                )
            await self.events.put(
                record(
                    AssistantMessage(
                        content=[block], model="opus", message_id=message_id, usage=initial_usage
                    )
                )
            )
            await event({"type": "content_block_stop", "index": index})
        if stop_reason is not None:
            await event(
                {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": usage}
            )
        if message_stop:
            await event({"type": "message_stop"})

    async def stop(self) -> None:
        self.closed = True
        self.pending.clear()
        if self.waiter:
            self.waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.waiter

    def abort(self) -> None:
        self.closed = True


class ClaudeProtocolTransport(Transport):
    """A single Claude turn: MCP handshake, one Tool, and its final answer."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.initialized = asyncio.Event()
        self.owner: asyncio.Task[Any] | None = None
        self.closed = False
        self.hooks: dict[str, Any] = {}
        self.control: list[str] = []
        self.tool_result: dict[str, Any] | None = None

    def emit(self, *messages: dict[str, Any]) -> None:
        for message in messages:
            self.queue.put_nowait(message)

    def request(self, request_id: str, subtype: str, **payload: Any) -> None:
        self.emit(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": subtype, **payload},
            }
        )

    def event(self, event: dict[str, Any]) -> None:
        self.emit({"type": "stream_event", "uuid": "event", "session_id": "native", "event": event})

    def hook(self, event: str, callback: str) -> None:
        self.request(
            callback,
            "hook_callback",
            callback_id=self.hooks[event][0]["hookCallbackIds"][0],
            tool_use_id="tool-1",
            input={
                "hook_event_name": event,
                "tool_name": "mcp__vbot__echo",
                "tool_use_id": "tool-1",
                "tool_input": {"count": "2"},
            },
        )

    @override
    async def connect(self) -> None:
        self.owner = asyncio.current_task()

    @override
    async def write(self, data: str) -> None:
        message = json.loads(data)
        if message["type"] == "control_request":
            assert message["request"]["subtype"] == "initialize"
            self.control.append("initialize")
            self.hooks = message["request"]["hooks"]
            self.emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": {
                            "claude_code_version": "2.1.302",
                            "account": {"subscriptionType": "Claude Team"},
                            "models": [{"value": "opus"}],
                        },
                    },
                }
            )
            # Python's SDK uses the ordinary MCP handshake. OpenCode's TS
            # recordings instead park prebuilt manifests during initialization.
            self.request(
                "mcp-init",
                "mcp_message",
                server_name="vbot",
                message={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "loopback", "version": "1"},
                    },
                },
            )
            return
        if message["type"] == "user":
            await self.initialized.wait()
            self.event(
                {"type": "message_start", "message": {"id": "first", "usage": {"input_tokens": 10}}}
            )
            self.emit(
                {
                    "type": "assistant",
                    "message": {
                        "id": "first",
                        "model": "opus",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "mcp__vbot__echo",
                                "input": {"count": "2"},
                            }
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                }
            )
            # Real recordings can request PreToolUse before message_stop.
            self.hook("PreToolUse", "before")
            self.event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 3},
                }
            )
            self.event({"type": "message_stop"})
            return
        assert message["type"] == "control_response"
        response = message["response"]
        assert response["subtype"] == "success", response
        request_id = response["request_id"]
        self.control.append(request_id)
        if request_id == "mcp-init":
            assert "error" not in response["response"]["mcp_response"]
            self.request(
                "mcp-ready",
                "mcp_message",
                server_name="vbot",
                message={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        elif request_id == "mcp-ready":
            self.initialized.set()
        elif request_id == "before":
            assert response["response"] == {}
            self.request(
                "call",
                "mcp_message",
                server_name="vbot",
                message={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "echo",
                        "arguments": {"count": "2"},
                        "_meta": {"claudecode/toolUseId": "tool-1", "progressToken": 2},
                    },
                },
            )
        elif request_id == "call":
            self.tool_result = response["response"]["mcp_response"]["result"]
            self.hook("PostToolUse", "after")
        else:
            assert request_id == "after"
            self.emit(
                {
                    "type": "assistant",
                    "message": {
                        "id": "last",
                        "model": "opus",
                        "content": [{"type": "text", "text": "Done"}],
                        "usage": {"input_tokens": 12, "output_tokens": 4},
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 2,
                    "session_id": "native",
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "usage": {"input_tokens": 22, "output_tokens": 7},
                },
            )

    @override
    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while (message := await self.queue.get()) is not None:
            yield message

    @override
    async def close(self) -> None:
        assert asyncio.current_task() is self.owner
        self.closed = True
        self.queue.put_nowait(None)

    @override
    def is_ready(self) -> bool:
        return not self.closed

    @override
    async def end_input(self) -> None:
        pass


TOOLS: list[dict[str, Any]] = [
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
MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "Synthetic vBot Agent identity"},
    {"role": "user", "content": "Work"},
]


@pytest.fixture
def native_registry(monkeypatch: pytest.MonkeyPatch) -> ClaudeAgentSessions:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    FakeNative.instances = []
    monkeypatch.setattr(
        sessions_module,
        "load_sdk",
        lambda: SimpleNamespace(ClaudeAgentOptions=ClaudeAgentOptions, HookMatcher=HookMatcher),
    )
    monkeypatch.setattr(sessions_module, "executable", lambda: "C:/synthetic/claude.exe")
    return ClaudeAgentSessions(native_factory=FakeNative)  # type: ignore[arg-type]


def adapter(registry: ClaudeAgentSessions) -> ClaudeAgentAdapter:
    return ClaudeAgentAdapter(
        ProviderConfig(
            id="claude-agent",
            name="Claude Agent",
            adapter="claude_agent",
            base_url="",
            connections=[
                ConnectionConfig(
                    id="subscription",
                    type="external",
                    label="Claude Code login",
                    auth=AuthConfig(header="", prefix=""),
                )
            ],
        ),
        sessions=registry,
    )


async def collect(client: ClaudeAgentAdapter, messages: list[dict], **kwargs: Any) -> list[dict]:
    return [part async for part in client.stream(messages, model_id="opus", **kwargs)]
