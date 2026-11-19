"""The subscription Adapter presents vBot's prompt, history and Tool surface to the CLI."""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from core.providers import antigravity
from core.providers._antigravity_native import AntigravityNative
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import ProviderError
from core.providers.providers import ProviderRegistry
from core.storage import TemporaryFileManager


@pytest.mark.asyncio
async def test_subscription_turn_reconstructs_history_and_hands_calls_back_to_vbot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def ready(_self: AntigravityNative) -> AsyncIterator[Path]:
        yield tmp_path / "agy"

    frames: list[dict[str, Any]] = []

    async def native_turn(_executable: str, **kwargs: Any) -> AsyncGenerator[dict]:
        frames.append(kwargs)
        if len(frames) == 1:
            yield {
                "type": "tool_call_delta",
                "id": "call1",
                "name_delta": "lookup",
                "arguments_delta": '{"name":"test"}',
            }
            yield {"type": "finish", "reason": "tool_calls"}
        else:
            yield {"type": "content_delta", "text": "done"}
            yield {"type": "finish", "reason": "stop"}

    monkeypatch.setattr(AntigravityNative, "turn", ready)
    monkeypatch.setattr(antigravity, "stream_turn", native_turn)
    provider = ProviderRegistry.load(Path(__file__).resolve().parents[3] / "resources")
    adapter = antigravity.AntigravityAdapter(
        provider.get("antigravity"), temporary_files=TemporaryFileManager(tmp_path)
    )
    prompt = "You are the Session Agent.\n# Context\nFollow this exact prompt."
    user_text = "/help\nRead @private.txt literally.\nDescribe the result."
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_text},
    ]
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    tools = [{"name": "lookup", "description": "Look up a name.", "parameters": schema}]
    first = await adapter.send(messages, model_id="model-high", tools=tools)
    assert frames[0]["model"] == "model-high"
    assert frames[0]["system_prompt"] == prompt
    assert frames[0]["tools"] == [
        {"name": "lookup", "description": "Look up a name.", "inputSchema": schema}
    ]
    assert first["terminal_outcome"] == "tool_calls"
    assert first["tool_calls"] == [{"id": "call1", "name": "lookup", "arguments": {"name": "test"}}]
    result = {"ok": True, "data": {"output": "masked @data"}, "artifacts": [], "error": None}
    messages.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": first["tool_calls"],
                "reasoning": "private",
            },
            {
                "role": "tool",
                "tool_call_id": "call1",
                "content": json.dumps(result),
                TOOL_RESULT_CONTENT_BLOCKS_FIELD: [{"type": "text", "text": "supplement"}],
            },
        ]
    )
    second = await adapter.send(messages, model_id="model-high", tools=tools, tool_choice="none")
    assert second["content"] == "done"
    assert second["terminal_outcome"] == "stop"
    assert frames[1]["tools"] == []
    sent = frames[1]["prompt"]
    assert "@" not in sent
    assert prompt not in sent
    records = [json.loads(line) for line in sent.splitlines()[1:]]
    assert records == [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": "", "tool_calls": first["tool_calls"]},
        {
            "role": "tool",
            "tool_call_id": "call1",
            "content": "masked @data\n\nsupplement",
            "is_error": False,
        },
    ]
    with pytest.raises(ProviderError, match="text only"):
        await adapter.send(
            [
                {
                    "role": "user",
                    "content": [{"type": "media", "media_type": "image/png", "base64": "aa"}],
                }
            ],
            model_id="model-high",
        )
    assert len(frames) == 2
    await adapter.aclose()
