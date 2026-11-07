"""Native Claude stand-in shared by Adapter and Chat-loop contract tests."""

from __future__ import annotations

import asyncio
import contextlib
import copy
from typing import Any

from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
from claude_agent_sdk.types import StreamEvent

from core.providers._claude_agent_sdk import PendingTools
from core.providers._claude_agent_wire import record


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
            futures = [self.pending.expect(call.id) for call in calls]
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
