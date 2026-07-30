"""Tests for the policy-driven compaction engine."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.messages import COMPACTION_SUMMARY_NOTE_PREFIX, _effective_compaction_messages
from core.compaction import (
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    TOOL_RESULT_CONTENT_PLACEHOLDER,
    CompactionError,
    CompactionInsufficientReclaimError,
    CompactionService,
    CompactionSettings,
    find_tail_boundary,
    is_compacted_tool_result_content,
)
from core.compaction.compaction import _age_tool_payloads
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
    assert "<previous_summary>\nPRIOR\n</previous_summary>" in rendered
    assert rendered.index("<previous_summary>") < rendered.index("<history>")


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
async def test_summary_tail_ages_old_tool_payloads_inside_one_long_user_turn() -> None:
    adapter = StubAdapter()
    old_arguments = {"path": "old.txt", "replacement": "A" * 12_000}
    old_result_content = json.dumps(
        {
            "ok": True,
            "data": {
                "path": "old.txt",
                "content": "R" * 40_000,
                "line_count": 900,
            },
        }
    )
    old_carrier = message(
        "a-old",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-old", "name": "read", "arguments": old_arguments}],
    )
    old_result = message(
        "t-old",
        "tool",
        old_result_content,
        tool_call_id="call-old",
        name="read",
    )
    latest_carrier = message(
        "a-latest",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-latest", "name": "edit", "arguments": {"path": "latest.txt"}}],
    )
    latest_result = message(
        "t-latest",
        "tool",
        json.dumps({"ok": True, "data": {"path": "latest.txt"}}),
        tool_call_id="call-latest",
        name="edit",
    )
    messages = [
        user("u1", "Keep working until the task is complete"),
        old_carrier,
        old_result,
        latest_carrier,
        latest_result,
    ]
    original_snapshot = [item.to_dict() for item in messages]
    service = CompactionService()

    assert service.has_new_compactable_context(
        messages,
        CompactionSettings(tail_tokens=1),
    )

    result = await service.compact(
        messages,
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
        minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    )

    assert adapter.requests == []
    assert [item.to_dict() for item in messages] == original_snapshot
    effective = _effective_compaction_messages([*messages, result])
    aged_carrier = next(item for item in effective if item.id == "a-old")
    aged_result = next(item for item in effective if item.id == "t-old")
    retained_latest = next(item for item in effective if item.id == "t-latest")
    assert aged_carrier.tool_calls is not None
    assert isinstance(aged_carrier.tool_calls[0].arguments, dict)
    assert len(json.dumps(aged_carrier.tool_calls[0].arguments)) <= 2_000
    assert is_compacted_tool_result_content(aged_result.content)
    assert json.loads(str(aged_result.content))["outcome"]["ok"] is True
    assert retained_latest == latest_result
    assert not service.has_new_compactable_context(
        [*messages, result],
        CompactionSettings(tail_tokens=1),
    )


@pytest.mark.asyncio
async def test_tool_aging_reuses_previous_summary_without_model_call() -> None:
    adapter = StubAdapter("must not be used")
    current_user = user("u1", "Continue the current Run")
    old_carrier = message(
        "a1",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "c1", "name": "read", "arguments": {"path": "old"}}],
    )
    old_result = message(
        "t1",
        "tool",
        json.dumps({"ok": True, "data": {"content": "X" * 30_000}}),
        tool_call_id="c1",
        name="read",
    )
    latest_carrier = message(
        "a2",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "c2", "name": "read", "arguments": {"path": "latest"}}],
    )
    latest_result = message(
        "t2",
        "tool",
        json.dumps({"ok": True, "data": {"path": "latest"}}),
        tool_call_id="c2",
        name="read",
    )
    prior = checkpoint(
        [current_user, old_carrier, old_result, latest_carrier, latest_result],
        count=500,
    )

    result = await CompactionService().compact(
        [current_user, old_carrier, old_result, latest_carrier, latest_result, prior],
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
        minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    )

    assert adapter.requests == []
    assert result.content == "PRIOR"
    effective = _effective_compaction_messages(
        [current_user, old_carrier, old_result, latest_carrier, latest_result, prior, result]
    )
    summary_notes = [
        item
        for item in effective
        if item.role == "note"
        and isinstance(item.content, str)
        and item.content.startswith(COMPACTION_SUMMARY_NOTE_PREFIX)
    ]
    assert [item.content for item in summary_notes] == [f"{COMPACTION_SUMMARY_NOTE_PREFIX}PRIOR"]


def test_tool_aging_preserves_newest_and_incomplete_batches() -> None:
    messages = [
        user("u1", "long run"),
        message(
            "a1",
            "assistant",
            "",
            model="openai/gpt-5",
            tool_calls=[{"id": "c1", "name": "read", "arguments": {"path": "one"}}],
        ),
        message(
            "t1",
            "tool",
            json.dumps({"ok": True, "data": {"content": "X" * 30_000}}),
            tool_call_id="c1",
            name="read",
        ),
        message(
            "a2",
            "assistant",
            "",
            model="openai/gpt-5",
            tool_calls=[{"id": "c2", "name": "read", "arguments": {"path": "two"}}],
        ),
        message(
            "t2",
            "tool",
            json.dumps({"ok": True, "data": {"content": "Y" * 30_000}}),
            tool_call_id="c2",
            name="read",
        ),
        message(
            "a3",
            "assistant",
            "",
            model="openai/gpt-5",
            tool_calls=[{"id": "c3", "name": "read", "arguments": {"path": "three"}}],
        ),
    ]

    projected, reclaimed = _age_tool_payloads(messages)

    assert reclaimed > 0
    assert is_compacted_tool_result_content(projected[2].content)
    assert projected[4] == messages[4]
    assert projected[5] == messages[5]


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
async def test_summary_prompt_includes_bounded_structured_tool_outcome() -> None:
    adapter = StubAdapter()
    raw_body = "private-body-" * 3_000
    messages = [
        user("u1", "old"),
        message(
            "a1",
            "assistant",
            "",
            model="openai/gpt-5",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "read",
                    "arguments": {
                        "path": "report.txt",
                        "query": "Q" * 8_000,
                        "api_key": "argument-secret",
                    },
                }
            ],
        ),
        message(
            "t1",
            "tool",
            json.dumps(
                {
                    "ok": True,
                    "data": {
                        "path": "report.txt",
                        "line_count": 42,
                        "content": raw_body,
                        "access_token": "result-secret",
                    },
                }
            ),
            tool_call_id="call-1",
            name="read",
        ),
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
    assert '"ok":true' in prompt
    assert "report.txt" in prompt
    assert "line_count" in prompt
    assert raw_body not in prompt
    assert "Q" * 8_000 not in prompt
    assert "argument-secret" not in prompt
    assert "result-secret" not in prompt
    assert "[REDACTED]" in prompt


@pytest.mark.asyncio
async def test_automatic_compaction_rejects_projection_below_minimum_reclaim() -> None:
    adapter = StubAdapter("A summary that is intentionally much larger " * 500)
    messages = [
        user("u1", "old"),
        assistant("a1", "short"),
        user("u2", "tail"),
    ]

    with pytest.raises(
        CompactionInsufficientReclaimError,
        match="minimum is 4096",
    ):
        await CompactionService().compact(
            messages,
            agent=object(),
            summary_adapter=adapter,
            summary_model_id="openai/summary",
            storage=StubStorage(),
            settings=CompactionSettings(tail_tokens=1),
            minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
        )

    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_compaction_checkpoint_records_context_tokens_before_and_after() -> None:
    service = CompactionService()
    messages = [
        user("u1", "old context " * 2_000),
        assistant("a1", "old answer " * 2_000),
        user("u2", "tail"),
        assistant("a2", "tail answer"),
    ]

    result = await service.compact(
        messages,
        agent=object(),
        summary_adapter=StubAdapter("Short retained summary."),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
        context_tokens_before=25_000,
    )

    assert result.usage is not None
    assert result.usage["context_tokens_before"] == 25_000
    assert 0 <= result.usage["context_tokens_after"] < 25_000


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
