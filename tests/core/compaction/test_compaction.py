"""Tests for compaction primitives and strategy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from core.chat import ChatMessage
from core.compaction import (
    TOOL_RESULT_CONTENT_PLACEHOLDER,
    CompactionError,
    CompactionService,
    CompactionSettings,
    SummarizationStrategy,
    find_tail_boundary,
)
from core.utils.tokens import estimate_message_tokens, estimate_tokens

JsonObject = dict[str, Any]
TIMESTAMP = "2026-05-19T12:00:00+00:00"
FIXED_TIMESTAMP = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)


class StubStorage:
    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self.requested_fragments: list[str] = []

    def read_prompt_fragment(self, name: str) -> str:
        self.requested_fragments.append(name)
        return self.prompt


class StubSummaryAdapter:
    def __init__(self, response: JsonObject) -> None:
        self.response = response
        self.requests: list[JsonObject] = []

    async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
        self.requests.append(
            {
                "messages": [dict(message) for message in messages],
                "model_id": model_id,
                "kwargs": dict(kwargs),
            }
        )
        return dict(self.response)

    def normalize_response(self, response: JsonObject) -> JsonObject:
        return response


class NoopStrategy:
    async def compact(
        self,
        messages: list[ChatMessage],
        *,
        agent: Any,
        summary_adapter: Any,
        summary_model_id: str,
        storage: Any,
        settings: CompactionSettings,
    ) -> ChatMessage:
        raise AssertionError("NoopStrategy.compact should not be called in this test")


def _user(message_id: str, content: str) -> ChatMessage:
    return ChatMessage.from_dict(
        {
            "id": message_id,
            "timestamp": TIMESTAMP,
            "role": "user",
            "content": content,
        }
    )


def _assistant(
    message_id: str,
    content: str | None,
    *,
    tool_calls: list[JsonObject] | None = None,
) -> ChatMessage:
    payload: JsonObject = {
        "id": message_id,
        "timestamp": TIMESTAMP,
        "role": "assistant",
        "model": "openrouter/anthropic/claude-sonnet-4",
        "content": content,
    }
    if tool_calls is not None:
        payload["tool_calls"] = tool_calls
    return ChatMessage.from_dict(payload)


def _tool(message_id: str, *, tool_call_id: str, name: str, content: str) -> ChatMessage:
    return ChatMessage.from_dict(
        {
            "id": message_id,
            "timestamp": TIMESTAMP,
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        }
    )


def _content_tokens(content: Any) -> int:
    tokens, _ = estimate_tokens(str(content))
    return tokens


def test_find_tail_boundary_budget_reached_mid_history() -> None:
    messages = [
        _user("u1", "aaaaaaaa"),
        _assistant("a1", "bbbbbbbb"),
        _user("u2", "cccccccc"),
        _assistant("a2", "dddddddd"),
        _user("u3", "eeeeeeee"),
        _assistant("a3", "ffffffff"),
    ]

    boundary = find_tail_boundary(messages, tail_tokens=10)

    assert boundary == "u2"


def test_find_tail_boundary_exact_boundary_keeps_complete_tool_cycle() -> None:
    messages = [
        _user("u1", "turn one user"),
        _assistant(
            "a1",
            None,
            tool_calls=[{"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}],
        ),
        _tool("t1", tool_call_id="call_1", name="read_file", content="tool result content"),
        _assistant("a1f", "turn one assistant final"),
        _user("u2", "tail user"),
        _assistant("a2", "tail assistant"),
    ]
    tail_turn_tokens = _content_tokens("tail user") + _content_tokens("tail assistant")

    boundary = find_tail_boundary(messages, tail_tokens=tail_turn_tokens)

    assert boundary == "u2"


def test_find_tail_boundary_counts_tool_calls_in_current_tail_turn() -> None:
    messages = [
        _user("u1", "previous small turn"),
        _assistant("a1", "previous small answer"),
        _user("u2", "current turn"),
        _assistant(
            "a2",
            None,
            tool_calls=[
                {
                    "id": "call_2",
                    "name": "write_file",
                    "arguments": {"payload": "x" * 20_000},
                }
            ],
        ),
        _tool("t2", tool_call_id="call_2", name="write_file", content="ok"),
    ]

    boundary = find_tail_boundary(messages, tail_tokens=1_000)

    assert boundary == "u2"


def test_find_tail_boundary_rejects_empty_messages() -> None:
    with pytest.raises(CompactionError, match="empty message list"):
        find_tail_boundary([], tail_tokens=100)


def test_find_tail_boundary_with_large_budget_returns_first_turn() -> None:
    messages = [
        _user("u1", "turn one"),
        _assistant("a1", "assistant one"),
        _user("u2", "turn two"),
        _assistant("a2", "assistant two"),
    ]

    boundary = find_tail_boundary(messages, tail_tokens=10_000)

    assert boundary == "u1"


@pytest.mark.asyncio
async def test_summarization_strategy_compact_happy_path() -> None:
    messages = [
        _user("u1", "Need to debug parser."),
        _assistant(
            "a1",
            None,
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "grep",
                    "arguments": {"query": "parse"},
                }
            ],
        ),
        _tool(
            "t1",
            tool_call_id="call_1",
            name="grep",
            content="SECRET TOOL OUTPUT",
        ),
        _assistant("a1f", "Tool says parse_line is missing."),
        _user("u2", "Continue with the fix."),
        _assistant("a2", "I will continue."),
    ]

    adapter = StubSummaryAdapter({"content": "Compacted context for continuation."})
    storage = StubStorage("Create a continuation context.")
    settings = CompactionSettings(tail_tokens=4)
    strategy = SummarizationStrategy()

    checkpoint = await strategy.compact(
        messages,
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="anthropic/claude-sonnet-4",
        storage=storage,
        settings=settings,
    )

    expected_compacted_tokens = sum(
        estimate_message_tokens(message.to_dict())[0]
        for message in messages
        if message.id in {"u1", "a1", "t1", "a1f"}
    )

    assert checkpoint.role == "compaction_checkpoint"
    assert checkpoint.tail_boundary_id == "u2"
    assert checkpoint.content == "Compacted context for continuation."
    assert checkpoint.usage == {"compacted_token_count": expected_compacted_tokens}

    assert storage.requested_fragments == ["compaction.md"]
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request["model_id"] == "anthropic/claude-sonnet-4"
    assert request["kwargs"]["temperature"] == 0.0
    assert request["kwargs"]["thinking_effort"] == ""
    assert request["messages"][0]["role"] == "user"

    prompt = request["messages"][0]["content"]
    assert "Need to debug parser." in prompt
    assert "SECRET TOOL OUTPUT" not in prompt
    assert TOOL_RESULT_CONTENT_PLACEHOLDER in prompt


def test_compaction_service_should_auto_compact_thresholds() -> None:
    service = CompactionService(NoopStrategy())

    assert service.should_auto_compact(80, 100, 0.8) is True
    assert service.should_auto_compact(81, 100, 0.8) is True
    assert service.should_auto_compact(79, 100, 0.8) is False


def test_compaction_service_estimate_messages_tokens() -> None:
    service = CompactionService(NoopStrategy())

    plain_estimated = service.estimate_messages_tokens(
        [
            {"content": "abcd"},
            {"content": "abcde"},
        ]
    )
    structured_estimated = service.estimate_messages_tokens(
        [
            {
                "role": "assistant",
                "content": None,
                "reasoning": "Need a large tool call.",
                "tool_calls": [
                    {
                        "id": "call_large",
                        "name": "write_file",
                        "arguments": {"payload": "x" * 20_000},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "see attached"},
                    {"type": "media", "base64": "y" * 1_000, "media_type": "image/png"},
                ],
            },
        ]
    )

    assert plain_estimated == 3
    assert structured_estimated > 5_000


def test_chat_message_compaction_checkpoint_round_trip() -> None:
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Summary for continuation.",
        tail_boundary_id="user_tail_1",
        compacted_token_count=123,
        timestamp=FIXED_TIMESTAMP,
    )

    payload = checkpoint.to_dict()
    parsed = ChatMessage.from_dict(payload)
    parsed.validate()

    assert payload == {
        "id": checkpoint.id,
        "timestamp": "2026-05-19T12:00:00+00:00",
        "role": "compaction_checkpoint",
        "content": "Summary for continuation.",
        "usage": {"compacted_token_count": 123},
        "tail_boundary_id": "user_tail_1",
    }
    assert parsed.to_dict() == payload


def test_chat_message_from_dict_parses_compaction_tail_boundary() -> None:
    message = ChatMessage.from_dict(
        {
            "id": "checkpoint_1",
            "timestamp": TIMESTAMP,
            "role": "compaction_checkpoint",
            "content": "Summary content",
            "tail_boundary_id": "user_boundary_2",
            "usage": {"compacted_token_count": 77},
        }
    )

    assert message.role == "compaction_checkpoint"
    assert message.tail_boundary_id == "user_boundary_2"
    assert message.usage == {"compacted_token_count": 77}
