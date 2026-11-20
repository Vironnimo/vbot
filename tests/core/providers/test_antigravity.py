"""The subscription Adapter presents vBot's prompt, history and Tool surface to the CLI."""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from core.providers import antigravity
from core.providers._antigravity_native import AntigravityNative
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD, RequestContextText
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
    assert frames[0]["replay_steps"] == ()
    assert "Use the vbot MCP server for tool calls." in frames[0]["prompt"].splitlines()[0]
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
    assert "Answer directly without calling tools." in sent.splitlines()[0]
    assert "@" not in sent
    assert prompt not in sent
    records = [json.loads(line) for line in sent.splitlines()[1:]]
    assert records == [
        {"role": "user", "content": user_text, "user_input": True},
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


@pytest.mark.asyncio
async def test_subscription_replays_large_results_and_steers_in_bounded_native_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns: list[dict[str, Any]] = []

    @asynccontextmanager
    async def ready(_self: AntigravityNative) -> AsyncIterator[Path]:
        yield tmp_path / "agy"

    async def native_turn(_executable: str, **kwargs: Any) -> AsyncGenerator[dict]:
        turns.append(kwargs)
        yield {"type": "finish", "reason": "stop"}

    monkeypatch.setattr(AntigravityNative, "turn", ready)
    monkeypatch.setattr(antigravity, "stream_turn", native_turn)
    registry = ProviderRegistry.load(Path(__file__).resolve().parents[3] / "resources")
    adapter = antigravity.AntigravityAdapter(
        registry.get("antigravity"), temporary_files=TemporaryFileManager(tmp_path)
    )
    content = ('file @🛰: "ä"\n' * 5_000) + "final evidence"
    steer = "[Ana|001|user]: End the search and explain the final evidence."
    args = {"output": 'edit file 🛰 @x "\ndata ' * 2_900}
    calls = [
        {"id": "one", "name": "write", "arguments": args},
        {"id": "two", "name": "edit", "arguments": {"edit": "keep @final"}},
    ]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Search the log."},
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "one", "content": content},
        {"role": "tool", "tool_call_id": "two", "content": "changed"},
        {"role": "user", "content": steer},
        {
            "role": "user",
            "content": RequestContextText("<system-reminder>Context only.</system-reminder>"),
        },
    ]
    await adapter.send(messages, model_id="model-high")
    [turn] = turns
    steps = turn["replay_steps"]
    assert 1 < len(steps) <= 8
    assert "user_input" in turn["prompt"] and steer not in turn["prompt"]
    assert all(len(step.encode("utf-8")) <= 48_000 and "@" not in step for step in steps)
    records = [json.loads(line) for step in steps for line in step.splitlines()]
    parts = [
        record for record in records if record["role"] == "tool" and record["tool_call_id"] == "one"
    ]
    assert len(parts) > 1
    assert [part["part"] for part in parts] == list(range(1, len(parts) + 1))
    assert {part["message"] for part in parts} == {3}
    assert all(part["tool_call_id"] == "one" and not part["is_error"] for part in parts)
    assert "".join(part["content"] for part in parts) == content
    assert [record["content"] for record in records if record.get("user_input")] == [
        "Search the log.",
        steer,
    ]
    for index, call in enumerate(calls, start=1):
        pieces = [row for row in records if row.get("call") == index]
        assert all(
            row["name"] == call["name"] and row["tool_call_id"] == call["id"] for row in pieces
        )
        assert json.loads("".join(row["arguments_json"] for row in pieces)) == call["arguments"]
    assert records[-2]["content"] == steer and records[-1]["content"] == messages[-1]["content"]
    # Keeping each 25 kB message intact would require 14 steps. Its complete
    # contents fit in eight when the unused space carries the next message's parts.
    medium = [{"role": "user", "content": f"{i:02d}" * 12_500} for i in range(14)]
    await adapter.send(medium, model_id="model-high")
    packed = turns[-1]["replay_steps"]
    assert len(packed) == 8 and all(len(step.encode("utf-8")) <= 48_000 for step in packed)
    assert "".join(
        json.loads(line)["content"] for step in packed for line in step.splitlines()
    ) == "".join(message["content"] for message in medium)
    await adapter.aclose()


@pytest.mark.asyncio
async def test_subscription_continues_reads_and_rebuilds_changed_history_without_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def ready(_self: AntigravityNative) -> AsyncIterator[Path]:
        yield tmp_path / "agy"

    monkeypatch.setattr(AntigravityNative, "turn", ready)
    batches = [2, 6, 7, 8]
    requests = []
    sent_calls = 0

    async def packet(_executable: str, **kwargs: Any) -> AsyncGenerator[dict]:
        nonlocal sent_calls
        requests.append(kwargs)
        turn = len(requests) - 1
        if turn >= len(batches):
            yield {"type": "content_delta", "text": "Report."}
            yield {"type": "finish", "reason": "stop"}
            return
        for _ in range(batches[turn]):
            sent_calls += 1
            yield {
                "type": "tool_call_delta",
                "id": f"id-{sent_calls}",
                "name_delta": "read",
                "arguments_delta": json.dumps({"file": f"file-{sent_calls}"}),
            }
        yield {"type": "finish", "reason": "tool_calls"}

    monkeypatch.setattr(antigravity, "stream_turn", packet)
    registry = ProviderRegistry.load(Path(__file__).resolve().parents[3] / "resources")
    native = AntigravityNative(temporary_files=TemporaryFileManager(tmp_path))
    adapter = antigravity.AntigravityAdapter(registry.get("antigravity"), native=native)
    key = adapter.request_context_kwargs(agent_id="coder", session_id="session")
    tools = [{"name": "read", "description": "Read a file.", "parameters": {"type": "object"}}]
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Find the cause."}]
    for size in batches:
        result = await adapter.send(messages, model_id="hydrogen", tools=tools, **key)
        calls = result["tool_calls"]
        assert len(calls) == size
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        for call in calls:
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": call["id"] + "Z" * 43_000}
            )
    messages.append({"role": "user", "content": "Report now."})
    await adapter.send(messages, model_id="hydrogen", tools=tools, **key)
    profile = requests[0]["conversation"]
    assert profile.root.is_dir() and all(r["conversation"] is profile for r in requests)
    assert not any(r["seed_batches"] for r in requests)
    newest = "\n".join(requests[-1]["replay_steps"])
    assert "Report now." in newest and "id-23" in newest
    assert '"tool_call_id":"id-1"' not in newest and "Find the cause." not in newest

    # An edit requires a cold rebuild, including material exceeding an entire staging batch.
    messages[0] = {"role": "user", "content": "A different assignment."}
    await adapter.send(messages, model_id="hydrogen", tools=tools, **key)
    rebuilt = requests[-1]
    assert rebuilt["conversation"] is not profile and not profile.root.exists()
    assert len(rebuilt["seed_batches"]) == 2
    packets = [*rebuilt["seed_batches"], rebuilt["replay_steps"]]
    frames = [json.loads(line) for batch in packets for step in batch for line in step.splitlines()]
    assert [r["content"] for r in frames if r.get("user_input")] == [
        "A different assignment.",
        "Report now.",
    ]
    for number in range(1, 24):
        key_id = f"id-{number}"
        assert (
            "".join(
                r["content"] for r in frames if r["role"] == "tool" and r["tool_call_id"] == key_id
            )
            == key_id + "Z" * 43_000
        )
    assert (
        adapter.request_context_kwargs(
            agent_id="coder", session_id="session", request_kind="compaction"
        )
        == {}
    )
    await native.conversations.close_sessions({("coder", "session")})
    assert not rebuilt["conversation"].root.exists()
    await native.aclose()
