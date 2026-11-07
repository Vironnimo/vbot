"""Native Claude stand-in shared by Adapter and Chat-loop contract tests."""

from __future__ import annotations

import asyncio
import contextlib
import copy
from typing import Any

from core.providers._claude_agent_sdk import PendingTools


class FakeNative:
    instances: list[FakeNative] = []

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
            future = self.pending.expect("tool-1")
            await self.events.put(
                {
                    "type": "assistant",
                    "message": {
                        "id": "m1",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "mcp__vbot__echo",
                                "input": {"count": "2"},
                            }
                        ],
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 20,
                            "output_tokens": 3,
                        },
                    },
                }
            )

            async def complete() -> None:
                result, _ = await future
                assert result["content"][0]["text"] == "masked result"
                self.result_received.set()
                await self.answer()

            self.waiter = asyncio.create_task(complete())
        else:
            await self.answer()

    async def answer(self) -> None:
        await self.events.put(
            {
                "type": "assistant",
                "message": {
                    "id": "m2",
                    "content": [{"type": "text", "text": "Done"}],
                    "usage": {"input_tokens": 12, "output_tokens": 4},
                },
            }
        )
        await self.events.put(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {"input_tokens": 9999, "output_tokens": 9999},
            }
        )

    async def stop(self) -> None:
        self.closed = True
        self.pending.clear()
        if self.waiter:
            self.waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.waiter

    def abort(self) -> None:
        self.closed = True
