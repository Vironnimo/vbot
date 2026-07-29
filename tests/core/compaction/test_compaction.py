"""Tests for the policy-driven compaction engine."""

from __future__ import annotations

from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.messages import COMPACTION_SUMMARY_NOTE_PREFIX, _effective_compaction_messages
from core.compaction import (
    TOOL_RESULT_CONTENT_PLACEHOLDER,
    CompactionError,
    CompactionService,
    CompactionSettings,
    find_tail_boundary,
)
from core.utils.tokens import NATIVE_MEDIA_TOKEN_RESERVE

TIMESTAMP = "2026-05-19T12:00:00+00:00"


class StubStorage:
    def read_prompt_fragment(self, name: str) -> str:
        assert name == "compaction.md"
        return "Preserve decisions and unfinished work."


class StubAdapter:
    def __init__(self, text: str = "COMPACTED") -> None:
        self.text = text
        self.requests: list[dict[str, Any]] = []

    async def send(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
        self.requests.append({"messages": messages, **kwargs})
        return {"content": self.text}

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        return response


def message(message_id: str, role: str, content: str, **extra: Any) -> ChatMessage:
    return ChatMessage.from_dict(
        {"id": message_id, "timestamp": TIMESTAMP, "role": role, "content": content, **extra}
    )


def user(message_id: str, content: str) -> ChatMessage:
    return message(message_id, "user", content)


def assistant(message_id: str, content: str) -> ChatMessage:
    return message(message_id, "assistant", content, model="openai/gpt-5")


def checkpoint(projection: list[ChatMessage], count: int = 10) -> ChatMessage:
    return ChatMessage.compaction_checkpoint(
        summary="PRIOR",
        projection=projection,
        compacted_token_count=count,
        policy="summary_tail",
        strategy="summary_tail",
    )


def test_find_tail_boundary_keeps_complete_user_turns() -> None:
    messages = [
        user("u1", "first " * 30),
        assistant("a1", "answer " * 30),
        user("u2", "second " * 30),
        assistant("a2", "answer " * 30),
        user("u3", "last"),
        assistant("a3", "last answer"),
    ]

    assert find_tail_boundary(messages, tail_tokens=5) == "u3"


def test_context_ratio_and_absolute_token_triggers() -> None:
    service = CompactionService()

    assert service.should_auto_compact(80, 100, 0.8)
    assert not service.should_auto_compact(79, 100, 0.8)
    settings = CompactionSettings(trigger="input_tokens", trigger_tokens=100_000)
    assert service.should_auto_compact(100_000, 1_000_000, 0.8, settings=settings)
    assert not service.should_auto_compact(99_999, 1_000_000, 0.8, settings=settings)


def test_request_estimate_reserves_tool_result_media_without_counting_base64() -> None:
    encoded = "A" * 100_000
    messages = [
        {
            "role": "tool",
            "content": '{"ok":true}',
            "tool_call_id": "call-image",
            "tool_result_content": [
                {
                    "type": "media",
                    "media_type": "image/png",
                    "base64": encoded,
                }
            ],
        }
    ]

    estimated_tokens = CompactionService().estimate_messages_tokens(messages)

    assert estimated_tokens >= NATIVE_MEDIA_TOKEN_RESERVE
    assert estimated_tokens < NATIVE_MEDIA_TOKEN_RESERVE + 100


@pytest.mark.asyncio
async def test_summary_tail_executes_one_call_and_materializes_projection() -> None:
    summary_adapter = StubAdapter("NEW SUMMARY")
    active_adapter = StubAdapter("must not be used")
    messages = [
        user("u1", "old request " * 100),
        assistant("a1", "old response " * 100),
        user("u2", "recent request"),
        assistant("a2", "recent response"),
    ]

    result = await CompactionService().compact(
        messages,
        agent=object(),
        summary_adapter=summary_adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=5),
        active_adapter=active_adapter,
        active_model_id="openai/active",
    )

    assert len(summary_adapter.requests) == 1
    assert active_adapter.requests == []
    assert summary_adapter.requests[0]["model_id"] == "openai/summary"
    assert "old request" in summary_adapter.requests[0]["messages"][0]["content"]
    effective = _effective_compaction_messages([*messages, result])
    assert effective[0].role == "note"
    assert effective[0].content == f"{COMPACTION_SUMMARY_NOTE_PREFIX}NEW SUMMARY"
    assert [item.id for item in effective[1:]] == ["u2", "a2"]


@pytest.mark.asyncio
async def test_next_compaction_consumes_previous_projection_not_hidden_history() -> None:
    adapter = StubAdapter("NEXT")
    prior = checkpoint(
        [ChatMessage.note(f"{COMPACTION_SUMMARY_NOTE_PREFIX}PRIOR"), user("u2", "kept")]
    )
    hidden = user("u1", "hidden-secret-marker")

    await CompactionService().compact(
        [hidden, prior, assistant("a2", "new")],
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
    )

    rendered = adapter.requests[0]["messages"][0]["content"]
    assert "hidden-secret-marker" not in rendered
    assert "PRIOR" in rendered


def test_summary_tail_auto_compaction_waits_when_only_prior_summary_is_eligible() -> None:
    current_user = user("u1", "One long-running user turn")
    carrier = message(
        "a1",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-1", "name": "read", "arguments": {"path": "one"}}],
    )
    result = message(
        "t1",
        "tool",
        "result one",
        tool_call_id="call-1",
        name="read",
    )
    prior = checkpoint([current_user, carrier, result])
    next_carrier = message(
        "a2",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-2", "name": "edit", "arguments": {"path": "one"}}],
    )
    next_result = message(
        "t2",
        "tool",
        "result two",
        tool_call_id="call-2",
        name="edit",
    )

    can_compact = CompactionService().has_new_compactable_context(
        [current_user, carrier, result, prior, next_carrier, next_result],
        CompactionSettings(tail_tokens=1),
    )

    assert can_compact is False


def test_summary_tail_auto_compaction_resumes_when_retained_turn_becomes_eligible() -> None:
    retained_user = user("u1", "Previously retained turn")
    retained_assistant = assistant("a1", "Previously retained answer")
    prior = checkpoint([retained_user, retained_assistant])
    next_user = user("u2", "A new turn that advances the tail boundary")

    can_compact = CompactionService().has_new_compactable_context(
        [retained_user, retained_assistant, prior, next_user],
        CompactionSettings(tail_tokens=1),
    )

    assert can_compact is True


@pytest.mark.asyncio
async def test_summary_prompt_omits_raw_tool_result_content() -> None:
    adapter = StubAdapter()
    tool_call = {"id": "call-1", "name": "read", "arguments": {}}
    messages = [
        user("u1", "old"),
        message("a1", "assistant", "", model="openai/gpt-5", tool_calls=[tool_call]),
        message("t1", "tool", "sensitive-output", tool_call_id="call-1", name="read"),
        user("u2", "tail"),
    ]

    await CompactionService().compact(
        messages,
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
    )

    prompt = adapter.requests[0]["messages"][0]["content"]
    assert TOOL_RESULT_CONTENT_PLACEHOLDER in prompt
    assert "sensitive-output" not in prompt


@pytest.mark.asyncio
async def test_continuation_preserves_request_prefix_and_active_tools() -> None:
    active = StubAdapter("ACTIVE SUMMARY")
    summary = StubAdapter("must not be used")
    request = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer"},
    ]
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]

    result = await CompactionService().compact(
        [user("u1", "hello"), assistant("a1", "answer")],
        agent=object(),
        summary_adapter=summary,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="continuation"),
        request_messages=request,
        active_adapter=active,
        active_model_id="openai/active",
        active_tools=tools,
    )

    assert summary.requests == []
    assert len(active.requests) == 1
    assert active.requests[0]["messages"][:-1] == request
    assert "checkpoint" in active.requests[0]["messages"][-1]["content"]
    assert active.requests[0]["tools"] == tools
    assert result.projection is not None
    assert len(result.projection) == 1


@pytest.mark.asyncio
async def test_continuation_requires_active_request_and_target() -> None:
    with pytest.raises(CompactionError, match="active request"):
        await CompactionService().compact(
            [user("u1", "hello")],
            agent=object(),
            summary_adapter=StubAdapter(),
            summary_model_id="openai/summary",
            storage=StubStorage(),
            settings=CompactionSettings(strategy="continuation"),
        )


def test_checkpoint_round_trip_contains_projection_and_provenance() -> None:
    original = checkpoint([user("u2", "tail")])
    restored = ChatMessage.from_dict(original.to_dict())

    assert restored.role == "compaction_checkpoint"
    assert restored.projection is not None
    assert [entry["role"] for entry in restored.projection] == ["note", "user"]
    assert restored.compaction_policy == "summary_tail"
    assert restored.compaction_strategy == "summary_tail"


def test_legacy_checkpoint_is_read_only_input_to_the_new_projection_engine() -> None:
    old = user("u1", "hidden")
    tail = user("u2", "kept tail")
    legacy = ChatMessage.from_dict(
        {
            "id": "c1",
            "timestamp": TIMESTAMP,
            "role": "compaction_checkpoint",
            "content": "old checkpoint summary",
            "tail_boundary_id": "u2",
            "usage": {"compacted_token_count": 40},
        }
    )
    newer = assistant("a2", "new response")

    effective = _effective_compaction_messages([old, tail, legacy, newer])

    assert [message.role for message in effective] == ["note", "user", "assistant"]
    assert effective[0].content == (f"{COMPACTION_SUMMARY_NOTE_PREFIX}old checkpoint summary")
    assert effective[1:] == [tail, newer]
    assert legacy.to_dict()["tail_boundary_id"] == "u2"
